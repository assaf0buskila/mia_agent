"""Bounded, local OOXML reader. No evaluation, extraction, providers or configuration."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_CELLS = 2_000_000
MAX_COMPRESSION_RATIO = 100
_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?")
_REF = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})")


class XlsxError(ValueError):
    """The message is a fixed reason code, never an input value or a path."""


@dataclass(frozen=True)
class Cell:
    value: str | bool | None = field(repr=False)
    kind: str = "number"
    date_format: bool = False
    formula: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Sheet:
    name: str = field(repr=False)
    state: str
    cells: dict[str, Cell] = field(repr=False)


@dataclass(frozen=True)
class Workbook:
    sheets: tuple[Sheet, ...] = field(repr=False)
    source_digest: str = field(repr=False)
    date_1904: bool = False


def _safe_member(name: str) -> None:
    normalized = name.replace("\\", "/")
    if (not name or PurePosixPath(normalized).is_absolute()
            or PureWindowsPath(name).drive or ".." in normalized.split("/")
            or "\\" in name):
        raise XlsxError("unsafe_member")


def _preflight(archive: ZipFile) -> None:
    infos = archive.infolist()
    names: set[str] = set()
    total = 0
    for info in infos:
        _safe_member(info.filename)
        if info.filename in names:
            raise XlsxError("duplicate_member")
        names.add(info.filename)
        if info.flag_bits & 1:
            raise XlsxError("encrypted_member")
        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise XlsxError("uncompressed_size_limit")
        if info.file_size > MAX_COMPRESSION_RATIO * max(info.compress_size, 1):
            raise XlsxError("compression_ratio_limit")

    actual_size = 0
    cells = 0
    for info in infos:
        # Scan every part, including unreferenced/non-XML members. Removing NULs also
        # detects UTF-16/32 declarations before ElementTree can process an entity.
        carry = b""
        with archive.open(info) as stream:
            while chunk := stream.read(65536):
                actual_size += len(chunk)
                if actual_size > MAX_UNCOMPRESSED_BYTES:
                    raise XlsxError("uncompressed_size_limit")
                probe = carry + chunk.replace(b"\x00", b"").upper()
                if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
                    raise XlsxError("xml_declaration_forbidden")
                carry = probe[-16:]
    # Count before building any full XML tree, also in unreferenced worksheets.
    for info in infos:
        if info.filename.lower().endswith((".xml", ".rels")):
            with archive.open(info) as stream:
                for event, element in ET.iterparse(stream, events=("start", "end")):
                    if event == "start" and element.tag == f"{{{_NS}}}c":
                        cells += 1
                        if cells > MAX_CELLS:
                            raise XlsxError("cell_count_limit")
                    if event == "end":
                        element.clear()


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    # Phonetic annotations are not part of a cell's displayed string.
    return "".join(node.text or "" for child in element for node in (
        [child] if child.tag == f"{{{_NS}}}t" else child.findall(f"{{{_NS}}}t")
        if child.tag == f"{{{_NS}}}r" else []
    ))


def _date_format(number: int, custom: dict[int, str]) -> bool:
    if number in {*range(14, 23), *range(27, 37), *range(45, 48), *range(50, 59)}:
        return True
    code = custom.get(number, "").lower()
    code = re.sub(r'"[^"]*"|\\.|_.|\*.', "", code)
    code = re.sub(r"\[(?![hms]+\])[^\]]*\]", "", code)
    return re.search(r"[ymdhs]", code) is not None


def _cell(element: ET.Element, strings: list[str], dates: list[bool]) -> Cell:
    kind = element.get("t", "n")
    value = element.findtext(f"{{{_NS}}}v")
    formula_element = element.find(f"{{{_NS}}}f")
    formula = formula_element.text or "" if formula_element is not None else None
    style = int(element.get("s", "0"))
    if style < 0 or style >= len(dates):
        raise XlsxError("invalid_style")
    date_format = dates[style]
    if kind == "s":
        index = int(value or "-1")
        if index < 0 or index >= len(strings):
            raise XlsxError("invalid_shared_string")
        return Cell(strings[index], "string", formula=formula)
    if kind == "inlineStr":
        return Cell(_text(element.find(f"{{{_NS}}}is")), "string", formula=formula)
    if kind in {"str", "e", "d"}:
        return Cell(value, {"str": "string", "e": "error", "d": "iso_date"}[kind],
                    formula=formula)
    if kind == "b":
        if value not in {"0", "1"}:
            raise XlsxError("invalid_boolean")
        return Cell(value == "1", "boolean", formula=formula)
    if kind != "n" or (value is not None and not _NUMBER.fullmatch(value)):
        raise XlsxError("invalid_number")
    return Cell(value, "number", date_format, formula)


def _read(archive: ZipFile, source_digest: str) -> Workbook:
    _preflight(archive)
    book = ET.fromstring(archive.read("xl/workbook.xml"))
    relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets: dict[str, str] = {}
    kinds: dict[str, list[str]] = {}
    for relation in relations.findall(f"{{{_PKG}}}Relationship"):
        if relation.get("TargetMode") == "External":
            raise XlsxError("external_relationship")
        target = relation.attrib["Target"]
        # Absolute OPC relationship targets refer to package parts, not OS paths.
        target = target.lstrip("/") if target.startswith("/") else "xl/" + target
        _safe_member(target)
        identity = relation.attrib["Id"]
        if identity in targets:
            raise XlsxError("duplicate_relationship")
        targets[identity] = target
        kinds.setdefault(relation.attrib["Type"], []).append(target)

    def part(kind: str) -> ET.Element | None:
        paths = kinds.get(f"{_REL}/{kind}", [])
        if len(paths) > 1:
            raise XlsxError("duplicate_relationship")
        return ET.fromstring(archive.read(paths[0])) if paths else None

    shared = part("sharedStrings")
    strings = [_text(item) for item in shared.findall(f"{{{_NS}}}si")] if shared is not None else []
    styles = part("styles")
    dates = [False]
    if styles is not None:
        formats = {int(node.attrib["numFmtId"]): node.attrib["formatCode"]
                   for node in styles.findall(f"{{{_NS}}}numFmts/{{{_NS}}}numFmt")}
        dates = [_date_format(int(node.get("numFmtId", "0")), formats)
                 for node in styles.findall(f"{{{_NS}}}cellXfs/{{{_NS}}}xf")] or [False]

    sheets = []
    names: set[str] = set()
    # The preflight counts only parts that look like XML by extension, but a worksheet relationship
    # may name any part. The same limit is therefore enforced on the cells actually parsed.
    parsed_cells = 0
    for item in book.findall(f"{{{_NS}}}sheets/{{{_NS}}}sheet"):
        name = item.attrib["name"]
        state = item.get("state", "visible")
        if name in names or state not in {"visible", "hidden", "veryHidden"}:
            raise XlsxError("invalid_sheet")
        names.add(name)
        target = targets[item.attrib[f"{{{_REL}}}id"]]
        if target not in kinds.get(f"{_REL}/worksheet", []):
            raise XlsxError("invalid_sheet_relationship")
        cells: dict[str, Cell] = {}
        with archive.open(target) as stream:
            for _, element in ET.iterparse(stream, events=("end",)):
                if element.tag == f"{{{_NS}}}c":
                    parsed_cells += 1
                    if parsed_cells > MAX_CELLS:
                        raise XlsxError("cell_count_limit")
                    ref = element.get("r", "")
                    match = _REF.fullmatch(ref)
                    if not match or ref in cells:
                        raise XlsxError("invalid_cell_reference")
                    column = 0
                    for char in match[1]:
                        column = column * 26 + ord(char) - 64
                    if column > 16384 or int(match[2]) > 1048576:
                        raise XlsxError("invalid_cell_reference")
                    cells[ref] = _cell(element, strings, dates)
                    element.clear()
                elif element.tag == f"{{{_NS}}}row":
                    element.clear()
        sheets.append(Sheet(name, state, cells))
    if not sheets:
        raise XlsxError("missing_sheets")
    properties = book.find(f"{{{_NS}}}workbookPr")
    date_1904 = properties is not None and properties.get("date1904") in {"1", "true"}
    return Workbook(tuple(sheets), source_digest, date_1904)


def read_xlsx(path: str | Path) -> Workbook:
    """Read exact cell values, preserving decimal text and cached formula values.

    All sheets, including hidden journals, are returned. Dates are never guessed or
    converted: a numeric serial retains its value and date-format flag.
    """
    try:
        with open(path, "rb") as source:
            fingerprint = hashlib.file_digest(source, "sha256").hexdigest()
            source.seek(0)
            with ZipFile(source) as archive:
                return _read(archive, fingerprint)
    except XlsxError:
        raise
    except (OSError, BadZipFile, ET.ParseError, KeyError, ValueError,
            RuntimeError, NotImplementedError, OverflowError):
        raise XlsxError("invalid_xlsx") from None
