"""Canonical Assaf OS input validation and the seven-entity invariants (V2-16).

A verbatim copy of ``app/prototypes/assaf_os_domain.py``, which stays in place as V2-13's oracle
and is never edited (V2_PG_CONTRACTS.md section 3.1). Only two things differ from the prototype:
  * the checked-in schema projection (``assaf_os_local_schema.json``) is embedded as ``SCHEMA``,
    so the production module reads no prototype file;
  * the error type is ``DomainError`` instead of ``LocalPrimaryError``.
``tests/services/os_ledger/ported/`` runs the prototype's own tests against this module and pins the
schema and every public function to the prototype's behaviour.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date
from types import MappingProxyType
from typing import Any


class DomainError(Exception):
    """A domain refusal; ``code`` is the reason code (the prototype's LocalPrimaryError)."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


ENTITIES = ("leads", "clients", "works", "payments", "time", "expenses", "tasks")
ID_KEYS = MappingProxyType(
    {
        "leads": "lead_id",
        "clients": "person_id",
        "works": "work_id",
        "payments": "payment_id",
        "time": "time_id",
        "expenses": "expense_id",
        "tasks": "task_id",
    }
)

# The prototype's assaf_os_local_schema.json, embedded (a ported test pins the two equal).
SCHEMA: dict[str, Any] = {
    "source": "ops/assaf-os/schema.js",
    "version": "assaf-os-unified.v1",
    "enums": {
        "path": ["לימוד", "אוטומציה"],
        "lead_status": ["חדש", "רלוונטי", "לא רלוונטי", "לא ענה", "הפך ללקוח"],
        "client_status": ["פעיל", "לא פעיל"],
        "task_status": ["לביצוע", "בתהליך", "ממתין", "בוצע", "בוטל"],
        "work_status": ["מתוכננת", "פעילה", "הושלמה", "בוטלה"],
        "time_status": ["פעיל", "בוטל"],
        "payment_kind": ["התקבל", "החזר", "בוטל"],
        "payment_status": ["שולם", "לא שולם", "בוטל"],
        "expense_source": ["תשלום בפועל", "דיווח שימוש בפלטפורמה"],
        "source": ["משתמש", "OS", "MIA"],
        "currency": ["ILS", "USD", "EUR", "GBP"],
        "shared_path": ["לימוד", "אוטומציה", "משותף"],
    },
    "entities": {
        "leads": [
            {"key": "name", "type": "text", "role": "input", "required": True},
            {"key": "phone", "type": "text", "role": "input", "required": False},
            {"key": "email", "type": "text", "role": "input", "required": False},
            {"key": "path", "type": "enum", "role": "input", "required": True, "enum": "path"},
            {
                "key": "status",
                "type": "enum",
                "role": "input",
                "required": True,
                "enum": "lead_status",
            },
            {"key": "source_note", "type": "text", "role": "input", "required": False},
        ],
        "clients": [
            {"key": "name", "type": "text", "role": "input", "required": True},
            {"key": "phone", "type": "text", "role": "input", "required": False},
            {"key": "email", "type": "text", "role": "input", "required": False},
            {
                "key": "status",
                "type": "enum",
                "role": "input",
                "required": True,
                "enum": "client_status",
            },
            {"key": "notes", "type": "text", "role": "input", "required": False},
        ],
        "works": [
            {
                "key": "person_id",
                "type": "id",
                "role": "input",
                "required": True,
                "ref": "clients.person_id",
            },
            {"key": "title", "type": "text", "role": "input", "required": True},
            {
                "key": "path",
                "type": "enum",
                "role": "input",
                "required": True,
                "enum": "path",
                "immutableAfterCreate": True,
            },
            {"key": "start_date", "type": "date", "role": "input", "required": False},
            {
                "key": "status",
                "type": "enum",
                "role": "input",
                "required": True,
                "enum": "work_status",
            },
            {"key": "agreed_amount_ils", "type": "money_ils", "role": "input", "required": False},
            {
                "key": "currency",
                "type": "enum",
                "role": "input",
                "required": False,
                "enum": "currency",
            },
        ],
        "payments": [
            {
                "key": "person_id",
                "type": "id",
                "role": "input",
                "required": True,
                "ref": "clients.person_id",
            },
            {
                "key": "work_id",
                "type": "id",
                "role": "input",
                "required": True,
                "ref": "works.work_id",
            },
            {
                "key": "path",
                "type": "enum",
                "role": "input",
                "required": True,
                "enum": "path",
                "immutableAfterCreate": True,
            },
            {"key": "date", "type": "date", "role": "input", "required": True},
            {"key": "actual_amount_ils", "type": "money_ils", "role": "input", "required": True},
            {
                "key": "currency",
                "type": "enum",
                "role": "input",
                "required": True,
                "enum": "currency",
            },
            {
                "key": "kind",
                "type": "enum",
                "role": "input",
                "required": True,
                "enum": "payment_kind",
            },
            {
                "key": "original_payment_id",
                "type": "id",
                "role": "input",
                "required": False,
                "ref": "payments.payment_id",
            },
            {"key": "notes", "type": "text", "role": "input", "required": False},
        ],
        "time": [
            {
                "key": "person_id",
                "type": "id",
                "role": "input",
                "required": True,
                "ref": "clients.person_id",
            },
            {
                "key": "work_id",
                "type": "id",
                "role": "input",
                "required": True,
                "ref": "works.work_id",
            },
            {
                "key": "path",
                "type": "enum",
                "role": "input",
                "required": True,
                "enum": "path",
                "immutableAfterCreate": True,
            },
            {"key": "date", "type": "date", "role": "input", "required": True},
            {"key": "minutes", "type": "integer", "role": "input", "required": True, "minimum": 0},
            {"key": "description", "type": "text", "role": "input", "required": False},
            {
                "key": "status",
                "type": "enum",
                "role": "input",
                "required": True,
                "enum": "time_status",
            },
        ],
        "expenses": [
            {"key": "date", "type": "date", "role": "input", "required": True},
            {"key": "category", "type": "text", "role": "input", "required": True},
            {
                "key": "platform",
                "type": "enum",
                "role": "input",
                "required": True,
                "values": ["Google Ads", "Meta", "אחר"],
            },
            {
                "key": "path",
                "type": "enum",
                "role": "input",
                "required": True,
                "enum": "shared_path",
            },
            {"key": "vendor", "type": "text", "role": "input", "required": False},
            {"key": "amount_ils", "type": "money_ils", "role": "input", "required": True},
            {
                "key": "currency",
                "type": "enum",
                "role": "input",
                "required": True,
                "enum": "currency",
            },
            {
                "key": "payment_status",
                "type": "enum",
                "role": "input",
                "required": True,
                "enum": "payment_status",
            },
            {
                "key": "source_type",
                "type": "enum",
                "role": "input",
                "required": True,
                "enum": "expense_source",
            },
            {"key": "source_key", "type": "text", "role": "input", "required": True},
            {"key": "notes", "type": "text", "role": "input", "required": False},
        ],
        "tasks": [
            {"key": "title", "type": "text", "role": "input", "required": True},
            {
                "key": "person_id",
                "type": "id",
                "role": "input",
                "required": False,
                "ref": "clients.person_id",
            },
            {
                "key": "work_id",
                "type": "id",
                "role": "input",
                "required": False,
                "ref": "works.work_id",
            },
            {"key": "path", "type": "enum", "role": "input", "required": False, "enum": "path"},
            {
                "key": "status",
                "type": "enum",
                "role": "input",
                "required": True,
                "enum": "task_status",
            },
            {"key": "due_date", "type": "date", "role": "input", "required": False},
            {"key": "notes", "type": "text", "role": "input", "required": False},
        ],
    },
}

ENUMS = MappingProxyType({key: tuple(values) for key, values in SCHEMA["enums"].items()})
_FIELDS = {entity: tuple(fields) for entity, fields in SCHEMA["entities"].items()}
_FIELD_KEYS = {
    entity: frozenset(field["key"] for field in fields) for entity, fields in _FIELDS.items()
}
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,63}$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 ().-]*$")
_JS_TRIM_RE = re.compile(
    r"^[\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+"
    r"|[\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+$"
)
_MAX_TEXT = 2000
_MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _error(code: str) -> None:
    raise DomainError(code)


