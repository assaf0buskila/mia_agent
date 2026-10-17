"""Wire contract v2 (V2_PG_CONTRACTS.md section 10): the bodies, and the readers that accept them.

``assaf-os.v2`` is emitted only under ``backend=postgres`` (I9). Every body carries the binding of
section 10.1 and never a Sheets key. The builders here turn the frozen ledger types
(``os_ledger.api``) into the section 10.2 bodies; the ``read_*`` functions are the Python port of
the frontend's exact-key readers: one extra key, one missing key, an unknown actor or a mixed
v1/v2 body is refused with a ``ContractError`` whose reason is a fixed code.

The same readers validate the goldens (``tests/contract``), guard the PostgresOsBackend's own
output, and let Mia's follow-up consumers trust what they read. Nothing here logs, and nothing
here touches a database.
"""

from __future__ import annotations

import copy
import functools
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from importlib import resources
from typing import Any, Literal

from app.services.os_ledger import api

API_VERSION = "assaf-os.v2"
V1_API_VERSION = "assaf-os.v1"
SOURCE = "postgresql"
V1_SOURCE = "google_sheets"
SCHEMA_VERSION = "assaf-os-unified.v1"
MONEY_UNIT = "minor"
APPROVAL_GUARD_VERSION = 1

ENTITIES: tuple[str, ...] = api.ENTITIES
# The record entities plus the one composite proposal entity (a lead promotion).
PROPOSAL_ENTITIES: tuple[str, ...] = (*ENTITIES, "promote_lead")
# A non-proposal kind names its own entity: the abandon marker, the statement batch, the import.
KIND_ENTITIES: tuple[str, ...] = (*PROPOSAL_ENTITIES, "abandon", "statements", "import")
ACTOR_SOURCES: tuple[str, ...] = (
    "OS", "OS_DIRECT", "MIA", "TELEGRAM_APPROVAL",
    "ASSISTANT_CLAUDE", "ASSISTANT_GPT", "ASSISTANT_PASTE", "IMPORT",
)
PROPOSAL_KINDS: tuple[str, ...] = (
    "proposal", "owner_direct", "owner_undo", "statement_batch", "import",
)
# The statuses a stored ``proposal``-kind row may carry, pending or decided: what the backend's own
# guard accepts for a replay or a read by id. ``superseded`` and ``abandoned`` are added inside the
# request-recovery envelope (section 10.2).
PROPOSAL_STATUSES: tuple[str, ...] = (
    "pending", "committed", "rejected", "expired", "target_changed",
)
PROPOSAL_REQUEST_STATUSES: tuple[str, ...] = (*PROPOSAL_STATUSES, "superseded", "abandoned")
# The frontend's ``readProposalV2``, ``readPendingList`` and the ``GET /proposals/{id}`` body are
# pending and of kind ``proposal`` only (section 10.2). Anything wider is an explicit argument.
LONE_PROPOSAL_STATUSES: tuple[str, ...] = ("pending",)
LONE_PROPOSAL_KINDS: tuple[str, ...] = ("proposal",)
# The kinds whose committed receipt always lists records: they always carry record mutations.
# A statement batch of only personal lines (no expense op, section 12.2) and an import do not.
_RECORD_KINDS = frozenset({"proposal", "owner_direct", "owner_undo"})
RECEIPT_STATUSES: tuple[str, ...] = (
    "committed", "rejected", "expired", "target_changed", "superseded", "abandoned",
)
COVERAGE: tuple[str, ...] = ("complete", "partial", "unknown")
MIRROR_STATES: tuple[str, ...] = ("verified", "lagging", "unknown", "conflict", "disabled")
UNDO_STATES: tuple[str, ...] = ("available", "undone", "expired", "none")
LABEL_KEYS: tuple[str, ...] = ("clients.person_id", "works.work_id")
MAX_PAGE_RECORDS = 200
MAX_MONTHLY_ROWS = 600
MAX_REASON_CODES = 20
MAX_LABEL_CHARS = 120
SUMMARY_MONTHLY_KEYS: tuple[str, ...] = (
    "month", "currency", "paid_income_ils", "paid_expenses_ils", "net_cash_ils",
    "recorded_minutes", "effective_hourly_ils", "coverage", "reason_codes",
)
_MONTHLY_INT_KEYS = (
    "paid_income_ils", "paid_expenses_ils", "net_cash_ils", "recorded_minutes",
    "effective_hourly_ils",
)

