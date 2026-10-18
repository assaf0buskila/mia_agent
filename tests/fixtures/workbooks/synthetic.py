"""Synthetic OOXML builders for the V2-30 importer tests. No real workbook is ever read or stored.

``make_xlsx`` writes one raw-XML sheet (reader and attack cases). ``write_book`` writes a whole
physical workbook from rows. ``synthetic_records`` and ``tables_from_records`` build a coherent
native workbook with its hidden accepted journal, using the prototype as the normalization oracle.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from xml.etree import ElementTree as ET
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from app.prototypes import assaf_os_export_provenance as oracle
from app.prototypes.assaf_os_domain import ENTITIES, ID_KEYS, SCHEMA
from app.services.os_ledger import api

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
JOURNAL = "_assaf_os_accepted"
_EPOCH = (2026, 1, 1, 0, 0, 0)


def _member(archive: ZipFile, name: str, body, compression: int = ZIP_STORED) -> None:
    """Write a member with a fixed timestamp, so the same tables are the same bytes (and the same
    source digest) whenever they are built. ``writestr(name, ...)`` would stamp the current time."""
    info = ZipInfo(name, _EPOCH)
    info.compress_type = compression
    archive.writestr(info, body)


def column(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def make_xlsx(path, cells="", *, extra=None, compression=ZIP_STORED):
    """One hidden raw-XML sheet. ``extra`` adds or overrides whole package parts."""
    parts = {
        "xl/workbook.xml": (
            f'<workbook xmlns="{NS}" xmlns:r="{REL}"><sheets>'
            '<sheet name="journal" sheetId="1" state="hidden" r:id="rId1"/>'
            '</sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" '
            f'Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{REL}/sharedStrings" '
            'Target="sharedStrings.xml"/>'
            f'<Relationship Id="rId3" Type="{REL}/styles" Target="styles.xml"/>'
            '</Relationships>'
        ),
        "xl/sharedStrings.xml": (
            f'<sst xmlns="{NS}"><si><r><t>בדיקה</t></r><r><t> בלבד</t></r></si></sst>'
        ),
        "xl/styles.xml": (
            f'<styleSheet xmlns="{NS}"><numFmts count="2">'
            '<numFmt numFmtId="164" formatCode="yyyy-mm-dd"/>'
            '<numFmt numFmtId="165" formatCode="0.00 &quot;days&quot;"/>'
            '</numFmts><cellXfs count="4"><xf numFmtId="0"/><xf numFmtId="14"/>'
            '<xf numFmtId="164"/><xf numFmtId="165"/></cellXfs></styleSheet>'
        ),
        "xl/worksheets/sheet1.xml": (
            f'<worksheet xmlns="{NS}"><sheetData><row r="1">{cells}</row>'
            '</sheetData></worksheet>'
        ),
    }
    parts.update(extra or {})
    with ZipFile(path, "w", compression=compression) as archive:
        for name, body in parts.items():
            _member(archive, name, body, compression)
    return path


def write_book(path, tables):
    """A workbook of inline-string sheets; a sheet named ``_...`` is hidden."""
    book = ET.Element("workbook", xmlns=NS)
    sheets = ET.SubElement(book, "sheets")
    relationships = ET.Element("Relationships", xmlns=PKG)
    with ZipFile(path, "w", compression=ZIP_STORED) as archive:
        for index, (name, table) in enumerate(tables.items(), 1):
            ET.SubElement(sheets, "sheet", {
                "name": name, "sheetId": str(index), f"{{{REL}}}id": f"r{index}",
                "state": "hidden" if name.startswith("_") else "visible",
            })
            ET.SubElement(relationships, "Relationship", {
                "Id": f"r{index}", "Type": f"{REL}/worksheet",
                "Target": f"worksheets/s{index}.xml",
            })
            sheet = ET.Element("worksheet", xmlns=NS)
            data = ET.SubElement(sheet, "sheetData")
            for row_number, values in enumerate(table, 1):
                row = ET.SubElement(data, "row", r=str(row_number))
                for col, value in enumerate(values, 1):
                    cell = ET.SubElement(row, "c", r=f"{column(col)}{row_number}")
                    if type(value) in {int, float}:
                        ET.SubElement(cell, "v").text = str(value)
                    else:
                        cell.set("t", "inlineStr")
                        ET.SubElement(ET.SubElement(cell, "is"), "t").text = str(value or "")
            _member(archive, f"xl/worksheets/s{index}.xml", ET.tostring(sheet))
        _member(archive, "xl/workbook.xml", ET.tostring(book))
        _member(archive, "xl/_rels/workbook.xml.rels", ET.tostring(relationships))
    return path


def synthetic_records() -> dict[str, list[dict]]:
    """Two currencies, a refund chain and Hebrew enums; every record at revision 1."""
    records: dict[str, list[dict]] = {entity: [] for entity in ENTITIES}
    records["clients"] = [{"person_id": "PER_ONE", "revision": 1,
                           "name": "Synthetic only", "status": "פעיל"}]
    for currency in ("ILS", "USD"):
        records["works"].append({
            "work_id": "WOR_" + currency, "person_id": "PER_ONE", "revision": 1,
            "title": "Synthetic work", "path": "לימוד", "status": "פעילה",
            "currency": currency, "agreed_amount_ils": 200,
        })
        records["payments"].append({
            "payment_id": "PAY_" + currency, "person_id": "PER_ONE",
            "work_id": "WOR_" + currency, "revision": 1, "path": "לימוד",
            "date": "2026-10-01", "actual_amount_ils": 100,
            "currency": currency, "kind": "התקבל",
        })
    records["payments"].append({**records["payments"][0], "payment_id": "PAY_REFUND",
                                "kind": "החזר", "original_payment_id": "PAY_ILS",
                                "actual_amount_ils": 25})
    return records


def task_record(task_id: str, **over) -> dict:
    return {"task_id": task_id, "revision": 1, "title": "Synthetic task", "status": "לביצוע"} | over


def tables_from_records(
    records: Mapping[str, list[dict]], revisions: Mapping[str, int] | None = None
):
    """Native entity tabs plus a hidden journal that accepts every record at its own revision."""
    revisions = revisions or {}
    tables = {entity: [list(oracle.NATIVE_ENTITY_HEADERS[entity])] for entity in ENTITIES}
    journal = [list(oracle.ACCEPTED_HEADERS)]
    references = {entity: frozenset(row[ID_KEYS[entity]] for row in rows)
                  for entity, rows in records.items()}
    for entity, rows in records.items():
        for record in rows:
            record = {**record, "revision": revisions.get(record[ID_KEYS[entity]],
                                                          record["revision"])}
            physical = [record.get(field["key"], "") for field in oracle.NATIVE_FIELDS[entity]]
            tables[entity].append(physical)
            canonical = oracle._normalise_record(
                entity, oracle._native_keyed_row(entity, physical), "native_major_decimal",
                reference_ids=references,
            )
            inputs = {field["key"]: canonical[field["key"]]
                      for field in SCHEMA["entities"][entity]}
            # The native journal keeps one accepted row per revision: 1..N with no gap.
            for revision in range(1, record["revision"] + 1):
                snapshot = {"entity": entity, "record_id": record[ID_KEYS[entity]],
                            "revision": revision, "input": inputs}
                journal.append([
                    entity, record[ID_KEYS[entity]], revision, api.digest(inputs),
                    oracle.native_raw_owned_digest(entity, physical, references),
                    json.dumps(snapshot, ensure_ascii=False), "2026-10-02T00:00:00+00:00",
                    "OS", "synthetic-request",
                ])
    tables[JOURNAL] = journal
    return tables


def synthetic_tables(revisions: Mapping[str, int] | None = None):
    return tables_from_records(synthetic_records(), revisions)
