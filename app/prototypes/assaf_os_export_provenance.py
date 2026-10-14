"""Pure provenance mapper for the fixture-only Assaf OS export experiment.

The mapper consumes only the frozen GET reader result.  It never calls Google,
imports a loader, or writes to a database.  It accepts either the invented
normalised fixture projection or physical rows matching the frozen local
``schema.js`` descriptor.  Both remain preparation fixtures: neither proves a
live workbook inspection, source authorization, or production cutover.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.prototypes.assaf_os_domain import (
    ENTITIES,
    ID_KEYS,
    SCHEMA,
    canonical_input,
    lead_contact_dependencies,
    validate_state,
)
from app.prototypes.assaf_os_primary import LocalPrimaryError
from app.prototypes.assaf_os_summaries import SummaryInputError, build_summaries

MAX_RECORDS = 2_000
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 100
MAX_JSON_NODES = 100_000
MAX_SAFE_INTEGER = 9_007_199_254_740_991
NORMALISED_SOURCE_FORMAT = "invented_fixture_normalised_real_source.v1"
PHYSICAL_SOURCE_FORMAT = "fixture_physical_assaf_os_schema.v1"
PHYSICAL_COMPATIBILITY = "frozen_descriptor_fixture_only_requires_live_header_inspection"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$")
_A1_RE = re.compile(
    r"^(?P<sheet>(?:'(?:[^']|'')+'|[A-Za-z][A-Za-z0-9_-]{0,99}))!"
    r"(?P<start_col>[A-Z]{1,3})(?P<start_row>[1-9][0-9]{0,6}):"
    r"(?P<end_col>[A-Z]{1,3})(?P<end_row>[1-9][0-9]{0,6})$"
)
_DISPLAY_REFERENCE_RE = re.compile(r" ⟦([^⟦⟧]+)⟧$")

_PHYSICAL_DESCRIPTOR_PATH = Path(__file__).with_name("assaf_os_export_schema.json")
with _PHYSICAL_DESCRIPTOR_PATH.open("r", encoding="utf-8") as _descriptor_file:
    _PHYSICAL_DESCRIPTOR = json.load(_descriptor_file)
if _PHYSICAL_DESCRIPTOR.get("version") != SCHEMA["version"] or set(
    _PHYSICAL_DESCRIPTOR.get("entities", {})
) != set(ENTITIES):
    raise RuntimeError("invalid_frozen_export_schema_descriptor")


@dataclass(frozen=True)
class SourceBinding:
    """Frozen subset matching the fixture GET reader's ReaderBinding."""

    workbook_id: str
    workspace_id: str
    authority_epoch: int
    source_epoch: int
    schema_version: str
    allowed_ranges: tuple[str, ...]
    guard_range: str


def _entity_headers(entity: str) -> tuple[str, ...]:
    prefix = [ID_KEYS[entity]]
    if entity == "leads":
        prefix.append("person_id")
    return tuple(prefix + [field["key"] for field in SCHEMA["entities"][entity]] + ["revision"])


ENTITY_HEADERS = {entity: _entity_headers(entity) for entity in ENTITIES}
ENTITY_ROLES = {
    entity: tuple(
        "input" if key in {field["key"] for field in SCHEMA["entities"][entity]} else "system"
        for key in ENTITY_HEADERS[entity]
    )
    for entity in ENTITIES
}
NATIVE_FIELDS = {
    entity: tuple(_PHYSICAL_DESCRIPTOR["entities"][entity]["fields"]) for entity in ENTITIES
}
NATIVE_ENTITY_HEADERS = {
    entity: tuple(field["label"] for field in NATIVE_FIELDS[entity]) for entity in ENTITIES
}
NATIVE_ENTITY_ROLES = {
    entity: tuple(field["role"] for field in NATIVE_FIELDS[entity]) for entity in ENTITIES
}
MONEY_FIELDS = {
    entity: frozenset(
        field["key"] for field in SCHEMA["entities"][entity] if field["type"] == "money_ils"
    )
    for entity in ENTITIES
}
REF_FIELDS = {
    entity: frozenset(field["key"] for field in SCHEMA["entities"][entity] if field.get("ref"))
    for entity in ENTITIES
}

# These three headers intentionally match the native UnifiedRuntime.gs journals.
ACCEPTED_HEADERS = (
    "entity",
    "record_id",
    "revision",
    "input_fingerprint",
    "raw_fingerprint",
    "snapshot_json",
    "accepted_at",
    "source",
    "request_id",
)
PROPOSAL_HEADERS = (
    "proposal_id",
    "request_id",
    "owner_id",
    "actor_source",
    "status",
    "entity",
    "record_id",
    "expected_revision",
    "target_fingerprint",
    "proposal_json",
    "created_at",
    "expires_at",
    "decided_at",
)
RECEIPT_HEADERS = (
    "request_id",
    "owner_id",
    "proposal_id",
    "status",
    "receipt_json",
    "committed_at",
)

# The following are fixture evidence ranges.  They are not asserted to exist in
# the live workbook and cannot be replaced by caller-side claims.
SOURCE_METADATA_HEADERS = (
    "range",
    "entity",
    "source_id",
    "workbook_id",
    "workspace_id",
    "authority_epoch",
    "source_epoch",
    "schema_version",
    "money_unit",
    "raw_fingerprint_contract",
    "unformatted_hash",
    "formula_hash",
    "observed_at",
    "format",
)
COVERAGE_HEADERS = (
    "entity",
    "records",
    "accepted_history",
    "receipt_history",
    "proposal_history",
    "header_history",
)
HEADER_HISTORY_HEADERS = (
    "entity",
    "schema_version",
    "headers_json",
    "roles_json",
    "observed_at",
)