def _plain(value: Any) -> bool:
    return isinstance(value, dict)


def _safe_integer(value: Any) -> bool:
    return type(value) is int and abs(value) <= _MAX_SAFE_INTEGER


def _js_trim(value: str) -> str:
    return _JS_TRIM_RE.sub("", value)


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _field_value(field: Mapping[str, Any], value: Any) -> Any:
    if value is None or value == "":
        return None
    kind, key = field["type"], field["key"]
    if kind == "text":
        utf16_length = (
            len(value.encode("utf-16-le", "surrogatepass")) // 2
            if isinstance(value, str)
            else 0
        )
        if not isinstance(value, str) or utf16_length > _MAX_TEXT:
            _error("invalid_" + key)
        return _js_trim(value)
    if kind == "id":
        if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
            _error("invalid_" + key)
        return value
    if kind == "date":
        if not _valid_date(value):
            _error("invalid_" + key)
        return value
    if kind == "integer":
        if not _safe_integer(value) or value < field.get("minimum", 0):
            _error("invalid_" + key)
        return value
    if kind == "money_ils":
        if not _safe_integer(value) or value < 0:
            _error("invalid_" + key)
        return value
    if kind == "enum":
        values = field.get("values", ENUMS.get(field.get("enum"), ()))
        if not isinstance(value, str) or value not in values:
            _error("invalid_" + key)
        return value
    _error("unsupported_field_type")