_BINDING_KEYS = ("api_version", "source", "workspace_id", "authority_epoch")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", re.IGNORECASE
)
_TOKEN = re.compile(r"[\x21-\x7e]{1,512}")
_SINCE = re.compile(r"[0-9]{1,19}\.[0-9]{1,19}\.[0-9]{1,19}")
_MONTH = re.compile(r"[0-9]{4}-[0-9]{2}")
_MAX_SAFE_INTEGER = 2**53 - 1
_FIELD_TYPES = frozenset(
    {"text", "id", "enum", "date", "month", "datetime", "integer", "decimal", "money_ils",
     "boolean"}
)
_FIELD_ROLES = frozenset({"input", "system", "calculated"})


class ContractError(ValueError):
    """A body that breaks the contract. ``reason`` is a fixed, log-safe code; never a value."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _fail(reason: str) -> Any:
    raise ContractError(reason)


# ---------------------------------------------------------------------------------------------
# Time


def iso(value: datetime) -> str:
    """UTC with millisecond precision and a ``Z``: the one timestamp format of every v2 body."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        _fail("naive_datetime")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else iso(value)


# ---------------------------------------------------------------------------------------------
# The schema (generated from ops/assaf-os/schema.js; tests/unit/test_contract_schema_parity.py
# pins the file to it)


@functools.lru_cache(maxsize=1)
def _schema_source() -> dict[str, Any]:
    text = resources.files(__package__).joinpath("schema_v2.json").read_text(encoding="utf-8")
    source = json.loads(text)
    if source.get("version") != SCHEMA_VERSION:
        _fail("schema_source_version")
    return source


def schema_tabs() -> list[dict[str, Any]]:
    return copy.deepcopy(_schema_source()["tabs"])


def schema_enums() -> dict[str, list[str]]:
    return copy.deepcopy(_schema_source()["enums"])


def _tab_fields(entity: str) -> list[dict[str, Any]]:
    tab = next(tab for tab in _schema_source()["tabs"] if tab["key"] == entity)
    return tab["fields"]


@functools.lru_cache(maxsize=1)
def input_fields() -> Mapping[str, frozenset[str]]:
    """Per entity, the input-role field keys (what a proposal may set)."""
    return {
        entity: frozenset(f["key"] for f in _tab_fields(entity) if f["role"] == "input")
        for entity in ENTITIES
    }


@functools.lru_cache(maxsize=1)
def text_fields() -> Mapping[str, frozenset[str]]:
    """Per entity, the free-text input fields: the ones that can carry third-party text."""
    return {
        entity: frozenset(
            f["key"] for f in _tab_fields(entity) if f["role"] == "input" and f["type"] == "text"
        )
        for entity in ENTITIES
    }


@functools.lru_cache(maxsize=1)
def reference_fields() -> Mapping[str, tuple[tuple[str, str], ...]]:
    """Per entity, ``(field, "<entity>.<id key>")`` for every field that names a labelled row."""
    return {
        entity: tuple(
            (f["key"], f["ref"]) for f in _tab_fields(entity) if f.get("ref") in LABEL_KEYS
        )
        for entity in ENTITIES
    }


# ---------------------------------------------------------------------------------------------
# Builders: ledger types -> section 10.2 bodies


def binding(
    workspace_id: str, authority_epoch: int, observed_version: int | None = None
) -> dict[str, Any]:
    """Section 10.1. ``observed_version`` is on every read body except the schema."""
    body: dict[str, Any] = {
        "api_version": API_VERSION,
        "source": SOURCE,
        "workspace_id": workspace_id,
        "authority_epoch": authority_epoch,
    }
    if observed_version is not None:
        body["observed_version"] = observed_version
    return body


def schema_body(workspace_id: str, authority_epoch: int) -> dict[str, Any]:
    return binding(workspace_id, authority_epoch) | {
        "schema_version": SCHEMA_VERSION,
        "money_unit": MONEY_UNIT,
        "tabs": schema_tabs(),
        "enums": schema_enums(),
    }


def mirror_view(
    state: str,
    *,
    last_verified_at: datetime | str | None,
    verified_version: int,
    lag_s: int | None,
) -> dict[str, Any]:
    return {
        "state": state,
        "last_verified_at": (
            last_verified_at if isinstance(last_verified_at, str | type(None))
            else iso(last_verified_at)
        ),
        "verified_version": verified_version,
        "lag_s": lag_s,
    }