def canonical_digest(value: Any) -> str:
    """Digest compatible with stable, sorted, UTF-8 JSON fixture evidence."""

    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _js_stable(value: Any) -> str:
    """Encode the bounded scalar/list/dict subset like runtime.js stableStringify."""

    if isinstance(value, list):
        return "[" + ",".join(_js_stable(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                json.dumps(key, ensure_ascii=False, separators=(",", ":"))
                + ":"
                + _js_stable(value[key])
                for key in sorted(value)
            )
            + "}"
        )
    if type(value) is float and value.is_integer():
        value = int(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _js_digest(value: Any) -> str:
    return hashlib.sha256(_js_stable(value).encode("utf-8")).hexdigest()


def _canonical_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _safe_int(value: Any, *, positive: bool = False) -> bool:
    return type(value) is int and abs(value) <= MAX_SAFE_INTEGER and (not positive or value > 0)


def _validate_json(value: Any) -> None:
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise LocalPrimaryError("export_json_too_complex")
        if item is None or type(item) in {bool, int}:
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise LocalPrimaryError("export_not_json")
            continue
        if type(item) is str:
            if "\x00" in item:
                raise LocalPrimaryError("export_not_json")
            continue
        if type(item) is list:
            pending.extend((child, depth + 1) for child in item)
            continue
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str or "\x00" in key:
                    raise LocalPrimaryError("export_not_json")
                pending.append((child, depth + 1))
            continue
        raise LocalPrimaryError("export_not_json")


def _validate_payload(extract: Any, evidence: Any) -> None:
    _validate_json(extract)
    _validate_json(evidence)
    try:
        size = len(
            json.dumps(
                {"extract": extract, "evidence": evidence},
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise LocalPrimaryError("export_not_json") from exc
    if size > MAX_PAYLOAD_BYTES:
        raise LocalPrimaryError("export_payload_too_large")


def _binding_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    elif isinstance(value, Mapping):
        value = dict(value)
    else:
        names = SourceBinding.__dataclass_fields__
        try:
            value = {name: getattr(value, name) for name in names}
        except AttributeError as exc:
            raise LocalPrimaryError("invalid_expected_binding") from exc
    expected = set(SourceBinding.__dataclass_fields__)
    if set(value) != expected:
        raise LocalPrimaryError("invalid_expected_binding")
    ranges = value["allowed_ranges"]
    if not isinstance(ranges, (list, tuple)) or any(type(item) is not str for item in ranges):
        raise LocalPrimaryError("invalid_expected_binding")
    result = dict(value)
    result["allowed_ranges"] = list(ranges)
    range_bounds = [_range_bounds(item) for item in result["allowed_ranges"]]
    guard_bounds = _range_bounds(result["guard_range"])
    if (
        any(
            type(result[key]) is not str or not result[key]
            for key in ("workbook_id", "workspace_id", "schema_version", "guard_range")
        )
        or not _safe_int(result["authority_epoch"], positive=True)
        or not _safe_int(result["source_epoch"], positive=True)
        or not result["allowed_ranges"]
        or len(set(result["allowed_ranges"])) != len(result["allowed_ranges"])
        or result["guard_range"] in result["allowed_ranges"]
        or any(item is None for item in range_bounds)
        or guard_bounds is None
    ):
        raise LocalPrimaryError("invalid_expected_binding")
    data_bounds = [item for item in range_bounds if item is not None]
    assert guard_bounds is not None
    if (
        len(data_bounds) > 49
        or any(rows > 200 or columns > 64 for _, rows, columns in data_bounds)
        or sum(rows for _, rows, _ in data_bounds) > MAX_RECORDS
        or guard_bounds[1] > 2
        or not 6 <= guard_bounds[2] <= 8
    ):
        raise LocalPrimaryError("invalid_expected_binding")
    return result


def _parse_utc(value: Any) -> str | None:
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset().total_seconds() != 0
    ):
        return None
    return parsed.isoformat()


def _issue(
    issues: list[dict[str, Any]],
    reason_code: str,
    *,
    entity: str | None = None,
    record_id: Any = None,
    source_range: str | None = None,
    row_number: int | None = None,
    detail: Mapping[str, Any] | None = None,
) -> None:
    issues.append(
        {
            "reason_code": reason_code,
            "entity": entity,
            "record_id": record_id if isinstance(record_id, str) else None,
            "source_range": source_range,
            "row_number": row_number,
            "detail": dict(detail or {}),
        }
    )


def _base_manifest(
    status: str,
    binding: Mapping[str, Any],
    observed_at: Any,
    issues: Sequence[Mapping[str, Any]],
    *,
    reader_evidence: Any = None,
) -> dict[str, Any]:
    result = {
        "status": status,
        "synthetic_only": True,
        "source_format": NORMALISED_SOURCE_FORMAT,
        "physical_schema_compatibility": PHYSICAL_COMPATIBILITY,
        "source_binding": _copy(binding),
        "observed_at": observed_at if _parse_utc(observed_at) else None,
        "issue_count": len(issues),
        "issue_codes": sorted({str(item["reason_code"]) for item in issues}),
        "issues": _copy(list(issues)),
        "reader_evidence": _copy(reader_evidence),
        "writes_performed": 0,
        "importer_invoked": False,
        "business_ledger_invoked": False,
        "cutover_proof": False,
        "source_authorization_proof": False,
    }
    result["manifest_digest"] = canonical_digest(result)
    return result


def _column_number(value: str) -> int:
    result = 0
    for character in value:
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _range_bounds(value: Any) -> tuple[int, int, int] | None:
    if type(value) is not str or len(value) > 256:
        return None
    match = _A1_RE.fullmatch(value)
    if match is None:
        return None
    start_column = _column_number(match.group("start_col"))
    end_column = _column_number(match.group("end_col"))
    start_row = int(match.group("start_row"))
    end_row = int(match.group("end_row"))
    if start_column > end_column or start_row > end_row or end_column > 64:
        return None
    return start_row, end_row - start_row + 1, end_column - start_column + 1


def _envelope(value: Any, source_range: str) -> tuple[list[list[Any]], str] | None:
    if not isinstance(value, dict) or set(value) != {"range", "majorDimension", "values"}:
        return None
    if value.get("range") != source_range or value.get("majorDimension") != "ROWS":
        return None
    bounds = _range_bounds(source_range)
    rows = value.get("values")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, list) for row in rows):
        return None
    if bounds is None or len(rows) > bounds[1] or any(len(row) > bounds[2] for row in rows):
        return None
    if any(
        type(cell) not in {str, bool, int, float}
        or (type(cell) is int and not _safe_int(cell))
        or (type(cell) is float and not math.isfinite(cell))
        for row in rows
        for cell in row
    ):
        return None
    return rows, canonical_digest(value)