def canonical_input(entity: str, raw: Any, creating: bool = True) -> dict[str, Any]:
    """Match ``AssafOsRuntime.canonicalInput`` for the seven persisted entities."""
    if entity not in ENTITIES:
        _error("invalid_entity")
    if not _plain(raw):
        _error("invalid_input_shape")
    if any(key not in _FIELD_KEYS[entity] for key in raw):
        _error("non_input_field")

    output: dict[str, Any] = {}
    for field in _FIELDS[entity]:
        key = field["key"]
        if key not in raw:
            if creating and field["required"]:
                _error("missing_" + key)
            continue
        value = _field_value(field, raw[key])
        if field["required"] and value is None:
            _error("missing_" + key)
        output[key] = value

    if entity == "payments":
        if output.get("kind") == "החזר" and not output.get("original_payment_id"):
            _error("refund_original_required")
        if output.get("kind") != "החזר" and output.get("original_payment_id"):
            _error("original_payment_only_for_refund")
        if "actual_amount_ils" in output and output["actual_amount_ils"] <= 0:
            _error("invalid_actual_amount_ils")
    if entity == "expenses" and "amount_ils" in output and output["amount_ils"] <= 0:
        _error("invalid_amount_ils")
    return output


def lead_phone_identity(value: Any) -> str:
    """Return the exact contact identity used by the Apps Script duplicate guard."""
    raw = _js_trim(str(value or ""))
    if not raw or _PHONE_RE.fullmatch(raw) is None or raw.count("(") != raw.count(")"):
        return ""
    digits = re.sub(r"[^0-9]", "", raw)
    if raw.startswith("+"):
        if re.fullmatch(r"[1-9][0-9]{6,14}", digits) is None or digits.startswith("9720"):
            return ""
        if digits.startswith("9725") and re.fullmatch(r"9725[0-9]{8}", digits) is None:
            return ""
        return "+" + digits
    if digits.startswith("05"):
        return "+972" + digits[1:] if re.fullmatch(r"05[0-9]{8}", digits) else ""
    return digits if re.fullmatch(r"[0-9]{7,15}", digits) else ""


def lead_contact_dependencies(
    candidate: Mapping[str, Any] | None,
    leads: Sequence[Mapping[str, Any]],
    record_id: str | None = None,
) -> list[Mapping[str, Any]]:
    """Find committed leads that the Apps Script contact guard considers equivalent."""
    if candidate is None:
        return []
    phone = lead_phone_identity(candidate.get("phone"))
    email = _js_trim(str(candidate.get("email") or "")).lower()
    if not phone and not email:
        return []
    return [
        lead
        for lead in leads
        if lead.get("lead_id") != record_id
        and (
            bool(phone and lead_phone_identity(lead.get("phone")) == phone)
            or bool(email and _js_trim(str(lead.get("email") or "")).lower() == email)
        )
    ]


def _canonical_record(entity: str, record: Any) -> dict[str, Any]:
    if not _plain(record):
        _error("invalid_records")
    id_key = ID_KEYS[entity]
    system_keys = {id_key, "revision"} | ({"person_id"} if entity == "leads" else set())
    if any(key not in _FIELD_KEYS[entity] | system_keys for key in record):
        _error("non_input_field")
    identity = record.get(id_key)
    if not isinstance(identity, str) or _ID_RE.fullmatch(identity) is None:
        _error("invalid_" + id_key)
    revision = record.get("revision")
    if not _safe_integer(revision) or revision <= 0:
        _error("invalid_revision")
    if entity == "leads":
        person_id = record.get("person_id")
        if not isinstance(person_id, str) or _ID_RE.fullmatch(person_id) is None:
            _error("invalid_person_id")
    raw = {key: record[key] for key in _FIELD_KEYS[entity] if key in record}
    canonical = canonical_input(entity, raw, creating=True)
    # Reject stored values whose canonical representation differs (notably untrimmed text).
    if canonical != raw:
        for key in canonical:
            if canonical[key] != raw.get(key):
                _error("invalid_" + key)
        _error("invalid_records")
    return record


