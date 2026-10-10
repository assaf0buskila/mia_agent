"""Read-only, privacy-safe CRM reconciliation and operational diagnostics."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    CrmActivityRow,
    CrmContactRow,
    CrmFormIntakeReceiptRow,
    CrmIdentityRow,
    CrmIssueContactRow,
    CrmIssueRow,
    CrmOutboxRow,
    CrmSyncSnapshotRow,
    CrmWorkerStateRow,
    OwnerNotificationRecipientClaimRow,
)
from app.domain.handoff.delivery import (
    KIND_WEBSITE_HANDOFF_DELIVERY,
    WEBSITE_HANDOFF_DELIVERY_KINDS,
    form_ping_scope,
    website_ping_scope,
)
from app.services.crm_v2 import (
    CONTACT_FIELDS,
    CrmService,
    _activity_action_cell,
    _activity_outcome_cell,
)

MAX_DIAGNOSTIC_ROWS = 5_000
MAX_REPORT_ITEMS = 100
SHEET_PAGE_SIZE = 100

_SAFE_ID = re.compile(r"^(?:crm|activity|issue|job)_[0-9a-f]{32}$")
_SITE_SOURCE = re.compile(r"^site:([^:]{1,64}):")
_SAFE_STATE_KEYS = frozenset(
    {
        "contacts_last_import",
        "crm_worker_heartbeat",
        "crm_last_completed_cycle",
        "crm_last_successful_cycle",
        "crm_last_successful_delivery",
        "crm_last_successful_sheets_projection",
    }
)
_ISSUE_TYPES = frozenset(
    {
        "destination_identity_collision",
        "duplicate_sheet_id",
        "field_conflict",
        "identity_collision",
        "legacy_binding_changed",
        "missing_contact",
        "missing_sheet_row",
    }
)
_ISSUE_STATUSES = frozenset({"open", "resolving", "resolved"})
_OUTBOX_STATUSES = ("pending", "in_flight", "confirmed", "failed", "unknown", "conflict")
_DESTINATIONS = ("contacts", "activity", "telegram")
_UNDELIVERED = frozenset({"pending", "in_flight", "failed", "unknown", "conflict"})


class ReadOnlyCrmSheetsPort(Protocol):
    def read_crm_contacts_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]: ...

    def read_crm_activity_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]: ...


def _safe_id(value: object) -> str:
    raw = str(value or "")
    if _SAFE_ID.fullmatch(raw):
        return raw
    return "opaque_" + sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _safe_timestamp(value: object) -> str | None:
    raw = str(value or "")
    if not raw or len(raw) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _age_seconds(value: object, now: datetime) -> int | None:
    stamp = _safe_timestamp(value)
    if stamp is None:
        return None
    return max(0, int((now - datetime.fromisoformat(stamp)).total_seconds()))


def _load_json_object(raw: object) -> dict[str, Any] | None:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _sheet_extent(sheets: object, sheet_kind: str) -> tuple[int | None, str]:
    """Return a trustworthy last row only when the adapter explicitly supplies one."""
    reader = getattr(sheets, "crm_sheet_row_extent", None)
    if not callable(reader):
        return None, "unavailable"
    try:
        extent = reader(sheet_kind=sheet_kind)
    except Exception:  # provider text is never surfaced
        return None, "read_failed"
    if not isinstance(extent, int) or isinstance(extent, bool) or extent < 1:
        return None, "invalid"
    return extent, "available"


def _read_sheet_rows(
    sheets: object,
    reader: Callable[..., list[list[str]]],
    *,
    sheet_kind: str,
    width: int,
    id_index: int,
    max_rows: int,
    page_size: int,
) -> dict[str, Any]:
    """Read every bounded range, including ranges whose response is short or empty."""
    rows_by_id: dict[str, list[list[str]]] = defaultdict(list)
    malformed_ids = 0
    rows_seen = 0
    ranges_read = 0
    extent, extent_status = _sheet_extent(sheets, sheet_kind)
    last_row = min(max_rows + 1, extent) if extent is not None else max_rows + 1
    status = "complete" if extent is not None and extent <= max_rows + 1 else "partial"
    if extent is not None and extent > max_rows + 1:
        status = "truncated"
    start_row = 2
    while start_row <= last_row:
        limit = min(page_size, last_row - start_row + 1)
        try:
            chunk = reader(start_row=start_row, limit=limit)
        except Exception:
            status = "read_failed"
            break
        ranges_read += 1
        if not isinstance(chunk, list) or len(chunk) > limit:
            status = "read_failed"
            break
        for raw_row in chunk:
            if not isinstance(raw_row, list):
                malformed_ids += 1
                continue
            row = [str(cell or "") for cell in raw_row[:width]]
            row.extend([""] * (width - len(row)))
            if not any(row):
                continue
            raw_id = row[id_index]
            if not _SAFE_ID.fullmatch(raw_id):
                malformed_ids += 1
            rows_by_id[_safe_id(raw_id)].append(row)
            rows_seen += 1
        # A short Values response says only that this requested range was sparse.
        # Advance by the requested range, never by the number of returned rows.
        start_row += limit
    return {
        "status": status,
        "complete": status == "complete",
        "extent_status": extent_status,
        "extent_row": extent if extent_status == "available" else None,
        "ranges_read": ranges_read,
        "rows_seen": rows_seen,
        "malformed_ids": malformed_ids,
        "rows_by_id": rows_by_id,
    }


def _issue_summary(
    session: Session,
    *,
    contact_sheet: Mapping[str, Any],
    projected_fields: Mapping[str, Mapping[str, str]],
    max_items: int,
) -> dict[str, Any]:
    raw_statuses = Counter()
    for raw_status, count in session.execute(
        select(CrmIssueRow.status, func.count()).group_by(CrmIssueRow.status)
    ):
        status = raw_status if raw_status in _ISSUE_STATUSES else "unknown"
        raw_statuses[status] += int(count)

    issue_rows: dict[str, dict[str, Any]] = {}
    statement = (
        select(
            CrmIssueRow.id,
            CrmIssueRow.contact_id,
            CrmIssueRow.issue_type,
            CrmIssueRow.field_name,
            CrmIssueRow.base_value,
            CrmIssueRow.database_value,
            CrmIssueRow.sheet_value,
            CrmIssueRow.details_json,
            CrmIssueRow.status,
            CrmIssueRow.created_at,
            CrmIssueContactRow.contact_id.label("linked_contact_id"),
        )
        .outerjoin(CrmIssueContactRow, CrmIssueContactRow.issue_id == CrmIssueRow.id)
        .where(CrmIssueRow.status.in_(("open", "resolving")))
        .order_by(CrmIssueRow.id, CrmIssueContactRow.contact_id)
    )
    for row in session.execute(statement).yield_per(1_000):
        item = issue_rows.setdefault(
            row.id,
            {
                "issue_type_raw": row.issue_type,
                "field_name_raw": row.field_name,
                "base": row.base_value,
                "database": row.database_value,
                "sheet": row.sheet_value,
                "details": row.details_json,
                "status": row.status,
                "created_at": row.created_at,
                "contacts": set(),
            },
        )
        if row.contact_id:
            item["contacts"].add(_safe_id(row.contact_id))
        if row.linked_contact_id:
            item["contacts"].add(_safe_id(row.linked_contact_id))

    groups: dict[str, dict[str, Any]] = {}
    affected: set[str] = set()
    for issue in issue_rows.values():
        issue_type = (
            issue["issue_type_raw"] if issue["issue_type_raw"] in _ISSUE_TYPES else "unknown_issue"
        )
        field_name = issue["field_name_raw"] if issue["field_name_raw"] in CONTACT_FIELDS else ""
        related_ids = sorted(issue["contacts"])
        affected.update(related_ids)
        digest = sha256(
            json.dumps(
                {
                    "type": issue_type,
                    "field": field_name,
                    "contacts": related_ids,
                    "base": issue["base"],
                    "database": issue["database"],
                    "sheet": issue["sheet"],
                    "details": issue["details"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:16]
        group = groups.setdefault(
            digest,
            {
                "observation_digest": digest,
                "issue_type": issue_type,
                "field_name": field_name,
                "status_counts": {"open": 0, "resolving": 0},
                "related_contact_ids": related_ids,
                "duplicate_row_count": 0,
                "first_observed_at": _safe_timestamp(issue["created_at"]),
                "last_observed_at": _safe_timestamp(issue["created_at"]),
                "recommended_action_code": (
                    "resolve_field_conflict"
                    if issue_type == "field_conflict"
                    else "inspect_identity_collision"
                    if "identity" in issue_type
                    else "inspect_projection_issue"
                ),
            },
        )
        group["duplicate_row_count"] += 1
        group["status_counts"][issue["status"]] += 1
        stamp = _safe_timestamp(issue["created_at"])
        if stamp:
            if group["first_observed_at"] is None or stamp < group["first_observed_at"]:
                group["first_observed_at"] = stamp
            if group["last_observed_at"] is None or stamp > group["last_observed_at"]:
                group["last_observed_at"] = stamp

    for group in groups.values():
        evidence = "uncertain"
        related_ids = group["related_contact_ids"]
        if len(related_ids) == 1:
            contact_id = related_ids[0]
            rows = contact_sheet["rows_by_id"].get(contact_id, [])
            if group["issue_type"] == "missing_sheet_row":
                if rows:
                    evidence = "not_currently_observed"
                elif contact_sheet["complete"]:
                    evidence = "corroborated"
            elif group["issue_type"] == "duplicate_sheet_id":
                if len(rows) > 1:
                    evidence = "corroborated"
                elif contact_sheet["complete"]:
                    evidence = "not_currently_observed"
            elif group["issue_type"] == "field_conflict" and len(rows) == 1:
                field = group["field_name"]
                fields = projected_fields.get(contact_id)
                if fields is not None and field:
                    evidence = (
                        "corroborated"
                        if rows[0][CONTACT_FIELDS.index(field)] != fields.get(field, "")
                        else "not_currently_observed"
                    )
        group["current_sheet_evidence"] = evidence

    ordered = sorted(
        groups.values(),
        key=lambda item: (item["last_observed_at"] or "", item["observation_digest"]),
        reverse=True,
    )
    raw_rows = sum(raw_statuses.values())
    unresolved_rows = raw_statuses["open"] + raw_statuses["resolving"]
    return {
        "aggregation_complete": True,
        "raw_rows": raw_rows,
        "resolved_rows": raw_statuses["resolved"],
        "other_status_rows": raw_rows - unresolved_rows - raw_statuses["resolved"],
        "raw_status_counts": dict(sorted(raw_statuses.items())),
        "distinct_unresolved_observations": len(groups),
        "unresolved_rows": unresolved_rows,
        "repeated_unresolved_observation_rows": max(0, unresolved_rows - len(groups)),
        "affected_contact_count": len(affected),
        "affected_contact_ids": sorted(affected)[:max_items],
        "affected_contact_ids_truncated": len(affected) > max_items,
        "groups": ordered[:max_items],
        "groups_truncated": len(ordered) > max_items,
    }


def _outbox_summary(session: Session, now: datetime) -> dict[str, Any]:
    counts = {
        destination: {status: 0 for status in _OUTBOX_STATUSES} for destination in _DESTINATIONS
    }
    for destination, status, count in session.execute(
        select(CrmOutboxRow.destination, CrmOutboxRow.status, func.count()).group_by(
            CrmOutboxRow.destination, CrmOutboxRow.status
        )
    ):
        if destination in counts and status in counts[destination]:
            counts[destination][status] = int(count)
    oldest = {destination: None for destination in _DESTINATIONS}
    oldest_known = {destination: True for destination in _DESTINATIONS}
    for destination, created_at in session.execute(
        select(CrmOutboxRow.destination, func.min(CrmOutboxRow.created_at))
        .where(CrmOutboxRow.status.in_(_UNDELIVERED))
        .group_by(CrmOutboxRow.destination)
    ):
        if destination not in oldest:
            continue
        oldest[destination] = _age_seconds(created_at, now)
        oldest_known[destination] = oldest[destination] is not None
    return {
        "aggregation_complete": True,
        "by_destination": counts,
        "oldest_undelivered_age_seconds": oldest,
        "oldest_undelivered_known": oldest_known,
    }


def _form_summary(
    session: Session, *, max_items: int
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    valid = 0
    missing_contact = 0
    missing_activity = 0
    semantic_mismatch = 0
    mismatch_receipts: list[str] = []
    intents: list[dict[str, str]] = []
    statement = (
        select(
            CrmFormIntakeReceiptRow.source_id,
            CrmFormIntakeReceiptRow.contact_id,
            CrmFormIntakeReceiptRow.activity_id.label("receipt_activity_id"),
            CrmActivityRow.id.label("existing_activity_id"),
            CrmActivityRow.contact_id.label("activity_contact_id"),
            CrmActivityRow.source_ref,
            CrmActivityRow.channel,
            CrmActivityRow.action,
            CrmContactRow.id.label("existing_contact_id"),
        )
        .outerjoin(CrmActivityRow, CrmActivityRow.id == CrmFormIntakeReceiptRow.activity_id)
        .outerjoin(CrmContactRow, CrmContactRow.id == CrmFormIntakeReceiptRow.contact_id)
        .order_by(CrmFormIntakeReceiptRow.source_id)
    )
    receipts = 0
    for row in session.execute(statement).yield_per(1_000):
        receipts += 1
        intents.append(
            {
                "conversation": f"form:{row.source_id}",
                "contact_id": _safe_id(row.contact_id),
                "activity_id": _safe_id(row.receipt_activity_id),
            }
        )
        if row.existing_contact_id is None:
            missing_contact += 1
        if row.existing_activity_id is None:
            missing_activity += 1
            mismatch_receipts.append(_safe_id(row.source_id))
            continue
        expected_source = f"assafweb-form:{row.source_id}:activity"
        semantic_ok = (
            row.activity_contact_id == row.contact_id
            and row.source_ref == expected_source
            and row.channel == "form"
            and row.action == "contact_captured"
        )
        if semantic_ok and row.existing_contact_id is not None:
            valid += 1
        else:
            semantic_mismatch += 1
            mismatch_receipts.append(_safe_id(row.source_id))
    return (
        {
            "aggregation_complete": True,
            "receipts": receipts,
            "valid_correspondence": valid,
            "receipts_missing_contact": missing_contact,
            "receipts_missing_activity": missing_activity,
            "semantic_mismatch": semantic_mismatch,
            "mismatch_receipt_ids": mismatch_receipts[:max_items],
            "mismatch_receipt_ids_truncated": len(mismatch_receipts) > max_items,
            "rejected_count": None,
            "conflicted_count": None,
            "reject_conflict_measurement": "unmeasured",
        },
        intents,
    )


def _site_intents(session: Session) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    intents: dict[str, dict[str, str]] = {}
    unsupported: list[dict[str, str]] = []
    rows = session.execute(
        select(CrmActivityRow.id, CrmActivityRow.contact_id, CrmActivityRow.source_ref)
        .where(
            CrmActivityRow.channel == "website",
            CrmActivityRow.action == "contact_captured",
        )
        .order_by(CrmActivityRow.created_at, CrmActivityRow.id)
    )
    for activity_id, contact_id, source_ref in rows.yield_per(1_000):
        match = _SITE_SOURCE.match(str(source_ref or ""))
        if match:
            intents.setdefault(
                match.group(1),
                {
                    "conversation": match.group(1),
                    "contact_id": _safe_id(contact_id),
                    "activity_id": _safe_id(activity_id),
                },
            )
        else:
            unsupported.append(
                {
                    "contact_id": _safe_id(contact_id),
                    "activity_id": _safe_id(activity_id),
                }
            )
    return [intents[key] for key in sorted(intents)], unsupported


def _notification_summary(
    session: Session,
    *,
    form_intents: list[dict[str, str]],
    expected_recipient_ids: frozenset[str] | None,
    max_items: int,
) -> dict[str, Any]:
    if not expected_recipient_ids:
        return {
            "status": "unknown_config",
            "aggregation_complete": False,
            "expected_intents": None,
            "jobs_present": None,
            "jobs_missing": None,
            "receipts_confirmed": None,
            "receipts_missing": None,
            "receipts_unresolved": None,
            "legacy_claims_unknown": None,
            "coverage_scope": "primary_handoff_only",
            "update_intent_measurement": "unmeasured",
        }
    site_intents, unsupported_site = _site_intents(session)
    source_intents: dict[str, dict[str, str]] = {}
    for intent in (*form_intents, *site_intents):
        source_intents.setdefault(intent["conversation"], intent)
    expected: dict[tuple[str, str, str], dict[str, str]] = {}
    unsupported_scopes = 0
    for conversation, intent in sorted(source_intents.items()):
        try:
            lead_id, key = (
                form_ping_scope(conversation)
                if conversation.startswith("form:")
                else website_ping_scope(conversation)
            )
        except (AssertionError, ValueError):
            unsupported_scopes += 1
            continue
        for recipient in sorted(expected_recipient_ids):
            expected[(lead_id, key, recipient)] = intent

    jobs: dict[tuple[str, str], list[str]] = defaultdict(list)
    observed_update_jobs = 0
    for payload_json, status in session.execute(
        select(CrmOutboxRow.payload_json, CrmOutboxRow.status).where(
            CrmOutboxRow.destination == "telegram"
        )
    ).yield_per(1_000):
        payload = _load_json_object(payload_json)
        if payload is None:
            continue
        if payload.get("ping_scope"):
            observed_update_jobs += 1
            continue
        conversation = str(payload.get("conversation_id") or "")
        recipient = str(payload.get("recipient_id") or "")
        if conversation and recipient.isdigit():
            jobs[(conversation, recipient)].append(
                status if status in _OUTBOX_STATUSES else "unknown_status"
            )

    claims: dict[tuple[str, str, str], list[tuple[str, str]]] = defaultdict(list)
    for claim in session.scalars(select(OwnerNotificationRecipientClaimRow)).yield_per(1_000):
        claims[(claim.lead_id, claim.notification_key, claim.recipient_id)].append(
            (claim.kind, claim.delivery_status)
        )

    counts = Counter(
        {
            "jobs_present": 0,
            "jobs_missing": 0,
            "receipts_confirmed": 0,
            "receipts_missing": 0,
            "receipts_unresolved": 0,
            "legacy_claims_unknown": 0,
            "not_attempted": 0,
        }
    )
    job_statuses = Counter()
    attention: dict[tuple[str, str], dict[str, str]] = {}
    for scope, intent in expected.items():
        lead_id, key, recipient = scope
        conversation = intent["conversation"]
        matching_jobs = jobs.get((conversation, recipient), [])
        if matching_jobs:
            counts["jobs_present"] += 1
            job_statuses.update(matching_jobs)
        else:
            counts["jobs_missing"] += 1
            attention[(intent["contact_id"], intent["activity_id"])] = intent
        matching_claims = claims.get((lead_id, key, recipient), [])
        current = [
            status for kind, status in matching_claims if kind == KIND_WEBSITE_HANDOFF_DELIVERY
        ]
        legacy = [
            status
            for kind, status in matching_claims
            if kind in WEBSITE_HANDOFF_DELIVERY_KINDS and kind != KIND_WEBSITE_HANDOFF_DELIVERY
        ]
        if "accepted" in current:
            counts["receipts_confirmed"] += 1
        elif current:
            counts["receipts_unresolved"] += 1
            attention[(intent["contact_id"], intent["activity_id"])] = intent
        elif legacy:
            counts["legacy_claims_unknown"] += 1
            attention[(intent["contact_id"], intent["activity_id"])] = intent
        elif matching_jobs and any(
            status in {"confirmed", "unknown", "in_flight"} for status in matching_jobs
        ):
            counts["receipts_missing"] += 1
            attention[(intent["contact_id"], intent["activity_id"])] = intent
        else:
            counts["not_attempted"] += 1
            attention[(intent["contact_id"], intent["activity_id"])] = intent
    return {
        "status": "measured",
        "aggregation_complete": True,
        "expected_intents": len(expected),
        **dict(counts),
        "job_status_counts": dict(sorted(job_statuses.items())),
        "coverage_scope": "primary_handoff_only",
        "update_intent_measurement": "unmeasured",
        "observed_update_jobs": observed_update_jobs,
        "unsupported_source_scope_count": unsupported_scopes + len(unsupported_site),
        "unsupported_source_activity_ids": [
            item["activity_id"] for item in unsupported_site[:max_items]
        ],
        "unsupported_source_ids_truncated": len(unsupported_site) > max_items,
        "attention_contact_ids": sorted({item["contact_id"] for item in attention.values()})[
            :max_items
        ],
        "attention_activity_ids": sorted({item["activity_id"] for item in attention.values()})[
            :max_items
        ],
        "attention_ids_truncated": len(attention) > max_items,
    }


def _phone_readiness(
    session: Session,
    normalizer: Callable[[str], str] | None,
    *,
    max_items: int,
) -> dict[str, Any]:
    if normalizer is None:
        return {
            "status": "unavailable",
            "legacy_keys_requiring_migration": None,
            "invalid_keys": None,
            "cross_contact_equivalence_groups": [],
        }
    migration = 0
    invalid = 0
    grouped: dict[str, set[str]] = defaultdict(set)
    for normalized_value, contact_id in session.execute(
        select(CrmIdentityRow.normalized_value, CrmIdentityRow.contact_id).where(
            CrmIdentityRow.kind == "phone"
        )
    ).yield_per(1_000):
        try:
            canonical = normalizer(normalized_value)
        except (TypeError, ValueError):
            canonical = ""
        if not canonical:
            invalid += 1
            continue
        if canonical != normalized_value:
            migration += 1
        grouped[canonical].add(_safe_id(contact_id))
    collisions = sorted(
        (sorted(contact_ids) for contact_ids in grouped.values() if len(contact_ids) > 1),
        key=lambda ids: tuple(ids),
    )
    return {
        "status": "measured",
        "aggregation_complete": True,
        "legacy_keys_requiring_migration": migration,
        "invalid_keys": invalid,
        "cross_contact_equivalence_group_count": len(collisions),
        "cross_contact_equivalence_groups": collisions[:max_items],
        "groups_truncated": len(collisions) > max_items,
    }


def build_crm_diagnostics(
    session: Session,
    *,
    sheets: ReadOnlyCrmSheetsPort | None = None,
    now: datetime | None = None,
    max_rows: int = MAX_DIAGNOSTIC_ROWS,
    max_items: int = MAX_REPORT_ITEMS,
    phone_normalizer: Callable[[str], str] | None = None,
    recipient_ids: Sequence[str] | set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Return a deterministic sanitized snapshot without flushing or mutating data."""
    bounded_rows = max(1, min(int(max_rows), MAX_DIAGNOSTIC_ROWS))
    bounded_items = max(1, min(int(max_items), MAX_REPORT_ITEMS))
    observed_at = now or datetime.now(UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    observed_at = observed_at.astimezone(UTC)

    with session.no_autoflush:
        contact_total = int(session.scalar(select(func.count()).select_from(CrmContactRow)) or 0)
        activity_total = int(session.scalar(select(func.count()).select_from(CrmActivityRow)) or 0)
        contacts = session.scalars(
            select(CrmContactRow).order_by(CrmContactRow.id).limit(bounded_rows)
        ).all()
        activities = session.scalars(
            select(CrmActivityRow).order_by(CrmActivityRow.id).limit(bounded_rows)
        ).all()
        snapshots = session.scalars(
            select(CrmSyncSnapshotRow)
            .where(CrmSyncSnapshotRow.contact_id.in_([row.id for row in contacts]))
            .order_by(CrmSyncSnapshotRow.contact_id, CrmSyncSnapshotRow.field_name)
        ).all()
        states = session.scalars(
            select(CrmWorkerStateRow).where(CrmWorkerStateRow.key.in_(_SAFE_STATE_KEYS))
        ).all()

        if sheets is None:
            contact_sheet = {
                "status": "not_requested",
                "complete": False,
                "extent_status": "not_requested",
                "extent_row": None,
                "ranges_read": 0,
                "rows_seen": 0,
                "malformed_ids": 0,
                "rows_by_id": {},
            }
            activity_sheet = dict(contact_sheet)
        else:
            contact_sheet = _read_sheet_rows(
                sheets,
                sheets.read_crm_contacts_chunk,
                sheet_kind="contacts",
                width=15,
                id_index=14,
                max_rows=bounded_rows,
                page_size=SHEET_PAGE_SIZE,
            )
            activity_sheet = _read_sheet_rows(
                sheets,
                sheets.read_crm_activity_chunk,
                sheet_kind="activity",
                width=6,
                id_index=5,
                max_rows=bounded_rows,
                page_size=SHEET_PAGE_SIZE,
            )

        snapshot_map = {(row.contact_id, row.field_name): row for row in snapshots}
        service = CrmService(session, normalize_israeli_phones=False)
        contact_differences: list[dict[str, Any]] = []
        db_contact_ids: set[str] = set()
        projected_fields: dict[str, dict[str, str]] = {}
        for row in contacts:
            safe_contact_id = _safe_id(row.id)
            db_contact_ids.add(safe_contact_id)
            view = service._contact_view(row)
            projected_fields[safe_contact_id] = view.fields
            snapshot_diff = sorted(
                name
                for name in CONTACT_FIELDS
                if snapshot_map.get((row.id, name)) is None
                or snapshot_map[(row.id, name)].value != view.fields.get(name, "")
            )
            snapshot_revisions = sorted(
                {
                    snapshot_map[(row.id, name)].contact_revision
                    for name in CONTACT_FIELDS
                    if (row.id, name) in snapshot_map
                }
            )
            snapshot_revision_drift = snapshot_revisions != [row.revision]
            sheet_rows = contact_sheet["rows_by_id"].get(safe_contact_id, [])
            sheet_diff: list[str] = []
            sheet_state = "unavailable"
            if sheet_rows:
                sheet_state = "duplicate" if len(sheet_rows) > 1 else "present"
                sheet_diff = sorted(
                    name
                    for index, name in enumerate(CONTACT_FIELDS)
                    if sheet_rows[0][index] != view.fields.get(name, "")
                )
            elif contact_sheet["complete"]:
                sheet_state = "missing"
            if (
                snapshot_diff
                or snapshot_revision_drift
                or sheet_diff
                or sheet_state in {"missing", "duplicate"}
            ):
                contact_differences.append(
                    {
                        "contact_id": safe_contact_id,
                        "revision": row.revision,
                        "snapshot_revisions": snapshot_revisions,
                        "snapshot_revision_drift": snapshot_revision_drift,
                        "database_vs_snapshot_fields": snapshot_diff,
                        "database_vs_sheet_fields": sheet_diff,
                        "sheet_state": sheet_state,
                        "uncertain": not contact_sheet["complete"],
                        "recommended_action_code": (
                            "inspect_duplicate_sheet_id"
                            if sheet_state == "duplicate"
                            else "inspect_missing_sheet_row"
                            if sheet_state == "missing"
                            else "inspect_field_drift"
                        ),
                    }
                )

        db_activity_ids: set[str] = set()
        activity_differences: list[dict[str, Any]] = []
        activity_fields = ("occurred_at", "who", "channel", "action", "result")
        for row in activities:
            safe_activity_id = _safe_id(row.id)
            db_activity_ids.add(safe_activity_id)
            view = service._activity_view(row)
            expected_cells = (
                view.occurred_at,
                view.who,
                view.channel,
                _activity_action_cell(view.action),
                _activity_outcome_cell(view.action, view.result),
            )
            sheet_rows = activity_sheet["rows_by_id"].get(safe_activity_id, [])
            sheet_state = "unavailable"
            sheet_diff: list[str] = []
            if sheet_rows:
                sheet_state = "duplicate" if len(sheet_rows) > 1 else "present"
                sheet_diff = sorted(
                    name
                    for index, name in enumerate(activity_fields)
                    if sheet_rows[0][index] != expected_cells[index]
                )
            elif activity_sheet["complete"]:
                sheet_state = "missing"
            if sheet_diff or sheet_state in {"missing", "duplicate"}:
                activity_differences.append(
                    {
                        "activity_id": safe_activity_id,
                        "contact_id": _safe_id(view.contact_id),
                        "database_vs_sheet_fields": sheet_diff,
                        "sheet_state": sheet_state,
                        "uncertain": not activity_sheet["complete"],
                        "recommended_action_code": (
                            "inspect_duplicate_sheet_id"
                            if sheet_state == "duplicate"
                            else "inspect_missing_sheet_row"
                            if sheet_state == "missing"
                            else "inspect_field_drift"
                        ),
                    }
                )
        missing_activity = (
            sorted(db_activity_ids - set(activity_sheet["rows_by_id"]))
            if activity_sheet["complete"]
            else []
        )
        sheet_only_contact_ids = (
            sorted(set(contact_sheet["rows_by_id"]) - db_contact_ids)
            if contact_total <= bounded_rows
            else []
        )
        sheet_only_activity_ids = (
            sorted(set(activity_sheet["rows_by_id"]) - db_activity_ids)
            if activity_total <= bounded_rows
            else []
        )

        issues = _issue_summary(
            session,
            contact_sheet=contact_sheet,
            projected_fields=projected_fields,
            max_items=bounded_items,
        )
        outbox = _outbox_summary(session, observed_at)
        form_intake, form_intents = _form_summary(session, max_items=bounded_items)
        notifications = _notification_summary(
            session,
            form_intents=form_intents,
            expected_recipient_ids=(
                frozenset(str(value) for value in recipient_ids if str(value).isdigit())
                if recipient_ids is not None
                else None
            ),
            max_items=bounded_items,
        )

        state_report = {
            row.key: {
                "timestamp": _safe_timestamp(row.updated_at),
                "age_seconds": _age_seconds(row.updated_at, observed_at),
                "state": (
                    row.value
                    if row.key == "crm_worker_heartbeat"
                    and row.value in {"running", "completed", "completed_with_failures", "failed"}
                    else "recorded"
                ),
            }
            for row in states
        }
        heartbeat = state_report.get("crm_worker_heartbeat")
        heartbeat_status = "unknown"
        if heartbeat and heartbeat["age_seconds"] is not None:
            heartbeat_status = "stale" if heartbeat["age_seconds"] > 60 else heartbeat["state"]

        return {
            "schema_version": 2,
            "observed_at": observed_at.isoformat(),
            "read_only": True,
            "database": {
                "contacts": contact_total,
                "activities": activity_total,
                "reconciliation_rows_bounded": (
                    contact_total > bounded_rows or activity_total > bounded_rows
                ),
            },
            "sheets": {
                "contacts": {k: v for k, v in contact_sheet.items() if k != "rows_by_id"},
                "activity": {k: v for k, v in activity_sheet.items() if k != "rows_by_id"},
            },
            "reconciliation": {
                "contact_differences": contact_differences[:bounded_items],
                "contact_differences_truncated": len(contact_differences) > bounded_items,
                "activity_differences": activity_differences[:bounded_items],
                "activity_differences_truncated": len(activity_differences) > bounded_items,
                "missing_activity_ids": missing_activity[:bounded_items],
                "missing_activity_ids_truncated": len(missing_activity) > bounded_items,
                "sheet_only_contact_ids": sheet_only_contact_ids[:bounded_items],
                "sheet_only_contact_ids_truncated": len(sheet_only_contact_ids) > bounded_items,
                "sheet_only_activity_ids": sheet_only_activity_ids[:bounded_items],
                "sheet_only_activity_ids_truncated": len(sheet_only_activity_ids) > bounded_items,
                "missing_claims_are_uncertain": (
                    contact_total > bounded_rows
                    or activity_total > bounded_rows
                    or not contact_sheet["complete"]
                    or not activity_sheet["complete"]
                ),
            },
            "outbox": outbox,
            "notifications": notifications,
            "form_intake": form_intake,
            "issues": issues,
            "worker": {
                "heartbeat_status": heartbeat_status,
                "states": dict(sorted(state_report.items())),
            },
            "phone_normalization_readiness": _phone_readiness(
                session,
                phone_normalizer,
                max_items=bounded_items,
            ),
        }


def render_operational_health_he(report: Mapping[str, Any], *, max_chars: int = 2_800) -> str:
    """Bounded Hebrew owner summary containing no customer or provider values."""
    worker = report.get("worker") if isinstance(report.get("worker"), Mapping) else {}
    outbox = report.get("outbox") if isinstance(report.get("outbox"), Mapping) else {}
    issues = report.get("issues") if isinstance(report.get("issues"), Mapping) else {}
    forms = report.get("form_intake") if isinstance(report.get("form_intake"), Mapping) else {}
    notifications = (
        report.get("notifications") if isinstance(report.get("notifications"), Mapping) else {}
    )
    sheets = report.get("sheets") if isinstance(report.get("sheets"), Mapping) else {}
    database = report.get("database") if isinstance(report.get("database"), Mapping) else {}
    reconciliation = (
        report.get("reconciliation") if isinstance(report.get("reconciliation"), Mapping) else {}
    )
    states = worker.get("states") if isinstance(worker.get("states"), Mapping) else {}

    def age_label(key: str) -> str:
        state = states.get(key)
        if not isinstance(state, Mapping) or state.get("age_seconds") is None:
            return "לא ידוע"
        return f"לפני {int(state['age_seconds'])} שניות"

    lines = [
        "מצב תפעולי של CRM (קריאה בלבד):",
        f"- פעימת עובד: {worker.get('heartbeat_status', 'unknown')}",
        f"- מחזור שהושלם לאחרונה: {age_label('crm_last_completed_cycle')}",
        f"- מחזור נקי אחרון: {age_label('crm_last_successful_cycle')}",
        f"- מסירה מוצלחת אחרונה: {age_label('crm_last_successful_delivery')}",
        f"- הקרנת Sheets אחרונה: {age_label('crm_last_successful_sheets_projection')}",
        f"- יבוא Contacts אחרון: {age_label('contacts_last_import')}",
        (
            f"- תקלות: {int(issues.get('raw_rows') or 0)} שורות שמורות, "
            f"{int(issues.get('resolved_rows') or 0)} פתורות, "
            f"{int(issues.get('unresolved_rows') or 0)} שורות לא פתורות, "
            f"מהן {int(issues.get('repeated_unresolved_observation_rows') or 0)} חזרות; "
            f"{int(issues.get('distinct_unresolved_observations') or 0)} תצפיות פתוחות ייחודיות; "
            f"{int(issues.get('affected_contact_count') or 0)} אנשי קשר מושפעים"
        ),
        (
            f"- קבלות טופס: {int(forms.get('receipts') or 0)}; "
            f"תקינות {int(forms.get('valid_correspondence') or 0)}, "
            f"אי התאמות {int(forms.get('semantic_mismatch') or 0)}, "
            f"ללא פעילות {int(forms.get('receipts_missing_activity') or 0)}"
        ),
    ]
    notification_status = notifications.get("status", "unknown")
    if notification_status == "measured":
        lines.append(
            f"- הודעות צפויות: {int(notifications.get('expected_intents') or 0)}; "
            f"עבודות חסרות {int(notifications.get('jobs_missing') or 0)}, "
            f"קבלות מאושרות {int(notifications.get('receipts_confirmed') or 0)}, "
            f"חסרות {int(notifications.get('receipts_missing') or 0)}, "
            f"לא מוכרעות {int(notifications.get('receipts_unresolved') or 0)}, "
            f"מורשת לא ודאית {int(notifications.get('legacy_claims_unknown') or 0)}"
        )
        lines.append(
            "- כיסוי הודעות: מסירת handoff ראשית בלבד; "
            f"עדכונים נצפים {int(notifications.get('observed_update_jobs') or 0)}, "
            "כוונת עדכונים חסרה אינה ניתנת למדידה; "
            f"מקורות ללא scope נתמך {int(notifications.get('unsupported_source_scope_count') or 0)}"
        )
    else:
        lines.append("- כיסוי הודעות: לא ידוע, רשימת נמענים מאושרת אינה זמינה")
    for destination in _DESTINATIONS:
        by_destination = outbox.get("by_destination")
        counts = by_destination.get(destination) if isinstance(by_destination, Mapping) else None
        oldest = outbox.get("oldest_undelivered_age_seconds")
        age = oldest.get(destination) if isinstance(oldest, Mapping) else None
        if isinstance(counts, Mapping):
            status_text = ", ".join(
                f"{status}={int(counts.get(status) or 0)}" for status in _OUTBOX_STATUSES
            )
            age_text = f"{int(age)} שניות" if age is not None else "לא ידוע"
            lines.append(f"- תור {destination}: {status_text}; הוותיק שלא הושלם: {age_text}")
    for kind in ("contacts", "activity"):
        sheet = sheets.get(kind) if isinstance(sheets, Mapping) else None
        if isinstance(sheet, Mapping):
            lines.append(
                f"- קריאת Sheet {kind}: {sheet.get('status', 'unknown')}; "
                f"שלמות {('מוכחת' if sheet.get('complete') else 'לא ידועה')}; "
                f"שורות מחוץ לסריקה {('לא' if sheet.get('complete') else 'אפשריות')}"
            )
    if forms.get("reject_conflict_measurement") == "unmeasured":
        lines.append("- דחיות והתנגשויות קליטת טופס: אינן נמדדות כרגע")
    if database.get("reconciliation_rows_bounded"):
        lines.append("- ספירות מסד הנתונים מלאות; פירוט ההתאמות נסרק במדגם מוגבל")
    if reconciliation.get("contact_differences_truncated") or reconciliation.get(
        "activity_differences_truncated"
    ):
        lines.append("- רשימת הבדלי ההתאמה קוצרה; הספירות המצטברות לא קוצרו")
    if issues.get("groups_truncated"):
        lines.append("- רשימת קבוצות התקלה קוצרה; ספירות התקלה המלאות מוצגות")
    return "\n".join(lines)[:max_chars]
