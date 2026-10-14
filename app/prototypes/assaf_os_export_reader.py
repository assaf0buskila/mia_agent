"""Default-off, synthetic-only evidence reader for a future Sheet export.

This module has no provider client, credentials, settings integration, routes, or
write operations. It can only replay caller-supplied JSON fixtures through the
exact ``SyntheticGetTransport`` type. Matching fixture reads are merely a
candidate for provenance mapping; they are not a transactional Google snapshot,
an authority switch, or evidence that a real source was frozen.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from urllib.parse import quote, unquote

MAX_TOTAL_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_REQUESTS = 200
MAX_ROWS_PER_PASS = 2_000
MAX_CHUNK_ROWS = 200
MAX_COLUMNS = 64
MAX_JSON_DEPTH = 12
MAX_JSON_NODES = 100_000
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_RANGES = 49

_PATH_RE = re.compile(r"^/v4/spreadsheets/([^/?#]+)/values/([^/?#]+)$")
_WORKBOOK_RE = re.compile(r"^[A-Za-z0-9_-]{8,256}$")
_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_A1_RE = re.compile(
    r"^(?P<sheet>(?:'(?:[^']|'')+'|[A-Za-z][A-Za-z0-9_-]{0,99}))!"
    r"(?P<start_col>[A-Z]{1,3})(?P<start_row>[1-9][0-9]{0,6}):"
    r"(?P<end_col>[A-Z]{1,3})(?P<end_row>[1-9][0-9]{0,6})$"
)
_PARAMS_UNFORMATTED = (
    ("majorDimension", "ROWS"),
    ("valueRenderOption", "UNFORMATTED_VALUE"),
)
_PARAMS_FORMULA = (
    ("majorDimension", "ROWS"),
    ("valueRenderOption", "FORMULA"),
)
_GUARD_HEADER = [
    "workspace_id",
    "authority_epoch",
    "source_epoch",
    "schema_version",
    "revision",
    "export_token",
]
_CONSISTENCY_NOTE = "matching_synthetic_reads_require_approved_source_freeze"


class ExportReaderError(Exception):
    """Stable refusal raised before or by the fixture-only read boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ReaderBinding:
    """Invented preparation binding; it does not describe the current workbook."""

    workbook_id: str
    workspace_id: str
    authority_epoch: int
    source_epoch: int
    schema_version: str
    allowed_ranges: tuple[str, ...]
    guard_range: str


@dataclass(frozen=True)
class ReadRequest:
    """A closed request value accepted by ``SyntheticGetTransport.read`` only."""

    method: str
    path: str
    params: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _A1Range:
    text: str
    sheet: str
    start_col: int
    end_col: int
    start_row: int
    end_row: int

    @property
    def rows(self) -> int:
        return self.end_row - self.start_row + 1

    @property
    def columns(self) -> int:
        return self.end_col - self.start_col + 1