def _record_indexes(
    records: Mapping[str, list[dict[str, Any]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    indexes: dict[str, dict[str, dict[str, Any]]] = {}
    for entity in ENTITIES:
        by_id: dict[str, dict[str, Any]] = {}
        for record in records[entity]:
            _canonical_record(entity, record)
            identity = record[ID_KEYS[entity]]
            if identity in by_id:
                _error("invalid_or_duplicate_record_id")
            by_id[identity] = record
        indexes[entity] = by_id
    return indexes


def _validate_work_dependency(
    entity: str,
    record: Mapping[str, Any],
    clients: Mapping[str, Mapping[str, Any]],
    works: Mapping[str, Mapping[str, Any]],
) -> None:
    person_id = record.get("person_id")
    if person_id and person_id not in clients:
        _error("client_not_found")
    work_id = record.get("work_id")
    if not work_id or entity == "works":
        return
    work = works.get(work_id)
    if work is None:
        _error("work_not_found")
    if person_id and work["person_id"] != person_id:
        _error("work_person_mismatch")
    if record.get("path") and work["path"] != record["path"]:
        _error("work_path_mismatch")
    if record.get("currency") and work.get("currency") != record["currency"]:
        _error("work_currency_mismatch")


def _validate_payment_graph(payments: Sequence[Mapping[str, Any]]) -> None:
    by_id = {payment["payment_id"]: payment for payment in payments}
    for candidate in payments:
        if candidate["kind"] != "החזר":
            continue
        original = by_id.get(candidate.get("original_payment_id"))
        if original is None or original["kind"] != "התקבל":
            _error("refund_original_not_found")
        if any(
            original[key] != candidate[key] for key in ("person_id", "work_id", "path", "currency")
        ):
            _error("refund_original_mismatch")
        if candidate["date"] < original["date"]:
            _error("refund_before_original")
        total = (
            sum(
                payment["actual_amount_ils"]
                for payment in payments
                if payment["kind"] == "החזר"
                and payment.get("original_payment_id") == original["payment_id"]
                and payment["payment_id"] != candidate["payment_id"]
            )
            + candidate["actual_amount_ils"]
        )
        if not _safe_integer(total) or total > original["actual_amount_ils"]:
            _error("refund_exceeds_original")


def validate_state(records: Any) -> None:
    """Validate one workspace's complete canonical seven-entity state."""
    if not _plain(records) or set(records) != set(ENTITIES):
        _error("invalid_records")
    if any(not isinstance(records[entity], list) for entity in ENTITIES):
        _error("invalid_records")
    indexes = _record_indexes(records)
    clients, works = indexes["clients"], indexes["works"]
    for entity in ("works", "payments", "time", "tasks"):
        for record in records[entity]:
            _validate_work_dependency(entity, record, clients, works)

    _validate_payment_graph(records["payments"])
    expense_sources: set[tuple[Any, Any]] = set()
    for expense in records["expenses"]:
        source = (expense["source_type"], expense["source_key"])
        if source in expense_sources:
            _error("duplicate_expense_source")
        expense_sources.add(source)


def validate_transition(
    entity: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any],
    records: Mapping[str, list[dict[str, Any]]],
) -> None:
    """Validate a prospective create/update against the current workspace state."""
    if entity not in ENTITIES:
        _error("invalid_entity")
    _canonical_record(entity, after)
    id_key = ID_KEYS[entity]
    if before is not None:
        _canonical_record(entity, before)
        if before[id_key] != after[id_key]:
            _error("immutable_field_changed")
        for field in _FIELDS[entity]:
            if field.get("immutableAfterCreate") and before.get(field["key"]) != after.get(
                field["key"]
            ):
                _error("immutable_field_changed")
    elif entity == "leads" and lead_contact_dependencies(after, records.get("leads", [])):
        _error("lead_already_exists")

    prospective = {key: [dict(row) for row in records[key]] for key in ENTITIES}
    found = False
    for index, row in enumerate(prospective[entity]):
        if row.get(id_key) == after[id_key]:
            prospective[entity][index] = dict(after)
            found = True
            break
    if found != (before is not None):
        _error("target_changed")
    if not found:
        prospective[entity].append(dict(after))
    validate_state(prospective)


__all__ = [
    "ENTITIES",
    "ENUMS",
    "DomainError",
    "ID_KEYS",
    "SCHEMA",
    "canonical_input",
    "lead_contact_dependencies",
    "lead_phone_identity",
    "validate_state",
    "validate_transition",
]