def summary_view(monthly: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """``{coverage, monthly}``: the v1 monthly rows unchanged, with the coverage they imply."""
    rows = [dict(row) for row in monthly]
    coverages = {row.get("coverage") for row in rows}
    coverage = (
        "unknown" if "unknown" in coverages
        else "partial" if coverages - {"complete"} else "complete"
    )
    return {"coverage": coverage, "monthly": rows}


def label_refs(entity: str, records: Iterable[Mapping[str, Any]]) -> list[api.RecordRef]:
    """The labelled rows the records on one page refer to, sorted and unique."""
    wanted: set[tuple[str, str]] = set()
    for record in records:
        for field, ref in reference_fields().get(entity, ()):
            value = record.get(field)
            if isinstance(value, str) and value:
                wanted.add((ref.split(".")[0], value))
    return [
        api.RecordRef(target, record_id)  # type: ignore[arg-type]
        for target, record_id in sorted(wanted)
    ]


def _cut_utf16(text: str, limit: int) -> str:
    """The longest prefix of ``text`` whose JavaScript length is at most ``limit``."""
    kept, used = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > limit:
            break
        kept.append(char)
        used += width
    return "".join(kept)


def labels_view(raw: Mapping[str, Mapping[str, str]]) -> dict[str, dict[str, str]]:
    """Both label maps, always present, each label at most 120 UTF-16 units (the frontend cap)."""
    return {
        key: {
            record_id: _cut_utf16(str(label), MAX_LABEL_CHARS)
            for record_id, label in sorted(dict(raw.get(key, {})).items())
        }
        for key in LABEL_KEYS
    }


def page_body(
    *,
    workspace_id: str,
    authority_epoch: int,
    observed_version: int,
    observed_at: datetime,
    since: str,
    entity: str,
    records: Iterable[Mapping[str, Any]],
    next_cursor: str | None,
    total: int,
    summary: Mapping[str, Any],
    pending_count: int,
    labels: Mapping[str, Mapping[str, str]],
    mirror: Mapping[str, Any],
) -> dict[str, Any]:
    return binding(workspace_id, authority_epoch, observed_version) | {
        "observed_at": iso(observed_at),
        "unchanged": False,
        "since": since,
        "entity": entity,
        "records": [dict(record) for record in records],
        "next_cursor": next_cursor,
        "total": total,
        "coverage": "complete",
        "summary": dict(summary),
        "pending_count": pending_count,
        "labels": labels_view(labels),
        "mirror": dict(mirror),
    }


def unchanged_body(
    *,
    workspace_id: str,
    authority_epoch: int,
    observed_version: int,
    since: str,
    mirror: Mapping[str, Any],
) -> dict[str, Any]:
    return binding(workspace_id, authority_epoch, observed_version) | {
        "unchanged": True,
        "since": since,
        "mirror": dict(mirror),
    }


def deleted_body(
    *,
    workspace_id: str,
    authority_epoch: int,
    observed_version: int,
    entity: str,
    records: Iterable[Mapping[str, Any]],
    next_cursor: str | None,
) -> dict[str, Any]:
    return binding(workspace_id, authority_epoch, observed_version) | {
        "entity": entity,
        "records": [dict(record) for record in records],
        "next_cursor": next_cursor,
    }


def pending_body(
    *,
    workspace_id: str,
    authority_epoch: int,
    observed_version: int,
    items: Iterable[Mapping[str, Any]],
    next_cursor: str | None,
) -> dict[str, Any]:
    return binding(workspace_id, authority_epoch, observed_version) | {
        "items": [dict(item) for item in items],
        "next_cursor": next_cursor,
    }


def _changed_input(entity: str, before: Mapping[str, Any] | None, after: Mapping[str, Any]):
    keys = input_fields().get(entity, frozenset())
    return {
        key: after[key]
        for key in after
        if key in keys and (before is None or before.get(key) != after.get(key))
    }


def _proposal_states(row: api.ProposalRow) -> tuple[dict, Any, Any]:
    """``input``, ``before`` and ``after`` of a proposal, derived from its immutable mutations.

    A single-record proposal shows the changed input fields and the whole record before and after.
    A lead promotion shows the lead and its client. A bulk, an abandon marker, a statement batch
    and an import show none: their receipt carries the records.
    """
    mutations = row.mutations
    if row.kind == "proposal" and row.entity == "promote_lead":
        by_entity = {m.entity: m for m in mutations}
        if len(mutations) != 2 or set(by_entity) != {"leads", "clients"}:
            _fail("promotion_mutations")
        lead, client = by_entity["leads"], by_entity["clients"]
        before = {
            "lead": dict(lead.before) if lead.before else None,
            "client": dict(client.before) if client.before else None,
        }
        after = {"lead": dict(lead.after), "client": dict(client.after)}
        return {}, before, after
    if row.kind in {"proposal", "owner_direct", "owner_undo"} and len(mutations) == 1:
        mutation = mutations[0]
        before = dict(mutation.before) if mutation.before else None
        after = dict(mutation.after)
        return _changed_input(mutation.entity, before, after), before, after
    if row.kind == "proposal":
        _fail("proposal_mutations")
    return {}, None, None


def proposal_v2(row: api.ProposalRow, *, authority_epoch: int) -> dict[str, Any]:
    """``proposal_v2``: the binding without ``observed_version`` plus the immutable proposal."""
    input_, before, after = _proposal_states(row)
    return binding(row.workspace_id, authority_epoch) | {
        "status": row.status,
        "proposal_id": row.proposal_id,
        "request_id": row.request_id,
        "approval_request_id": row.approval_id,
        "rejection_request_id": row.rejection_id,
        "approval_guard_version": APPROVAL_GUARD_VERSION,
        "actor_source": row.source,
        "kind": row.kind,
        "expires_at": iso(row.expires_at),
        "entity": row.entity,
        "record_id": row.record_id,
        "expected_revision": row.expected_revision,
        "input": input_,
        "before": before,
        "after": after,
    }


_NO_UNDO = {"state": "none", "until": None, "undo_request_id": None}


def receipt_v2(
    result: api.ReceiptResult,
    *,
    workspace_id: str,
    authority_epoch: int,
    request_id: str,
    server_now: datetime,
) -> dict[str, Any]:
    """``receipt_v2`` for a ledger answer; ``unknown`` (all nullable fields null) when not found."""
    head = binding(workspace_id, authority_epoch, result.observed_version)
    if not result.found:
        return head | {
            "status": "unknown",
            "request_id": request_id,
            "proposal_id": None,
            "kind": None,
            "actor_source": None,
            "commit_version": None,
            "committed_at": None,
            "server_now": iso(server_now),
            "records": [],
            "undo": None,
            "undo_of": None,
            "reason_codes": ["receipt_not_found"],
        }
    receipt, proposal = result.receipt, result.proposal
    if receipt is None or proposal is None:
        _fail("receipt_without_proposal")
    shows_records = receipt.status in {"committed", "target_changed"}
    undo = None
    if receipt.kind == "owner_direct":
        view = result.undo
        if view is None or receipt.status != "committed":
            undo = dict(_NO_UNDO)
        else:
            undo = {
                "state": view.state,
                "until": _optional_iso(view.until),
                "undo_request_id": view.undo_request_id,
            }
    return head | {
        "status": receipt.status,
        "request_id": receipt.request_id,
        "proposal_id": receipt.proposal_id,
        "kind": receipt.kind,
        "actor_source": proposal.source,
        "commit_version": receipt.commit_version,
        "committed_at": iso(receipt.completed_at) if receipt.status == "committed" else None,
        "server_now": iso(server_now),
        "records": (
            [{"entity": entity, "record": dict(record)} for entity, record in result.records]
            if shows_records else []
        ),
        "undo": undo,
        "undo_of": receipt.undo_of,
        "reason_codes": list(receipt.reason_codes),
    }


def proposal_request_body(
    *,
    workspace_id: str,
    authority_epoch: int,
    observed_version: int,
    request_id: str,
    proposal: Mapping[str, Any] | None,
    receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    found = proposal is not None
    return binding(workspace_id, authority_epoch, observed_version) | {
        "request_id": request_id,
        "status": "found" if found else "unknown",
        "proposal": dict(proposal) if proposal is not None else None,
        "receipt": dict(receipt) if receipt is not None else None,
        "reason_codes": [] if found else ["proposal_request_unknown"],
    }


# ---------------------------------------------------------------------------------------------
# Readers: the Python port of the frontend's exact-key readers


def _is_object(value: Any) -> bool:
    return isinstance(value, dict)


def _member(value: Any, allowed: Iterable[str]) -> bool:
    """``value`` is one of the strings in ``allowed``. Never raises on a list or object value."""
    return isinstance(value, str) and value in tuple(allowed)


def _exact(value: Any, required: Iterable[str], optional: Iterable[str] = ()) -> dict[str, Any]:
    if not _is_object(value):
        _fail("not_an_object")
    required, optional = tuple(required), tuple(optional)
    if not all(key in value for key in required):
        _fail("missing_keys")
    if not all(key in required or key in optional for key in value):
        _fail("unexpected_keys")
    return value


def _is_int(value: Any) -> bool:
    return type(value) is int and abs(value) <= _MAX_SAFE_INTEGER


def _is_nat(value: Any) -> bool:
    return _is_int(value) and value >= 0


def _js_length(value: str) -> int:
    """JavaScript's ``String.length``: UTF-16 code units, which the frontend readers cap."""
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


def _is_text(value: Any, max_chars: int = 2000) -> bool:
    return isinstance(value, str) and _js_length(value) <= max_chars


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _is_uuid(value: Any) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _is_iso(value: Any) -> bool:
    if not _is_text(value, 40):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _nullable(value: Any, test) -> bool:
    return value is None or bool(test(value))


def _is_token(value: Any) -> bool:
    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None


def _is_loose_record(value: Any) -> bool:
    if not _is_object(value) or len(value) > 50:
        return False
    for key, item in value.items():
        if not _is_id(key):
            return False
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, str):
            if not _is_text(item):
                return False
        elif type(item) in {int, float}:
            if not math.isfinite(item):
                return False
        else:
            return False
    return True


def _reason_codes(value: Any) -> bool:
    return (
        isinstance(value, list) and len(value) <= MAX_REASON_CODES
        and all(_is_id(code) for code in value)
    )


def check_binding(value: Any, *, observed: bool) -> dict[str, Any]:
    """Section 10.1: the v2 binding keys (and ``observed_version`` when ``observed``)."""
    if not _is_object(value):
        _fail("not_an_object")
    if (
        value.get("api_version") != API_VERSION
        or value.get("source") != SOURCE
        or not _is_id(value.get("workspace_id"))
        or not _is_int(value.get("authority_epoch")) or value["authority_epoch"] < 1
    ):
        _fail("binding")
    if observed and not _is_nat(value.get("observed_version")):
        _fail("observed_version")
    return value


def _same_binding(left: Mapping[str, Any], right: Mapping[str, Any]) -> None:
    if (left["workspace_id"], left["authority_epoch"]) != (
        right["workspace_id"], right["authority_epoch"]
    ):
        _fail("binding_mismatch")


def _read_field(value: Any) -> None:
    _exact(
        value, ("key", "label", "type", "role", "required"),
        ("enum", "values", "stable", "unique", "ref", "note", "immutableAfterCreate", "minimum"),
    )
    if (
        not _is_id(value["key"]) or not _is_text(value["label"], 120)
        or not _member(value["type"], _FIELD_TYPES) or not _member(value["role"], _FIELD_ROLES)
        or type(value["required"]) is not bool
    ):
        _fail("schema_field")
    if "enum" in value and not _is_id(value["enum"]):
        _fail("schema_field")
    if "values" in value and (
        not isinstance(value["values"], list) or len(value["values"]) > 50
        or not all(_is_text(item, 100) for item in value["values"])
    ):
        _fail("schema_field")
    for flag in ("stable", "unique", "immutableAfterCreate"):
        if flag in value and type(value[flag]) is not bool:
            _fail("schema_field")
    if ("ref" in value and not _is_text(value["ref"], 128)) or (
        "note" in value and not _is_text(value["note"], 300)
    ) or ("minimum" in value and not (
        type(value["minimum"]) in {int, float} and math.isfinite(value["minimum"])
    )):
        _fail("schema_field")


def read_schema_v2(value: Any) -> dict[str, Any]:
    """``GET /schema``: the binding without ``observed_version``, then the v1 tab rules."""
    _exact(value, (*_BINDING_KEYS, "schema_version", "money_unit", "tabs", "enums"))
    check_binding(value, observed=False)
    tabs = value["tabs"]
    if (
        value["schema_version"] != SCHEMA_VERSION or value["money_unit"] != MONEY_UNIT
        or not isinstance(tabs, list) or not 8 <= len(tabs) <= 12 or not _is_object(value["enums"])
    ):
        _fail("schema_shape")
    seen: set[str] = set()
    for tab in tabs:
        _exact(tab, ("key", "name", "kind", "frozenRows", "fields", "order"))
        fields = tab["fields"]
        if (
            not _is_id(tab["key"]) or not _is_text(tab["name"], 80)
            or not _member(tab["kind"], ("input", "calculated"))
            or not _is_int(tab["frozenRows"]) or not _is_int(tab["order"])
            or not isinstance(fields, list) or len(fields) > 40 or tab["key"] in seen
        ):
            _fail("schema_tab")
        seen.add(tab["key"])
        keys: set[str] = set()
        for field in fields:
            _read_field(field)
            if field["key"] in keys:
                _fail("schema_field")
            keys.add(field["key"])
    if not {"summary", *ENTITIES} <= seen:
        _fail("schema_tabs")
    for key, values in value["enums"].items():
        if not _is_id(key) or not isinstance(values, list) or len(values) > 50 or not all(
            _is_text(item, 100) for item in values
        ):
            _fail("schema_enum")
    return value


def _read_mirror(value: Any) -> None:
    _exact(value, ("state", "last_verified_at", "verified_version", "lag_s"))
    if (
        value["state"] not in MIRROR_STATES
        or not _nullable(value["last_verified_at"], _is_iso)
        or not _is_nat(value["verified_version"])
        or not _nullable(value["lag_s"], _is_nat)
    ):
        _fail("mirror")


def _read_summary(value: Any) -> None:
    _exact(value, ("coverage", "monthly"))
    monthly = value["monthly"]
    if (
        value["coverage"] not in COVERAGE or not isinstance(monthly, list)
        or len(monthly) > MAX_MONTHLY_ROWS
    ):
        _fail("summary")
    for row in monthly:
        _exact(row, SUMMARY_MONTHLY_KEYS)
        if (
            not isinstance(row["month"], str) or _MONTH.fullmatch(row["month"]) is None
            or not _is_id(row["currency"]) or row["coverage"] not in COVERAGE
            or not all(_nullable(row[key], _is_int) for key in _MONTHLY_INT_KEYS)
            or not isinstance(row["reason_codes"], list)
            or len(row["reason_codes"]) > MAX_REASON_CODES
            or not all(_is_id(code) for code in row["reason_codes"])
        ):
            _fail("summary_row")


def _read_labels(value: Any) -> None:
    _exact(value, LABEL_KEYS)
    for key in LABEL_KEYS:
        mapping = value[key]
        if not _is_object(mapping) or len(mapping) > MAX_PAGE_RECORDS * 2 or not all(
            _is_id(record_id) and _is_text(label, MAX_LABEL_CHARS)
            for record_id, label in mapping.items()
        ):
            _fail("labels")


def _read_records(value: Any) -> None:
    if (
        not isinstance(value, list) or len(value) > MAX_PAGE_RECORDS
        or not all(_is_loose_record(record) for record in value)
    ):
        _fail("records")


def read_snapshot_v2(value: Any) -> dict[str, Any]:
    """``GET ""``: a page body or the ``unchanged`` body (section 10.2), exactly."""
    if _is_object(value) and value.get("unchanged") is True:
        _exact(value, (*_BINDING_KEYS, "observed_version", "unchanged", "since", "mirror"))
        check_binding(value, observed=True)
        if not isinstance(value["since"], str) or _SINCE.fullmatch(value["since"]) is None:
            _fail("since")
        _read_mirror(value["mirror"])
        return value
    _exact(value, (
        *_BINDING_KEYS, "observed_version", "observed_at", "unchanged", "since", "entity",
        "records", "next_cursor", "total", "coverage", "summary", "pending_count", "labels",
        "mirror",
    ))
    check_binding(value, observed=True)
    if (
        value["unchanged"] is not False or not _is_iso(value["observed_at"])
        or not isinstance(value["since"], str) or _SINCE.fullmatch(value["since"]) is None
        or value["entity"] not in ENTITIES or value["coverage"] != "complete"
        or not _nullable(value["next_cursor"], _is_token)
        or not _is_nat(value["total"]) or not _is_nat(value["pending_count"])
    ):
        _fail("page_shape")
    _read_records(value["records"])
    if value["total"] < len(value["records"]):
        _fail("page_total")
    _read_summary(value["summary"])
    _read_labels(value["labels"])
    _read_mirror(value["mirror"])
    return value


def _read_state(value: Any, entity: str, *, after: bool, kind: str) -> None:
    """A proposal's ``before`` or ``after``: a record, a lead/client pair, or null."""
    if value is None:
        if after and kind == "proposal":
            _fail("proposal_state")
        return
    if entity == "promote_lead":
        _exact(value, ("lead", "client"))
        if not _is_loose_record(value["lead"]) or not (
            _is_loose_record(value["client"]) if after else _nullable(value["client"],
                                                                      _is_loose_record)
        ):
            _fail("proposal_state")
        return
    if not _is_loose_record(value):
        _fail("proposal_state")


def read_proposal_v2(
    value: Any,
    *,
    statuses: Sequence[str] = LONE_PROPOSAL_STATUSES,
    kinds: Sequence[str] = LONE_PROPOSAL_KINDS,
) -> dict[str, Any]:
    """``proposal_v2``: the binding without ``observed_version`` and the immutable proposal.

    By default it is the frontend's ``readProposalV2``: pending, kind ``proposal``. A caller that
    legitimately holds a decided or a batch/import proposal (the request-recovery envelope, the
    backend's own guard of a stored row) says so in ``statuses`` and ``kinds``.
    """
    _exact(value, (
        *_BINDING_KEYS, "status", "proposal_id", "request_id", "approval_request_id",
        "rejection_request_id", "approval_guard_version", "actor_source", "kind", "expires_at",
        "entity", "record_id", "expected_revision", "input", "before", "after",
    ))
    check_binding(value, observed=False)
    kind = value["kind"]
    if (
        not _member(value["status"], statuses) or not _is_id(value["proposal_id"])
        or not all(_is_uuid(value[key]) for key in (
            "request_id", "approval_request_id", "rejection_request_id"))
        or value["approval_request_id"] == value["rejection_request_id"]
        or type(value["approval_guard_version"]) is not int  # not ``true``: True == 1 in Python
        or value["approval_guard_version"] != APPROVAL_GUARD_VERSION
        or value["actor_source"] not in ACTOR_SOURCES or not _member(kind, kinds)
        or not _is_iso(value["expires_at"])
        or value["entity"] not in (PROPOSAL_ENTITIES if kind == "proposal" else KIND_ENTITIES)
        or not _nullable(value["record_id"], _is_id)
        or not _is_nat(value["expected_revision"]) or not _is_loose_record(value["input"])
    ):
        _fail("proposal_shape")
    _read_state(value["before"], value["entity"], after=False, kind=kind)
    _read_state(value["after"], value["entity"], after=True, kind=kind)
    return value


def read_pending_list(value: Any) -> dict[str, Any]:
    """``GET /pending``: pending ``proposal``-kind items of one binding."""
    _exact(value, (*_BINDING_KEYS, "observed_version", "items", "next_cursor"))
    check_binding(value, observed=True)
    items = value["items"]
    if (
        not isinstance(items, list) or len(items) > MAX_PAGE_RECORDS
        or not _nullable(value["next_cursor"], _is_token)
    ):
        _fail("pending_shape")
    for item in items:
        read_proposal_v2(item, statuses=LONE_PROPOSAL_STATUSES, kinds=LONE_PROPOSAL_KINDS)
        _same_binding(item, value)
    return value


def _read_undo(value: Any) -> None:
    _exact(value, ("state", "until", "undo_request_id"))
    state = value["state"]
    if (
        state not in UNDO_STATES or not _nullable(value["until"], _is_iso)
        or not _nullable(value["undo_request_id"], _is_uuid)
    ):
        _fail("undo")
    expected_until = state in {"available", "undone", "expired"}
    if (value["until"] is not None) != expected_until or (
        (value["undo_request_id"] is not None) != (state == "undone")
    ):
        _fail("undo")


def read_receipt_v2(value: Any) -> dict[str, Any]:
    """``receipt_v2``: also the body of a decision and of ``GET /receipts/{uuid}``."""
    _exact(value, (
        *_BINDING_KEYS, "observed_version", "status", "request_id", "proposal_id", "kind",
        "actor_source", "commit_version", "committed_at", "server_now", "records", "undo",
        "undo_of", "reason_codes",
    ))
    check_binding(value, observed=True)
    status = value["status"]
    if (
        status not in (*RECEIPT_STATUSES, "unknown") or not _is_uuid(value["request_id"])
        or not _reason_codes(value["reason_codes"]) or not _is_iso(value["server_now"])
        or not isinstance(value["records"], list)
    ):
        _fail("receipt_shape")
    if status == "unknown":
        if (
            any(value[key] is not None for key in (
                "proposal_id", "kind", "actor_source", "commit_version", "committed_at", "undo",
                "undo_of"))
            or value["records"] or value["reason_codes"] != ["receipt_not_found"]
        ):
            _fail("receipt_unknown")
        return value
    kind = value["kind"]
    if (
        not _is_id(value["proposal_id"]) or kind not in PROPOSAL_KINDS
        or value["actor_source"] not in ACTOR_SOURCES
        or not _is_int(value["commit_version"]) or value["commit_version"] < 1
        or not _nullable(value["undo_of"], _is_uuid) or (value["undo_of"] is not None) != (
            kind == "owner_undo")
    ):
        _fail("receipt_shape")
    if (value["committed_at"] is not None) != (status == "committed") or not _nullable(
        value["committed_at"], _is_iso
    ):
        _fail("receipt_committed_at")
    records = value["records"]
    if records and status not in {"committed", "target_changed"}:
        _fail("receipt_records")
    if status == "committed" and kind in _RECORD_KINDS and not records:
        _fail("receipt_records")
    for item in records:
        _exact(item, ("entity", "record"))
        if item["entity"] not in ENTITIES or not _is_loose_record(item["record"]):
            _fail("receipt_records")
    if (value["undo"] is not None) != (kind == "owner_direct"):
        _fail("receipt_undo")
    if value["undo"] is not None:
        _read_undo(value["undo"])
        if status != "committed" and value["undo"]["state"] != "none":
            _fail("receipt_undo")
    return value


_APPROVAL_KEYED = frozenset({"committed", "target_changed", "abandoned"})
_REJECTION_KEYED = frozenset({"rejected", "superseded"})


def read_proposal_request_v2(value: Any) -> dict[str, Any]:
    """``GET /proposal-requests/{uuid}``; its cross-check follows the receipt-key table."""
    _exact(value, (
        *_BINDING_KEYS, "observed_version", "request_id", "status", "proposal", "receipt",
        "reason_codes",
    ))
    check_binding(value, observed=True)
    if (
        not _is_uuid(value["request_id"]) or not _member(value["status"], ("found", "unknown"))
        or not _reason_codes(value["reason_codes"])
    ):
        _fail("proposal_request_shape")
    if value["status"] == "unknown":
        if (
            value["proposal"] is not None or value["receipt"] is not None
            or value["reason_codes"] != ["proposal_request_unknown"]
        ):
            _fail("proposal_request_unknown")
        return value
    proposal = value["proposal"]
    if value["reason_codes"]:
        _fail("proposal_request_shape")
    read_proposal_v2(proposal, statuses=PROPOSAL_REQUEST_STATUSES, kinds=PROPOSAL_KINDS)
    if proposal["request_id"] != value["request_id"]:
        _fail("proposal_request_id")
    _same_binding(proposal, value)
    receipt = value["receipt"]
    if receipt is None:
        if proposal["status"] != "pending":
            _fail("proposal_request_receipt")
        return value
    read_receipt_v2(receipt)
    _same_binding(receipt, value)
    status = proposal["status"]
    if (
        receipt["status"] == "unknown" or receipt["status"] != status
        or receipt["proposal_id"] != proposal["proposal_id"]
        or receipt["actor_source"] != proposal["actor_source"]
    ):
        _fail("proposal_request_receipt")
    approval, rejection = proposal["approval_request_id"], proposal["rejection_request_id"]
    allowed = (
        {approval} if status in _APPROVAL_KEYED
        else {rejection} if status in _REJECTION_KEYED
        else {approval, rejection} if status == "expired" else set()
    )
    if receipt["request_id"] not in allowed:
        _fail("proposal_request_receipt")
    return value


def read_deleted_page(value: Any) -> dict[str, Any]:
    """``GET /deleted``: the tombstoned rows of one entity."""
    _exact(value, (*_BINDING_KEYS, "observed_version", "entity", "records", "next_cursor"))
    check_binding(value, observed=True)
    if value["entity"] not in ENTITIES or not _nullable(value["next_cursor"], _is_token):
        _fail("deleted_shape")
    _read_records(value["records"])
    return value


READERS = {
    "schema": read_schema_v2,
    "snapshot": read_snapshot_v2,
    "pending": read_pending_list,
    "proposal": read_proposal_v2,
    "receipt": read_receipt_v2,
    "proposal_request": read_proposal_request_v2,
    "deleted": read_deleted_page,
}


# ---------------------------------------------------------------------------------------------
# Shape classification: the dispatch every ``*Any`` reader starts with

_V1_MARKERS = frozenset({"mode", "workbook_id", "entity_coverage", "pending_proposals", "issues"})
# A v1 receipt names its record at the top level; a v2 receipt only lists records.
_V1_RECEIPT_MARKERS = frozenset({"entity", "record_id", "revision"})
_V2_MARKERS = frozenset({
    "authority_epoch", "observed_version", "unchanged", "next_cursor", "since", "labels",
    "mirror", "server_now", "commit_version", "undo", "undo_of",
})


def body_shape(
    value: Any, *, context: Literal["any", "receipt"] = "any"
) -> Literal["v1", "v2", "mixed", "unknown"]:
    """Which contract a body speaks. A body with markers of both is ``mixed`` and never trusted."""
    if not _is_object(value):
        return "unknown"
    v1 = (
        bool(_V1_MARKERS & value.keys())
        or value.get("api_version") == V1_API_VERSION
        or value.get("source") == V1_SOURCE
        or isinstance(value.get("records"), dict)
        or (context == "receipt" and bool(_V1_RECEIPT_MARKERS & value.keys()))
    )
    v2 = (
        bool(_V2_MARKERS & value.keys())
        or value.get("api_version") == API_VERSION
        or value.get("source") == SOURCE
        or isinstance(value.get("records"), list)
    )
    if v1 and v2:
        return "mixed"
    return "v1" if v1 else "v2" if v2 else "unknown"