def _column_number(value: str) -> int:
    result = 0
    for character in value:
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _a1(value: Any, *, code: str) -> _A1Range:
    if type(value) is not str or len(value) > 256:
        raise ExportReaderError(code)
    match = _A1_RE.fullmatch(value)
    if match is None:
        raise ExportReaderError(code)
    start_col = _column_number(match.group("start_col"))
    end_col = _column_number(match.group("end_col"))
    start_row = int(match.group("start_row"))
    end_row = int(match.group("end_row"))
    if start_col > end_col or start_row > end_row or end_col > MAX_COLUMNS:
        raise ExportReaderError(code)
    raw_sheet = match.group("sheet")
    if raw_sheet.startswith("'"):
        sheet = raw_sheet[1:-1].replace("''", "'")
        if not sheet or "\x00" in sheet:
            raise ExportReaderError(code)
    else:
        sheet = raw_sheet
    return _A1Range(
        text=value,
        sheet=sheet,
        start_col=start_col,
        end_col=end_col,
        start_row=start_row,
        end_row=end_row,
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _json_copy(value: Any) -> Any:
    """Copy only values already proven to be exact built-in JSON types."""

    return json.loads(_canonical(value))


def _strict_json(value: Any) -> None:
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ExportReaderError("response_json_too_complex")
        if item is None:
            raise ExportReaderError("response_json_null")
        if type(item) in {str, bool}:
            if type(item) is str and "\x00" in item:
                raise ExportReaderError("response_json_invalid")
            continue
        if type(item) is int:
            if not -MAX_SAFE_INTEGER <= item <= MAX_SAFE_INTEGER:
                raise ExportReaderError("response_json_invalid")
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise ExportReaderError("response_json_invalid")
            continue
        if type(item) is list:
            pending.extend((child, depth + 1) for child in item)
            continue
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str or "\x00" in key:
                    raise ExportReaderError("response_json_invalid")
                pending.append((child, depth + 1))
            continue
        raise ExportReaderError("response_json_invalid")


def _request_path(workbook_id: str, a1_range: str) -> str:
    return f"/v4/spreadsheets/{quote(workbook_id, safe='')}/values/{quote(a1_range, safe='')}"


def _build_request(
    binding: ReaderBinding,
    a1_range: str,
    *,
    formulas: bool,
) -> ReadRequest:
    return ReadRequest(
        method="GET",
        path=_request_path(binding.workbook_id, a1_range),
        params=_PARAMS_FORMULA if formulas else _PARAMS_UNFORMATTED,
    )


def _request_parts(request: Any) -> tuple[str, str, str]:
    if type(request) is not ReadRequest:
        raise ExportReaderError("invalid_read_request")
    if type(request.method) is not str or request.method != "GET":
        raise ExportReaderError("read_method_refused")
    if type(request.path) is not str or any(
        marker in request.path.lower()
        for marker in (
            "?",
            "#",
            "http://",
            "https://",
        )
    ):
        raise ExportReaderError("read_route_refused")
    match = _PATH_RE.fullmatch(request.path)
    if match is None:
        raise ExportReaderError("read_route_refused")
    if (
        type(request.params) is not tuple
        or len(request.params) != 2
        or any(
            type(pair) is not tuple
            or len(pair) != 2
            or any(type(part) is not str for part in pair)
            for pair in request.params
        )
        or (request.params != _PARAMS_UNFORMATTED and request.params != _PARAMS_FORMULA)
    ):
        raise ExportReaderError("read_query_refused")
    try:
        workbook_id = unquote(match.group(1), errors="strict")
        a1_range = unquote(match.group(2), errors="strict")
    except (UnicodeDecodeError, ValueError):
        raise ExportReaderError("read_route_refused") from None
    if request.path != _request_path(workbook_id, a1_range):
        raise ExportReaderError("read_route_refused")
    _a1(a1_range, code="read_route_refused")
    return workbook_id, a1_range, dict(request.params)["valueRenderOption"]


class SyntheticGetTransport:
    """Preparation-only fixture transport with no HTTP or mutation implementation."""

    __slots__ = ("_workbook_id", "_responses", "_offsets", "_readonly_calls")

    def __init__(self, workbook_id: str, responses: dict[str, list[dict[str, Any]]]):
        if type(workbook_id) is not str or _WORKBOOK_RE.fullmatch(workbook_id) is None:
            raise ExportReaderError("invalid_transport_workbook")
        if type(responses) is not dict or not responses:
            raise ExportReaderError("invalid_synthetic_responses")
        encoded: dict[str, tuple[bytes, ...]] = {}
        for a1_range, scripts in responses.items():
            _a1(a1_range, code="invalid_synthetic_range")
            if type(scripts) is not list or any(type(item) is not dict for item in scripts):
                raise ExportReaderError("invalid_synthetic_responses")
            for item in scripts:
                _strict_json(item)
            encoded[a1_range] = tuple(_canonical(item) for item in scripts)
        self._workbook_id = workbook_id
        self._responses = MappingProxyType(encoded)
        self._offsets = dict.fromkeys(encoded, 0)
        self._readonly_calls: list[ReadRequest] = []

    @property
    def workbook_id(self) -> str:
        return self._workbook_id

    @property
    def ranges(self) -> frozenset[str]:
        return frozenset(self._responses)

    @property
    def readonly_calls(self) -> tuple[ReadRequest, ...]:
        return tuple(self._readonly_calls)

    def read(self, request: ReadRequest) -> dict[str, Any]:
        workbook_id, a1_range, _render = _request_parts(request)
        if workbook_id != self._workbook_id:
            raise ExportReaderError("workbook_not_allowed")
        if a1_range not in self._responses:
            raise ExportReaderError("range_not_allowed")
        self._readonly_calls.append(request)
        offset = self._offsets[a1_range]
        scripts = self._responses[a1_range]
        if offset >= len(scripts):
            raise ExportReaderError("synthetic_response_missing")
        self._offsets[a1_range] = offset + 1
        return json.loads(scripts[offset])


def _validate_binding(binding: Any, chunk_rows: Any) -> tuple[dict[str, _A1Range], _A1Range]:
    if type(binding) is not ReaderBinding:
        raise ExportReaderError("invalid_reader_binding")
    if (
        type(binding.workbook_id) is not str
        or _WORKBOOK_RE.fullmatch(binding.workbook_id) is None
    ):
        raise ExportReaderError("invalid_workbook_id")
    if (
        type(binding.workspace_id) is not str
        or _WORKSPACE_RE.fullmatch(binding.workspace_id) is None
    ):
        raise ExportReaderError("invalid_workspace_id")
    if (
        type(binding.authority_epoch) is not int
        or not 0 < binding.authority_epoch <= MAX_SAFE_INTEGER
        or type(binding.source_epoch) is not int
        or not 0 < binding.source_epoch <= MAX_SAFE_INTEGER
    ):
        raise ExportReaderError("invalid_binding_epoch")
    if (
        type(binding.schema_version) is not str
        or _VERSION_RE.fullmatch(binding.schema_version) is None
    ):
        raise ExportReaderError("invalid_schema_version")
    if type(chunk_rows) is not int or not 1 <= chunk_rows <= MAX_CHUNK_ROWS:
        raise ExportReaderError("invalid_chunk_rows")
    if (
        type(binding.allowed_ranges) is not tuple
        or not 1 <= len(binding.allowed_ranges) <= MAX_RANGES
        or any(type(item) is not str for item in binding.allowed_ranges)
        or len(set(binding.allowed_ranges)) != len(binding.allowed_ranges)
    ):
        raise ExportReaderError("invalid_allowed_ranges")

    parsed: dict[str, _A1Range] = {}
    total_rows = 0
    for value in binding.allowed_ranges:
        item = _a1(value, code="invalid_allowed_range")
        if item.rows > chunk_rows:
            raise ExportReaderError("range_chunk_too_large")
        total_rows += item.rows
        parsed[value] = item
    if total_rows > MAX_ROWS_PER_PASS:
        raise ExportReaderError("data_row_budget_exceeded")

    guard = _a1(binding.guard_range, code="invalid_guard_range")
    if binding.guard_range in parsed:
        raise ExportReaderError("guard_range_is_data_range")
    if guard.rows > 2 or guard.columns < len(_GUARD_HEADER) or guard.columns > 8:
        raise ExportReaderError("invalid_guard_range")
    for item in parsed.values():
        overlaps = (
            item.sheet == guard.sheet
            and item.start_row <= guard.end_row
            and guard.start_row <= item.end_row
            and item.start_col <= guard.end_col
            and guard.start_col <= item.end_col
        )
        if overlaps:
            raise ExportReaderError("guard_range_overlaps_data")
    request_count = 2 * (2 + 2 * len(parsed))
    if request_count > MAX_REQUESTS:
        raise ExportReaderError("request_budget_exceeded")
    return parsed, guard


def _validate_envelope(response: Any, expected: _A1Range) -> dict[str, Any]:
    _strict_json(response)
    if type(response) is not dict or set(response) != {
        "range",
        "majorDimension",
        "values",
    }:
        raise ExportReaderError("invalid_response_envelope")
    if response["range"] != expected.text or response["majorDimension"] != "ROWS":
        raise ExportReaderError("response_binding_mismatch")
    values = response["values"]
    if type(values) is not list or len(values) > expected.rows:
        raise ExportReaderError("response_matrix_bounds")
    for row in values:
        if type(row) is not list or len(row) > expected.columns:
            raise ExportReaderError("response_matrix_bounds")
        for cell in row:
            if type(cell) not in {str, bool, int, float}:
                raise ExportReaderError("response_cell_invalid")
    return _json_copy(response)


def _guard_values(response: dict[str, Any], binding: ReaderBinding) -> tuple[Any, ...]:
    values = response["values"]
    if (
        len(values) != 2
        or values[0] != _GUARD_HEADER
        or len(values[1]) != len(_GUARD_HEADER)
    ):
        raise ExportReaderError("guard_shape_invalid")
    row = values[1]
    checks = (
        (row[0], binding.workspace_id, "guard_workspace_mismatch"),
        (row[1], binding.authority_epoch, "guard_authority_epoch_mismatch"),
        (row[2], binding.source_epoch, "guard_source_epoch_mismatch"),
        (row[3], binding.schema_version, "guard_schema_version_mismatch"),
    )
    for actual, expected, code in checks:
        if type(actual) is not type(expected) or actual != expected:
            raise ExportReaderError(code)
    if type(row[4]) is not int or not 0 < row[4] <= MAX_SAFE_INTEGER:
        raise ExportReaderError("guard_revision_invalid")
    if (
        type(row[5]) is not str
        or not 1 <= len(row[5]) <= 256
        or "\x00" in row[5]
    ):
        raise ExportReaderError("guard_export_token_invalid")
    return tuple(row)


class LocalExportReader:
    """Run exactly two bounded passes against a synthetic GET fixture transport."""

    def __init__(
        self,
        binding: ReaderBinding,
        transport: SyntheticGetTransport,
        enabled: bool = False,
        chunk_rows: int = MAX_CHUNK_ROWS,
    ) -> None:
        if type(enabled) is not bool:
            raise ExportReaderError("invalid_reader_configuration")
        parsed, guard = _validate_binding(binding, chunk_rows)
        if type(transport) is not SyntheticGetTransport:
            raise ExportReaderError("synthetic_transport_required")
        if transport.workbook_id != binding.workbook_id:
            raise ExportReaderError("workbook_not_allowed")
        expected_ranges = frozenset((*binding.allowed_ranges, binding.guard_range))
        if transport.ranges != expected_ranges:
            raise ExportReaderError("transport_range_allowlist_mismatch")
        self.binding = binding
        self._transport = transport
        self._enabled = enabled
        self._ranges = parsed
        self._guard = guard

    def _binding_dict(self) -> dict[str, Any]:
        result = asdict(self.binding)
        result["allowed_ranges"] = list(self.binding.allowed_ranges)
        return result

    def extract(self) -> dict[str, Any]:
        if self._enabled is not True:
            raise ExportReaderError("export_reader_disabled")

        bytes_seen = 0
        attempted_response_bytes = 0
        byte_budget_exhausted = False
        requests = 0
        issues: list[dict[str, Any]] = []
        passes: list[dict[str, Any]] = []
        pass_reads: list[dict[str, Any] | None] = []
        all_guards: list[dict[str, Any]] = []
        guard_rows: list[tuple[Any, ...]] = []

        def add_issue(code: str, **context: Any) -> None:
            item = {"code": code}
            item.update(context)
            if item not in issues:
                issues.append(item)

        def read_one(
            a1_range: str,
            parsed: _A1Range,
            *,
            formulas: bool,
            pass_number: int,
            phase: str,
        ) -> dict[str, Any] | None:
            nonlocal attempted_response_bytes, byte_budget_exhausted, bytes_seen, requests
            if byte_budget_exhausted:
                return None
            if requests >= MAX_REQUESTS:
                add_issue("request_budget_exceeded", pass_number=pass_number, phase=phase)
                return None
            request = _build_request(self.binding, a1_range, formulas=formulas)
            requests += 1
            try:
                response = SyntheticGetTransport.read(self._transport, request)
                _strict_json(response)
                response_size = len(_canonical(response))
                attempted_response_bytes = min(
                    MAX_TOTAL_RESPONSE_BYTES + 1,
                    attempted_response_bytes + response_size,
                )
                if bytes_seen + response_size > MAX_TOTAL_RESPONSE_BYTES:
                    byte_budget_exhausted = True
                    raise ExportReaderError("response_byte_budget_exceeded")
                bytes_seen += response_size
                return _validate_envelope(response, parsed)
            except ExportReaderError as exc:
                add_issue(
                    exc.code,
                    pass_number=pass_number,
                    phase=phase,
                    range=a1_range,
                    render="FORMULA" if formulas else "UNFORMATTED_VALUE",
                )
                return None
            except Exception:
                add_issue(
                    "synthetic_read_failed",
                    pass_number=pass_number,
                    phase=phase,
                    range=a1_range,
                    render="FORMULA" if formulas else "UNFORMATTED_VALUE",
                )
                return None

        for pass_number in (1, 2):
            before = read_one(
                self.binding.guard_range,
                self._guard,
                formulas=False,
                pass_number=pass_number,
                phase="guard_before",
            )
            if before is not None:
                all_guards.append(before)
                try:
                    guard_rows.append(_guard_values(before, self.binding))
                except ExportReaderError as exc:
                    add_issue(exc.code, pass_number=pass_number, phase="guard_before")

            reads: dict[str, Any] = {}
            pass_complete = before is not None
            for a1_range, parsed in self._ranges.items():
                unformatted = read_one(
                    a1_range,
                    parsed,
                    formulas=False,
                    pass_number=pass_number,
                    phase="data",
                )
                formulas = read_one(
                    a1_range,
                    parsed,
                    formulas=True,
                    pass_number=pass_number,
                    phase="data",
                )
                if unformatted is None or formulas is None:
                    pass_complete = False
                else:
                    reads[a1_range] = {
                        "unformatted": unformatted,
                        "formulas": formulas,
                    }

            after = read_one(
                self.binding.guard_range,
                self._guard,
                formulas=False,
                pass_number=pass_number,
                phase="guard_after",
            )
            if after is not None:
                all_guards.append(after)
                try:
                    guard_rows.append(_guard_values(after, self.binding))
                except ExportReaderError as exc:
                    add_issue(exc.code, pass_number=pass_number, phase="guard_after")
            else:
                pass_complete = False

            if len(reads) != len(self._ranges):
                pass_complete = False
            read_digest = _digest(reads) if pass_complete else None
            passes.append(
                {
                    "pass_number": pass_number,
                    "read_digest": read_digest,
                    "guard_before": deepcopy(before),
                    "guard_after": deepcopy(after),
                }
            )
            pass_reads.append(reads if pass_complete else None)

        if len(all_guards) == 4:
            guard_digests = [_digest(item) for item in all_guards]
            if len(set(guard_digests)) != 1:
                add_issue("guard_changed")
                if len(guard_rows) == 4 and len({row[4] for row in guard_rows}) != 1:
                    add_issue("guard_revision_changed")
                if guard_digests[0] == guard_digests[-1] and any(
                    item != guard_digests[0] for item in guard_digests[1:-1]
                ):
                    add_issue("guard_aba_detected")
        else:
            add_issue("guard_read_incomplete")

        if pass_reads[0] is not None and pass_reads[1] is not None:
            for a1_range in self.binding.allowed_ranges:
                for render in ("unformatted", "formulas"):
                    if pass_reads[0][a1_range][render] != pass_reads[1][a1_range][render]:
                        add_issue("data_drift", range=a1_range, render=render)
        else:
            add_issue("data_read_incomplete")

        observed_at = datetime.now(UTC).isoformat()
        candidate = not issues and pass_reads[0] is not None and pass_reads[1] is not None
        accepted_reads = deepcopy(pass_reads[0]) if candidate else {}
        read_digest = _digest(accepted_reads) if candidate else None
        issue_codes = sorted({item["code"] for item in issues})
        evidence = {
            "reads": deepcopy(accepted_reads),
            "read_digest": read_digest,
            "guard_before": deepcopy(all_guards[0]) if all_guards else None,
            "guard_after": deepcopy(all_guards[-1]) if all_guards else None,
            "passes": passes,
            "issues": issues,
            "request_count": requests,
            "response_bytes": bytes_seen,
            "attempted_response_bytes": attempted_response_bytes,
            "response_byte_budget_exhausted": byte_budget_exhausted,
            "consistency_note": _CONSISTENCY_NOTE,
        }
        return {
            "status": "candidate" if candidate else "inconclusive",
            "records": accepted_reads,
            "evidence": evidence,
            "issue_codes": issue_codes,
            "source_binding": self._binding_dict(),
            "observed_at": observed_at,
            "synthetic_only": True,
            "preparation_only": True,
        }


__all__ = [
    "ExportReaderError",
    "LocalExportReader",
    "ReadRequest",
    "ReaderBinding",
    "SyntheticGetTransport",
]