def _reader_integrity(
    extract: Mapping[str, Any], evidence: Any, expected: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    keys = {
        "status",
        "records",
        "evidence",
        "issue_codes",
        "source_binding",
        "observed_at",
        "synthetic_only",
        "preparation_only",
    }
    if set(extract) != keys:
        _issue(issues, "reader_extract_shape_invalid")
        return None, issues
    if extract.get("source_binding") != expected or canonical_digest(
        extract.get("source_binding")
    ) != canonical_digest(expected):
        _issue(issues, "source_binding_changed")
    if extract.get("evidence") != evidence:
        _issue(issues, "evidence_not_reader_bound")
    if extract.get("status") != "candidate":
        _issue(issues, "reader_inconclusive")
    if extract.get("synthetic_only") is not True:
        _issue(issues, "synthetic_fixture_marker_required")
    if extract.get("preparation_only") is not True:
        _issue(issues, "preparation_only_marker_required")
    if _parse_utc(extract.get("observed_at")) is None:
        _issue(issues, "invalid_observed_at")
    if not isinstance(evidence, dict):
        _issue(issues, "reader_evidence_shape_invalid")
        return None, issues
    evidence_keys = {
        "reads",
        "read_digest",
        "guard_before",
        "guard_after",
        "passes",
        "issues",
        "request_count",
        "response_bytes",
        "attempted_response_bytes",
        "response_byte_budget_exhausted",
        "consistency_note",
    }
    if set(evidence) != evidence_keys:
        _issue(issues, "reader_evidence_shape_invalid")
        return None, issues
    records = extract.get("records")
    if not isinstance(records, dict) or records != evidence.get("reads"):
        _issue(issues, "reader_records_evidence_mismatch")
        return None, issues
    if set(records) != set(expected["allowed_ranges"]):
        _issue(issues, "covered_ranges_changed")
    digest = evidence.get("read_digest")
    if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        _issue(issues, "expected_source_hash_missing")
    elif digest != canonical_digest(records):
        _issue(issues, "reader_read_digest_changed")
    if extract.get("issue_codes") != [] or evidence.get("issues") != []:
        _issue(issues, "reader_reported_issues")
    if (
        evidence.get("consistency_note")
        != "matching_synthetic_reads_require_approved_source_freeze"
    ):
        _issue(issues, "reader_consistency_note_changed")
    expected_requests = 2 * (2 + 2 * len(expected["allowed_ranges"]))
    if evidence.get("request_count") != expected_requests:
        _issue(issues, "reader_request_count_changed")
    response_bytes = evidence.get("response_bytes")
    attempted_bytes = evidence.get("attempted_response_bytes")
    passes = evidence.get("passes")
    expected_response_bytes = None
    if isinstance(passes, list) and len(passes) == 2:
        try:
            expected_response_bytes = sum(
                _canonical_size(item[key])
                for item in passes
                for key in ("guard_before", "guard_after")
            ) + 2 * sum(
                _canonical_size(pair[render])
                for pair in records.values()
                for render in ("unformatted", "formulas")
            )
        except (TypeError, ValueError, AttributeError, KeyError):
            expected_response_bytes = None
    if (
        not _safe_int(response_bytes)
        or response_bytes < 0
        or response_bytes > MAX_PAYLOAD_BYTES
        or attempted_bytes != response_bytes
        or response_bytes != expected_response_bytes
        or evidence.get("response_byte_budget_exhausted") is not False
    ):
        _issue(issues, "reader_response_budget_invalid")

    def guard_valid(value: Any) -> bool:
        parsed = _envelope(value, expected["guard_range"])
        if parsed is None:
            return False
        rows, _ = parsed
        if len(rows) != 2 or len(rows[0]) != 6 or len(rows[1]) != 6:
            return False
        expected_header = [
            "workspace_id",
            "authority_epoch",
            "source_epoch",
            "schema_version",
            "revision",
            "export_token",
        ]
        expected_binding_values = [
            expected["workspace_id"],
            expected["authority_epoch"],
            expected["source_epoch"],
            expected["schema_version"],
        ]
        return (
            rows[0] == expected_header
            and all(
                type(actual) is type(wanted) and actual == wanted
                for actual, wanted in zip(rows[1][:4], expected_binding_values, strict=True)
            )
            and _safe_int(rows[1][4], positive=True)
            and type(rows[1][5]) is str
            and 1 <= len(rows[1][5]) <= 256
        )

    if not guard_valid(evidence.get("guard_before")) or not guard_valid(
        evidence.get("guard_after")
    ):
        _issue(issues, "reader_guard_invalid")
    if not isinstance(passes, list) or len(passes) != 2:
        _issue(issues, "reader_pass_evidence_invalid")
    else:
        for number, item in enumerate(passes, start=1):
            if (
                not isinstance(item, dict)
                or set(item) != {"pass_number", "read_digest", "guard_before", "guard_after"}
                or not _safe_int(item.get("pass_number"), positive=True)
                or item.get("pass_number") != number
                or item.get("read_digest") != digest
                or not guard_valid(item.get("guard_before"))
                or not guard_valid(item.get("guard_after"))
            ):
                _issue(issues, "reader_passes_changed")
                break
        else:
            guards = [
                passes[0]["guard_before"],
                passes[0]["guard_after"],
                passes[1]["guard_before"],
                passes[1]["guard_after"],
            ]
            if any(item != guards[0] for item in guards[1:]):
                _issue(issues, "reader_guard_changed")
            if (
                evidence.get("guard_before") != guards[0]
                or evidence.get("guard_after") != guards[-1]
            ):
                _issue(issues, "reader_guard_summary_changed")
    if evidence.get("guard_before") != evidence.get("guard_after"):
        _issue(issues, "reader_guard_changed")
    if issues:
        return None, issues
    return records, issues


def _json_cell(value: Any) -> Any:
    if type(value) is not str:
        raise ValueError("json_cell_not_string")
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("json_cell_invalid") from exc
    _validate_json(parsed)
    return parsed


def _row_dict(headers: Sequence[str], row: Sequence[Any]) -> dict[str, Any]:
    padded = list(row) + [""] * (len(headers) - len(row))
    return dict(zip(headers, padded, strict=True))


def _sheet_row_number(source_range: str, matrix_offset: int) -> int:
    bounds = _range_bounds(source_range)
    if bounds is None:
        raise LocalPrimaryError("invalid_source_range")
    return bounds[0] + matrix_offset - 1


def _major_to_minor(value: Any) -> int | None:
    if value in ("", None):
        return None
    if type(value) not in {int, float, str} or type(value) is bool:
        raise ValueError("invalid_major_amount")
    text = str(value)
    if _DECIMAL_RE.fullmatch(text) is None:
        raise ValueError("invalid_major_amount")
    try:
        scaled = Decimal(text) * 100
    except InvalidOperation as exc:
        raise ValueError("invalid_major_amount") from exc
    if scaled != scaled.to_integral_value():
        raise ValueError("money_precision_exceeded")
    result = int(scaled)
    if not _safe_int(result) or result < 0:
        raise ValueError("invalid_major_amount")
    return result


def _resolve_native_reference(value: Any, target_ids: frozenset[str]) -> Any:
    if value in ("", None):
        return None
    if not isinstance(value, str):
        raise LocalPrimaryError("invalid_native_reference")
    match = _DISPLAY_REFERENCE_RE.search(value)
    identity = match.group(1) if match else value
    if identity not in target_ids:
        raise LocalPrimaryError("unknown_native_reference")
    return identity


def _native_keyed_row(entity: str, row: Sequence[Any]) -> dict[str, Any]:
    fields = NATIVE_FIELDS[entity]
    padded = list(row) + [""] * (len(fields) - len(row))
    return {field["key"]: padded[index] for index, field in enumerate(fields)}


def native_raw_owned_digest(
    entity: str,
    row: Sequence[Any],
    reference_ids: Mapping[str, frozenset[str] | set[str]],
) -> str:
    """Reproduce ``assafOsRawOwnedFingerprint_`` for a frozen physical row."""

    fields = NATIVE_FIELDS[entity]
    padded = list(row) + [""] * (len(fields) - len(row))
    owned: list[Any] = []
    for index, field in enumerate(fields):
        value = None if field["role"] == "calculated" else padded[index]
        if value not in (None, "") and field.get("ref"):
            target = str(field["ref"]).split(".", 1)[0]
            value = _resolve_native_reference(value, frozenset(reference_ids.get(target, ())))
        owned.append(value)
    return _js_digest(owned)


def _normalise_record(
    entity: str,
    raw: Mapping[str, Any],
    money_unit: str,
    *,
    reference_ids: Mapping[str, frozenset[str]] | None = None,
) -> dict[str, Any]:
    input_raw: dict[str, Any] = {}
    for field in SCHEMA["entities"][entity]:
        key = field["key"]
        value = raw.get(key, "")
        if reference_ids is not None and key in REF_FIELDS[entity]:
            target = str(field["ref"]).split(".", 1)[0]
            value = _resolve_native_reference(value, reference_ids[target])
        if key in MONEY_FIELDS[entity]:
            if money_unit == "canonical_minor":
                if value in ("", None):
                    value = None
                elif type(value) is not int or type(value) is bool:
                    raise LocalPrimaryError("unsafe_money_unit_value")
            elif money_unit == "native_major_decimal":
                try:
                    value = _major_to_minor(value)
                except ValueError as exc:
                    raise LocalPrimaryError(str(exc)) from exc
            else:
                raise LocalPrimaryError("unsupported_money_unit")
        if value == "":
            value = None
        input_raw[key] = value
    canonical = canonical_input(entity, input_raw, creating=True)
    record: dict[str, Any] = {ID_KEYS[entity]: raw.get(ID_KEYS[entity])}
    if entity == "leads":
        record["person_id"] = raw.get("person_id")
    record.update(canonical)
    record["revision"] = raw.get("revision")
    return record


def record_input_digest(entity: str, record: Mapping[str, Any]) -> str:
    """Digest the exact native acceptance input shape, including optional nulls."""

    value = {field["key"]: record.get(field["key"]) for field in SCHEMA["entities"][entity]}
    return canonical_digest(value)


def _money(records: Mapping[str, Sequence[Mapping[str, Any]]]) -> tuple[dict[str, Any], list[dict]]:
    payment_received, payment_refund = SCHEMA["enums"]["payment_kind"][:2]
    paid_status = SCHEMA["enums"]["payment_status"][0]
    actual_source = SCHEMA["enums"]["expense_source"][0]
    result: dict[str, dict[str, int]] = {}
    refunds: list[dict] = []

    def bucket(currency: Any) -> dict[str, int]:
        return result.setdefault(
            str(currency),
            {
                "received_minor": 0,
                "refund_minor": 0,
                "net_payments_minor": 0,
                "paid_expenses_minor": 0,
                "net_cash_minor": 0,
            },
        )

    for row in records["payments"]:
        target = bucket(row.get("currency"))
        amount = row["actual_amount_ils"]
        if row.get("kind") == payment_received:
            target["received_minor"] += amount
            target["net_payments_minor"] += amount
        elif row.get("kind") == payment_refund:
            target["refund_minor"] += amount
            target["net_payments_minor"] -= amount
            refunds.append(
                {
                    "payment_id": row.get("payment_id"),
                    "original_payment_id": row.get("original_payment_id"),
                    "amount_minor": amount,
                    "currency": row.get("currency"),
                }
            )
    for row in records["expenses"]:
        if row.get("payment_status") == paid_status and row.get("source_type") == actual_source:
            bucket(row.get("currency"))["paid_expenses_minor"] += row["amount_ils"]
    for values in result.values():
        values["net_cash_minor"] = values["net_payments_minor"] - values["paid_expenses_minor"]
    refunds.sort(key=lambda item: str(item["payment_id"]))
    return {key: result[key] for key in sorted(result)}, refunds


def _references(records: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    indexes = {
        entity: {
            row.get(ID_KEYS[entity]) for row in rows if isinstance(row.get(ID_KEYS[entity]), str)
        }
        for entity, rows in records.items()
    }
    items: list[dict[str, Any]] = []

    def add(
        entity: str, row: Mapping[str, Any], field: str, target: str, required: bool = True
    ) -> None:
        value = row.get(field)
        if value in (None, "") and not required:
            return
        items.append(
            {
                "from_entity": entity,
                "from_id": row.get(ID_KEYS[entity]),
                "field": field,
                "to_entity": target,
                "to_id": value,
                "required": required,
                "resolved": isinstance(value, str) and value in indexes[target],
            }
        )

    for row in records["leads"]:
        add(
            "leads",
            row,
            "person_id",
            "clients",
            required=row.get("status") == SCHEMA["enums"]["lead_status"][-1],
        )
    for entity in ("works", "payments", "time", "tasks"):
        for row in records[entity]:
            add(
                entity,
                row,
                "person_id",
                "clients",
                required=entity != "tasks" or bool(row.get("person_id")),
            )
            if entity != "works":
                add(
                    entity,
                    row,
                    "work_id",
                    "works",
                    required=entity != "tasks" or bool(row.get("work_id")),
                )
    for row in records["payments"]:
        if row.get("original_payment_id"):
            add("payments", row, "original_payment_id", "payments")
    items.sort(key=lambda item: (item["from_entity"], str(item["from_id"]), item["field"]))
    return {
        "total": len(items),
        "unresolved": sum(item["required"] and not item["resolved"] for item in items),
        "items": items,
    }


def map_export(extract: Any, evidence: Any, *, expected_binding: Any) -> dict[str, Any]:
    """Map a stable reader candidate into a dry, evidence-rich manifest.

    Invalid reader/binding/source-fingerprint evidence is ``inconclusive``.
    Source rows with missing identity, acceptance, coverage, or domain parity are
    ``quarantined``.  No status grants source authorization or human approval.
    """

    _validate_payload(extract, evidence)
    binding = _binding_dict(expected_binding)
    if not isinstance(extract, dict):
        raise LocalPrimaryError("invalid_export_shape")
    records_by_range, integrity_issues = _reader_integrity(extract, evidence, binding)
    if records_by_range is None:
        return _base_manifest(
            "inconclusive",
            binding,
            extract.get("observed_at"),
            integrity_issues,
            reader_evidence=evidence,
        )

    issues: list[dict[str, Any]] = []
    decoded: dict[str, dict[str, Any]] = {}
    classified: dict[str, list[str]] = {entity: [] for entity in ENTITIES}
    special: dict[str, str] = {}
    source_evidence: dict[str, Any] = {}
    signatures = {
        ACCEPTED_HEADERS: "accepted",
        PROPOSAL_HEADERS: "proposals",
        RECEIPT_HEADERS: "receipts",
        SOURCE_METADATA_HEADERS: "source_metadata",
        COVERAGE_HEADERS: "coverage",
        HEADER_HISTORY_HEADERS: "header_history",
    }
    entity_signatures = {
        headers: (entity, "normalised") for entity, headers in ENTITY_HEADERS.items()
    }
    for entity, headers in NATIVE_ENTITY_HEADERS.items():
        entity_signatures[headers] = (entity, "physical")
    for source_range in binding["allowed_ranges"]:
        pair = records_by_range.get(source_range)
        if not isinstance(pair, dict) or set(pair) != {"unformatted", "formulas"}:
            _issue(issues, "range_read_shape_invalid", source_range=source_range)
            continue
        unformatted = _envelope(pair["unformatted"], source_range)
        formulas = _envelope(pair["formulas"], source_range)
        if unformatted is None or formulas is None:
            _issue(issues, "range_envelope_invalid", source_range=source_range)
            continue
        raw_rows, raw_hash = unformatted
        formula_rows, formula_hash = formulas
        if len(raw_rows) != len(formula_rows) or tuple(raw_rows[0]) != tuple(formula_rows[0]):
            _issue(issues, "formula_shape_changed", source_range=source_range)
            continue
        headers = tuple(raw_rows[0])
        if any(len(row) > len(headers) for row in raw_rows + formula_rows):
            _issue(issues, "range_row_wider_than_headers", source_range=source_range)
            continue
        non_formula_changed = False
        for raw_row, formula_row in zip(raw_rows, formula_rows, strict=True):
            raw_values = list(raw_row) + [""] * (len(headers) - len(raw_row))
            formula_values = list(formula_row) + [""] * (len(headers) - len(formula_row))
            for raw_value, formula_value in zip(raw_values, formula_values, strict=True):
                if (
                    not (isinstance(formula_value, str) and formula_value.startswith("="))
                    and formula_value != raw_value
                ):
                    non_formula_changed = True
                    break
        if non_formula_changed:
            _issue(issues, "formula_nonformula_value_changed", source_range=source_range)
            continue
        kind = signatures.get(headers)
        entity_signature = entity_signatures.get(headers)
        if kind is None and entity_signature is None:
            _issue(issues, "unrecognized_range_headers", source_range=source_range)
            continue
        entity = entity_signature[0] if entity_signature else None
        entity_mode = entity_signature[1] if entity_signature else None
        if entity is not None:
            classified[entity].append(source_range)
            kind = "entity"
        else:
            if kind in special:
                _issue(
                    issues,
                    "duplicate_evidence_range",
                    source_range=source_range,
                    detail={"kind": kind},
                )
            special[kind] = source_range
        decoded[source_range] = {
            "kind": kind,
            "entity": entity,
            "entity_mode": entity_mode,
            "headers": headers,
            "raw_rows": raw_rows,
            "formula_rows": formula_rows,
            "raw_hash": raw_hash,
            "formula_hash": formula_hash,
        }
        source_evidence[source_range] = {
            "kind": kind,
            "entity": entity,
            "unformatted_hash": raw_hash,
            "formula_hash": formula_hash,
            "unformatted_values": _copy(raw_rows),
            "formula_values": _copy(formula_rows),
        }
    for entity, ranges in classified.items():
        if not ranges:
            _issue(issues, "entity_range_missing", entity=entity)
    for kind in signatures.values():
        if kind not in special:
            _issue(issues, "evidence_range_missing", detail={"kind": kind})
    if any(
        item["reason_code"]
        in {
            "range_read_shape_invalid",
            "range_envelope_invalid",
            "formula_shape_changed",
            "formula_nonformula_value_changed",
            "range_row_wider_than_headers",
            "unrecognized_range_headers",
            "duplicate_evidence_range",
            "entity_range_missing",
            "evidence_range_missing",
        }
        for item in issues
    ):
        return _base_manifest(
            "inconclusive", binding, extract.get("observed_at"), issues, reader_evidence=evidence
        )

    def special_rows(name: str) -> list[dict[str, Any]]:
        item = decoded[special[name]]
        return [_row_dict(item["headers"], row) for row in item["raw_rows"][1:]]

    source_rows = special_rows("source_metadata")
    source_by_range: dict[str, dict[str, Any]] = {}
    source_ids: set[str] = set()
    for row in source_rows:
        source_range = row.get("range")
        if not isinstance(source_range, str) or source_range in source_by_range:
            _issue(issues, "source_metadata_duplicate_or_invalid")
            continue
        source_by_range[source_range] = row
        if isinstance(row.get("source_id"), str) and row["source_id"]:
            source_ids.add(row["source_id"])
    for entity, ranges in classified.items():
        for source_range in ranges:
            row = source_by_range.get(source_range)
            data = decoded[source_range]
            if row is None:
                _issue(issues, "source_metadata_missing", entity=entity, source_range=source_range)
                continue
            physical = data["entity_mode"] == "physical"
            expected_values = {
                "entity": entity,
                "workbook_id": binding["workbook_id"],
                "workspace_id": binding["workspace_id"],
                "authority_epoch": binding["authority_epoch"],
                "source_epoch": binding["source_epoch"],
                "schema_version": binding["schema_version"],
                "raw_fingerprint_contract": (
                    "apps_script_raw_owned_sha256" if physical else "normalized_row_sha256"
                ),
                "unformatted_hash": data["raw_hash"],
                "formula_hash": data["formula_hash"],
                "format": PHYSICAL_SOURCE_FORMAT if physical else NORMALISED_SOURCE_FORMAT,
            }
            unit = row.get("money_unit")
            if (physical and unit != "native_major_decimal") or (
                not physical and unit not in {"canonical_minor", "native_major_decimal"}
            ):
                _issue(issues, "unsupported_money_unit", entity=entity, source_range=source_range)
            if any(row.get(key) != value for key, value in expected_values.items()):
                _issue(issues, "source_metadata_changed", entity=entity, source_range=source_range)
            if row.get("observed_at") != extract.get("observed_at"):
                _issue(
                    issues, "invalid_source_observed_at", entity=entity, source_range=source_range
                )
    if set(source_by_range) != {item for ranges in classified.values() for item in ranges}:
        _issue(issues, "orphan_source_metadata")
    if len(source_ids) != 1:
        _issue(issues, "source_id_missing_or_changed")
    else:
        expected_source_id = next(iter(source_ids))
        for entity, ranges in classified.items():
            for source_range in ranges:
                if source_by_range.get(source_range, {}).get("source_id") != expected_source_id:
                    _issue(
                        issues,
                        "source_id_missing_or_changed",
                        entity=entity,
                        source_range=source_range,
                    )
        for item in source_evidence.values():
            item["source_id"] = expected_source_id

    coverage_rows = special_rows("coverage")
    coverage: dict[str, dict[str, str]] = {}
    for row in coverage_rows:
        entity = row.get("entity")
        if entity not in ENTITIES or entity in coverage:
            _issue(issues, "coverage_entity_duplicate_or_invalid")
            continue
        coverage[entity] = {key: row.get(key) for key in COVERAGE_HEADERS[1:]}
        for key, value in coverage[entity].items():
            if value not in {"complete", "partial", "unknown"}:
                _issue(issues, "coverage_value_invalid", entity=entity, detail={"field": key})
            elif value != "complete":
                _issue(
                    issues,
                    "coverage_incomplete",
                    entity=entity,
                    detail={"field": key, "value": value},
                )
    if set(coverage) != set(ENTITIES):
        _issue(issues, "coverage_entity_missing")

    header_rows = special_rows("header_history")
    header_history: dict[str, dict[str, Any]] = {}
    for row in header_rows:
        entity = row.get("entity")
        if entity not in ENTITIES or entity in header_history:
            _issue(issues, "header_history_entity_duplicate_or_invalid")
            continue
        try:
            headers = _json_cell(row.get("headers_json"))
            roles = _json_cell(row.get("roles_json"))
        except (ValueError, json.JSONDecodeError, LocalPrimaryError):
            _issue(issues, "header_history_invalid", entity=entity)
            continue
        header_history[entity] = _copy(row)
        entity_modes = {decoded[source_range]["entity_mode"] for source_range in classified[entity]}
        if len(entity_modes) != 1:
            _issue(issues, "mixed_entity_source_formats", entity=entity)
            continue
        physical = next(iter(entity_modes)) == "physical"
        expected_headers = NATIVE_ENTITY_HEADERS[entity] if physical else ENTITY_HEADERS[entity]
        expected_roles = NATIVE_ENTITY_ROLES[entity] if physical else ENTITY_ROLES[entity]
        if (
            row.get("schema_version") != binding["schema_version"]
            or headers != list(expected_headers)
            or roles != list(expected_roles)
            or _parse_utc(row.get("observed_at")) is None
        ):
            _issue(issues, "header_history_changed", entity=entity)
    if set(header_history) != set(ENTITIES):
        _issue(issues, "header_history_entity_missing")

    accepted_rows = special_rows("accepted")
    accepted_index: dict[tuple[str, str], dict[str, Any]] = {}
    accepted_revisions: dict[tuple[str, str, int], dict[str, Any]] = {}
    accepted_history: dict[str, list[dict[str, Any]]] = {}
    for row_number, row in enumerate(accepted_rows, start=2):
        entity, record_id, revision = row.get("entity"), row.get("record_id"), row.get("revision")
        key = (entity, record_id)
        if (
            entity not in ENTITIES
            or not isinstance(record_id, str)
            or not _safe_int(revision, positive=True)
        ):
            _issue(
                issues,
                "accepted_row_invalid",
                source_range=special["accepted"],
                row_number=row_number,
            )
            continue
        if (
            _DIGEST_RE.fullmatch(str(row.get("input_fingerprint"))) is None
            or _DIGEST_RE.fullmatch(str(row.get("raw_fingerprint"))) is None
        ):
            _issue(
                issues,
                "accepted_digest_invalid",
                entity=entity,
                record_id=record_id,
                row_number=row_number,
            )
            continue
        try:
            snapshot = _json_cell(row.get("snapshot_json"))
        except (ValueError, json.JSONDecodeError, LocalPrimaryError):
            _issue(
                issues,
                "accepted_snapshot_invalid",
                entity=entity,
                record_id=record_id,
                row_number=row_number,
            )
            continue
        snapshot_input = snapshot.get("input") if isinstance(snapshot, dict) else None
        canonical_snapshot_input = None
        if isinstance(snapshot_input, dict):
            try:
                canonical_snapshot_input = canonical_input(entity, snapshot_input, creating=True)
            except LocalPrimaryError:
                canonical_snapshot_input = None
        expected_input_keys = {field["key"] for field in SCHEMA["entities"][entity]}
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != {"entity", "record_id", "revision", "input"}
            or snapshot.get("entity") != entity
            or snapshot.get("record_id") != record_id
            or snapshot.get("revision") != revision
            or not isinstance(snapshot.get("input"), dict)
            or set(snapshot["input"]) != expected_input_keys
            or canonical_snapshot_input != snapshot["input"]
            or canonical_digest(snapshot.get("input")) != row.get("input_fingerprint")
            or _parse_utc(row.get("accepted_at")) is None
        ):
            _issue(
                issues,
                "accepted_snapshot_mismatch",
                entity=entity,
                record_id=record_id,
                row_number=row_number,
            )
            continue
        if (
            not isinstance(row.get("source"), str)
            or not 1 <= len(row["source"]) <= 64
            or not isinstance(row.get("request_id"), str)
            or not 1 <= len(row["request_id"]) <= 256
            or datetime.fromisoformat(row["accepted_at"])
            > datetime.fromisoformat(extract["observed_at"])
        ):
            _issue(
                issues,
                "accepted_audit_invalid",
                entity=entity,
                record_id=record_id,
                row_number=row_number,
            )
            continue
        preserved = {**_copy(row), "snapshot": snapshot, "row_number": row_number}
        revision_key = (entity, record_id, revision)
        prior_revision = accepted_revisions.get(revision_key)
        if prior_revision is not None:
            comparable = {key: value for key, value in preserved.items() if key != "row_number"}
            prior_comparable = {
                key: value for key, value in prior_revision.items() if key != "row_number"
            }
            _issue(
                issues,
                "duplicate_accepted_revision"
                if comparable == prior_comparable
                else "accepted_revision_ambiguous",
                entity=entity,
                record_id=record_id,
            )
        else:
            accepted_revisions[revision_key] = preserved
        accepted_history.setdefault(entity + ":" + record_id, []).append(preserved)
        existing = accepted_index.get(key)
        if existing is None or revision > existing["revision"]:
            accepted_index[key] = preserved
        elif revision == existing["revision"] and preserved != existing:
            _issue(issues, "accepted_revision_ambiguous", entity=entity, record_id=record_id)
    for entity_record, history in accepted_history.items():
        revisions = sorted({item["revision"] for item in history})
        complete_history = (
            bool(revisions)
            and revisions[0] == 1
            and all(
                current == previous + 1
                for previous, current in zip(revisions, revisions[1:], strict=False)
            )
        )
        if not complete_history:
            entity, record_id = entity_record.split(":", 1)
            _issue(issues, "accepted_history_gap", entity=entity, record_id=record_id)

    proposal_rows = special_rows("proposals")
    receipt_rows = special_rows("receipts")
    for kind in (
        "accepted",
        "proposals",
        "receipts",
        "source_metadata",
        "coverage",
        "header_history",
    ):
        item = decoded[special[kind]]
        for row_number, row in enumerate(item["formula_rows"][1:], start=2):
            if any(isinstance(value, str) and value.startswith("=") for value in row):
                _issue(
                    issues,
                    "formula_in_evidence_metadata",
                    source_range=special[kind],
                    row_number=row_number,
                )
    proposals_by_id: dict[str, dict[str, Any]] = {}
    receipts_by_request: dict[str, list[dict[str, Any]]] = {}
    observed = datetime.fromisoformat(extract["observed_at"])
    for row_number, row in enumerate(proposal_rows, start=2):
        try:
            proposal_json = _json_cell(row.get("proposal_json"))
        except (ValueError, LocalPrimaryError):
            proposal_json = None
        created = _parse_utc(row.get("created_at"))
        expires = _parse_utc(row.get("expires_at"))
        decided = _parse_utc(row.get("decided_at")) if row.get("decided_at") else None
        valid = (
            isinstance(proposal_json, dict)
            and all(
                isinstance(row.get(key), str) and 1 <= len(row[key]) <= 256
                for key in ("proposal_id", "request_id", "owner_id", "actor_source", "status")
            )
            and row.get("entity") in {*ENTITIES, "promote_lead"}
            and isinstance(row.get("record_id"), str)
            and bool(row["record_id"])
            and _safe_int(row.get("expected_revision"))
            and isinstance(row.get("target_fingerprint"), str)
            and _DIGEST_RE.fullmatch(row["target_fingerprint"]) is not None
            and created is not None
            and expires is not None
            and datetime.fromisoformat(created) <= observed
            and datetime.fromisoformat(expires) > datetime.fromisoformat(created)
            and (decided is None or datetime.fromisoformat(decided) <= observed)
        )
        if not valid:
            _issue(
                issues,
                "proposal_history_invalid",
                source_range=special["proposals"],
                row_number=row_number,
            )
            continue
        preserved = {**_copy(row), "proposal": proposal_json, "row_number": row_number}
        if row["proposal_id"] in proposals_by_id:
            _issue(
                issues,
                "proposal_history_ambiguous",
                source_range=special["proposals"],
                row_number=row_number,
            )
        else:
            proposals_by_id[row["proposal_id"]] = preserved
    for row_number, row in enumerate(receipt_rows, start=2):
        try:
            receipt_json = _json_cell(row.get("receipt_json"))
        except (ValueError, LocalPrimaryError):
            receipt_json = None
        committed = _parse_utc(row.get("committed_at"))
        valid = (
            isinstance(receipt_json, dict)
            and all(
                isinstance(row.get(key), str) and 1 <= len(row[key]) <= 256
                for key in ("request_id", "owner_id", "proposal_id", "status")
            )
            and committed is not None
            and datetime.fromisoformat(committed) <= observed
        )
        if not valid:
            _issue(
                issues,
                "receipt_history_invalid",
                source_range=special["receipts"],
                row_number=row_number,
            )
            continue
        preserved = {**_copy(row), "receipt": receipt_json, "row_number": row_number}
        receipts_by_request.setdefault(row["request_id"], []).append(preserved)
        if row["proposal_id"] not in proposals_by_id:
            _issue(
                issues,
                "receipt_proposal_unresolved",
                source_range=special["receipts"],
                row_number=row_number,
            )

    canonical_records: dict[str, list[dict[str, Any]]] = {entity: [] for entity in ENTITIES}
    provenance: dict[str, dict[str, Any]] = {entity: {} for entity in ENTITIES}
    total_records = 0
    seen_ids: dict[str, set[str]] = {entity: set() for entity in ENTITIES}
    native_reference_ids: dict[str, frozenset[str]] = {}
    for target, target_ranges in classified.items():
        identities: set[str] = set()
        for target_range in target_ranges:
            target_item = decoded[target_range]
            for target_row in target_item["raw_rows"][1:]:
                if target_item["entity_mode"] == "physical":
                    identity = _native_keyed_row(target, target_row).get(ID_KEYS[target])
                else:
                    identity = _row_dict(target_item["headers"], target_row).get(ID_KEYS[target])
                if isinstance(identity, str) and identity:
                    identities.add(identity)
        native_reference_ids[target] = frozenset(identities)
    for entity, ranges in classified.items():
        for source_range in ranges:
            item = decoded[source_range]
            unit = source_by_range.get(source_range, {}).get("money_unit")
            physical = item["entity_mode"] == "physical"
            for offset, row in enumerate(item["raw_rows"][1:], start=2):
                if all(value == "" for value in row):
                    continue
                total_records += 1
                if total_records > MAX_RECORDS:
                    raise LocalPrimaryError("export_record_limit")
                formula_row = item["formula_rows"][offset - 1]
                if physical:
                    raw = _native_keyed_row(entity, row)
                    formula = _native_keyed_row(entity, formula_row)
                    authoritative_keys = tuple(
                        field["key"]
                        for field in NATIVE_FIELDS[entity]
                        if field["role"] != "calculated"
                    )
                else:
                    raw = _row_dict(item["headers"], row)
                    formula = _row_dict(item["headers"], formula_row)
                    authoritative_keys = ENTITY_HEADERS[entity]
                record_id = raw.get(ID_KEYS[entity])
                if not isinstance(record_id, str) or not record_id:
                    _issue(
                        issues,
                        "missing_or_invalid_record_id",
                        entity=entity,
                        source_range=source_range,
                        row_number=offset,
                    )
                    continue
                if record_id in seen_ids[entity]:
                    _issue(
                        issues,
                        "duplicate_record_id",
                        entity=entity,
                        record_id=record_id,
                        source_range=source_range,
                        row_number=offset,
                    )
                    continue
                seen_ids[entity].add(record_id)
                for key in authoritative_keys:
                    if isinstance(formula.get(key), str) and formula[key].startswith("="):
                        _issue(
                            issues,
                            "formula_in_authoritative_field",
                            entity=entity,
                            record_id=record_id,
                            source_range=source_range,
                            row_number=offset,
                            detail={"field": key},
                        )
                try:
                    record = _normalise_record(
                        entity,
                        raw,
                        str(unit),
                        reference_ids=native_reference_ids if physical else None,
                    )
                except LocalPrimaryError as exc:
                    _issue(
                        issues,
                        "record_invalid",
                        entity=entity,
                        record_id=record_id,
                        source_range=source_range,
                        row_number=offset,
                        detail={"domain_code": exc.code},
                    )
                    continue
                canonical_records[entity].append(record)
                acceptance = accepted_index.get((entity, record_id))
                input_digest = record_input_digest(entity, record)
                try:
                    raw_digest = (
                        native_raw_owned_digest(entity, row, native_reference_ids)
                        if physical
                        else canonical_digest([raw.get(key, "") for key in ENTITY_HEADERS[entity]])
                    )
                except LocalPrimaryError as exc:
                    _issue(
                        issues,
                        "accepted_raw_unresolved",
                        entity=entity,
                        record_id=record_id,
                        source_range=source_range,
                        row_number=offset,
                        detail={"domain_code": exc.code},
                    )
                    raw_digest = None
                if acceptance is None:
                    _issue(
                        issues,
                        "unaccepted_record",
                        entity=entity,
                        record_id=record_id,
                        source_range=source_range,
                        row_number=offset,
                    )
                else:
                    snapshot = acceptance["snapshot"]
                    if acceptance["revision"] != record.get("revision"):
                        _issue(
                            issues,
                            "accepted_revision_changed",
                            entity=entity,
                            record_id=record_id,
                            source_range=source_range,
                            row_number=offset,
                        )
                    if acceptance["input_fingerprint"] != input_digest or snapshot["input"] != {
                        field["key"]: record.get(field["key"])
                        for field in SCHEMA["entities"][entity]
                    }:
                        _issue(
                            issues,
                            "accepted_input_changed",
                            entity=entity,
                            record_id=record_id,
                            source_range=source_range,
                            row_number=offset,
                        )
                    if acceptance["raw_fingerprint"] != raw_digest:
                        _issue(
                            issues,
                            "accepted_raw_changed",
                            entity=entity,
                            record_id=record_id,
                            source_range=source_range,
                            row_number=offset,
                        )
                request_id = acceptance.get("request_id") if acceptance else None
                linked_receipts = receipts_by_request.get(request_id, [])
                linked_proposals = [
                    proposals_by_id[receipt["proposal_id"]]
                    for receipt in linked_receipts
                    if receipt["proposal_id"] in proposals_by_id
                ]
                for proposal in linked_proposals:
                    target_matches = (
                        proposal["entity"] == entity and proposal["record_id"] == record_id
                    )
                    if proposal["entity"] == "promote_lead":
                        after = proposal["proposal"].get("after")
                        lead = after.get("lead") if isinstance(after, dict) else None
                        client = after.get("client") if isinstance(after, dict) else None
                        target_matches = (
                            entity == "leads"
                            and proposal["record_id"] == record_id
                            and isinstance(lead, dict)
                            and lead.get("lead_id") == record_id
                        ) or (
                            entity == "clients"
                            and isinstance(client, dict)
                            and client.get("person_id") == record_id
                        )
                    if not target_matches:
                        _issue(
                            issues,
                            "accepted_proposal_target_mismatch",
                            entity=entity,
                            record_id=record_id,
                            source_range=source_range,
                            row_number=offset,
                        )
                source_row_number = _sheet_row_number(source_range, offset)
                provenance[entity][record_id] = {
                    "source_range": source_range,
                    "source_row_number": source_row_number,
                    "source_format": (
                        PHYSICAL_SOURCE_FORMAT if physical else NORMALISED_SOURCE_FORMAT
                    ),
                    "raw_fingerprint_contract": source_by_range.get(source_range, {}).get(
                        "raw_fingerprint_contract"
                    ),
                    "unformatted_source_hash": item["raw_hash"],
                    "formula_source_hash": item["formula_hash"],
                    "raw_cells": _copy(raw),
                    "formula_cells": _copy(formula),
                    "money_unit": unit,
                    "source_input_digest": input_digest,
                    "source_raw_digest": raw_digest,
                    "source_native_acceptance": _copy(acceptance),
                    "native_acceptance_is_human_approval": False,
                    "human_receipts": _copy(linked_receipts),
                    "proposals": _copy(linked_proposals),
                }

    current_keys = {
        (entity, row[ID_KEYS[entity]]) for entity, rows in canonical_records.items() for row in rows
    }
    for entity, record_id in accepted_index:
        if (entity, record_id) not in current_keys:
            _issue(issues, "orphan_native_acceptance", entity=entity, record_id=record_id)

    references = _references(canonical_records)
    for item in references["items"]:
        if item["required"] and not item["resolved"]:
            _issue(
                issues,
                "unresolved_reference",
                entity=item["from_entity"],
                record_id=item["from_id"],
                detail={
                    "field": item["field"],
                    "to_entity": item["to_entity"],
                    "to_id": item["to_id"],
                },
            )
    for lead in canonical_records["leads"]:
        duplicates = lead_contact_dependencies(
            lead, canonical_records["leads"], lead.get("lead_id")
        )
        if duplicates:
            _issue(issues, "duplicate_lead_contact", entity="leads", record_id=lead.get("lead_id"))

    summaries = None
    try:
        validate_state(canonical_records)
        entity_coverage = {
            entity: coverage.get(entity, {}).get("records", "unknown") for entity in ENTITIES
        }
        aggregate = (
            "complete"
            if set(entity_coverage.values()) == {"complete"}
            else "unknown"
            if set(entity_coverage.values()) == {"unknown"}
            else "partial"
        )
        summaries = build_summaries(canonical_records, entity_coverage, aggregate)
    except (LocalPrimaryError, SummaryInputError) as exc:
        _issue(
            issues,
            "state_validation_failed",
            detail={"domain_code": getattr(exc, "code", str(exc))},
        )

    money, refunds = _money(canonical_records) if summaries is not None else ({}, [])
    ids = {
        entity: [row[ID_KEYS[entity]] for row in canonical_records[entity]] for entity in ENTITIES
    }
    revisions = {
        entity: {row[ID_KEYS[entity]]: row["revision"] for row in canonical_records[entity]}
        for entity in ENTITIES
    }
    record_hashes = {
        entity: {row[ID_KEYS[entity]]: canonical_digest(row) for row in canonical_records[entity]}
        for entity in ENTITIES
    }
    input_digests = {
        entity: {
            row[ID_KEYS[entity]]: record_input_digest(entity, row)
            for row in canonical_records[entity]
        }
        for entity in ENTITIES
    }
    source_modes = {
        decoded[source_range]["entity_mode"]
        for entity_ranges in classified.values()
        for source_range in entity_ranges
    }
    manifest_source_format = (
        PHYSICAL_SOURCE_FORMAT
        if source_modes == {"physical"}
        else NORMALISED_SOURCE_FORMAT
        if source_modes == {"normalised"}
        else "mixed_fixture_source_formats"
    )
    if len(source_modes) != 1:
        _issue(issues, "mixed_source_formats")
    result = {
        "status": "quarantined" if issues else "validated_candidate",
        "synthetic_only": True,
        "source_format": manifest_source_format,
        "physical_schema_compatibility": PHYSICAL_COMPATIBILITY,
        "source_binding": _copy(binding),
        "source_id": next(iter(source_ids)) if len(source_ids) == 1 else None,
        "observed_at": extract["observed_at"],
        "reader_read_digest": evidence["read_digest"],
        "counts": {entity: len(canonical_records[entity]) for entity in ENTITIES},
        "ids": ids,
        "revisions": revisions,
        "record_hashes": record_hashes,
        "input_digests": input_digests,
        "records_hash": canonical_digest(canonical_records),
        "coverage": coverage,
        "coverage_hash": canonical_digest(coverage),
        "header_history": header_history,
        "source_evidence": source_evidence,
        "provenance": provenance,
        "native_acceptance_history": accepted_history,
        "human_receipt_history": _copy(receipt_rows),
        "proposal_history": _copy(proposal_rows),
        "native_acceptance_is_human_approval": False,
        "audit_payload_validation_scope": "bounded_json_object_and_journal_linkage_only",
        "human_receipts_are_approval_proof": False,
        "references": references,
        "money_by_currency": money,
        "refunds": refunds,
        "summaries": summaries,
        "summaries_hash": canonical_digest(summaries) if summaries is not None else None,
        "issue_count": len(issues),
        "issue_codes": sorted({item["reason_code"] for item in issues}),
        "issues": issues,
        "writes_performed": 0,
        "importer_invoked": False,
        "business_ledger_invoked": False,
        "cutover_proof": False,
        "source_authorization_proof": False,
    }
    result["manifest_digest"] = canonical_digest(result)
    return _copy(result)


__all__ = [
    "ACCEPTED_HEADERS",
    "COVERAGE_HEADERS",
    "ENTITY_HEADERS",
    "ENTITY_ROLES",
    "HEADER_HISTORY_HEADERS",
    "NATIVE_ENTITY_HEADERS",
    "NATIVE_ENTITY_ROLES",
    "NATIVE_FIELDS",
    "NORMALISED_SOURCE_FORMAT",
    "PHYSICAL_COMPATIBILITY",
    "PHYSICAL_SOURCE_FORMAT",
    "PROPOSAL_HEADERS",
    "RECEIPT_HEADERS",
    "SOURCE_METADATA_HEADERS",
    "SourceBinding",
    "canonical_digest",
    "map_export",
    "native_raw_owned_digest",
    "record_input_digest",
]
