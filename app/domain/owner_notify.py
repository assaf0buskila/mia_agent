"""Owner meeting notification inbox (persist-only; no proactive send)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.meeting_slots import normalize_scheduled_at_utc, to_utc_aware
from app.integrations.calendar import format_slot_time

if TYPE_CHECKING:
    from app.db.store import LeadStore

KIND_MEETING_BOOKED = "meeting_booked"
KIND_MEETING_RESCHEDULED = "meeting_rescheduled"
KIND_MEETING_CANCELLATION_REQUESTED = "meeting_cancellation_requested"

OWNER_NOTIFY_KINDS = (
    KIND_MEETING_BOOKED,
    KIND_MEETING_RESCHEDULED,
    KIND_MEETING_CANCELLATION_REQUESTED,
)

_KIND_FIRST_LINES = {
    KIND_MEETING_BOOKED: "נקבעה פגישה.",
    KIND_MEETING_RESCHEDULED: "פגישה עודכנה.",
    KIND_MEETING_CANCELLATION_REQUESTED: "בקשת ביטול.",
}

_EMPTY_ACK = "אין התראות פגישות חדשות."
_MAX_RETURN = 3


def _utc_iso_now(now: datetime | None) -> str:
    clock = now or datetime.now(UTC)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)
    utc = clock.astimezone(UTC).replace(microsecond=0)
    return utc.isoformat()


def _format_when(scheduled_at: str, timezone: str) -> str:
    normalized = normalize_scheduled_at_utc(scheduled_at)
    if normalized is None:
        return ""
    start = to_utc_aware(datetime.fromisoformat(normalized))
    if start is None:
        return ""
    return format_slot_time(start, timezone)


def _format_notification_block(
    *,
    kind: str,
    lead_id: str,
    scheduled_at: str,
    timezone: str,
) -> str:
    when = _format_when(scheduled_at, timezone)
    first_line = _KIND_FIRST_LINES.get(kind, "נקבעה פגישה.")
    lines = [first_line, lead_id]
    if when:
        lines.append(f"מועד: {when}")
    return "\n".join(lines)


def format_owner_notify_inbox(
    rows: list,
    *,
    timezone: str,
    total_unseen: int,
) -> str:
    if not rows:
        return _EMPTY_ACK
    blocks = [
        _format_notification_block(
            kind=row.kind,
            lead_id=row.lead_id,
            scheduled_at=row.scheduled_at,
            timezone=timezone,
        )
        for row in rows
    ]
    body = "\n".join(blocks)
    extra = total_unseen - len(rows)
    if extra > 0:
        body = f"{body}\nעוד {extra} התראות."
    return body


def persist_owner_notify(
    store: LeadStore,
    *,
    kind: str,
    lead_id: str,
    scheduled_at: str,
    kill_switch: bool,
    demo_active: bool,
) -> None:
    if kind not in OWNER_NOTIFY_KINDS:
        return
    if demo_active or kill_switch:
        return
    try:
        assert_allowed(
            RiskAction(name="owner_notify_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    normalized = normalize_scheduled_at_utc(scheduled_at)
    if normalized is None:
        return
    store.upsert_owner_notification(
        kind=kind,
        lead_id=lead_id,
        scheduled_at=normalized,
    )


def persist_meeting_booked_owner_notify(
    store: LeadStore,
    *,
    lead_id: str,
    scheduled_at: str,
    kill_switch: bool,
    demo_active: bool,
) -> None:
    persist_owner_notify(
        store,
        kind=KIND_MEETING_BOOKED,
        lead_id=lead_id,
        scheduled_at=scheduled_at,
        kill_switch=kill_switch,
        demo_active=demo_active,
    )


def apply_owner_notify(
    store: LeadStore,
    *,
    timezone: str,
    kill_switch: bool,
    demo_active: bool,
    now: datetime | None = None,
) -> str | None:
    if demo_active:
        return None
    total = store.count_unseen_owner_notifications(kinds=OWNER_NOTIFY_KINDS)
    rows = store.list_unseen_owner_notifications(
        kinds=OWNER_NOTIFY_KINDS, limit=_MAX_RETURN
    )
    text = format_owner_notify_inbox(rows, timezone=timezone, total_unseen=total)
    if kill_switch or not rows:
        return text
    try:
        assert_allowed(
            RiskAction(name="owner_notify_deliver", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=False,
        )
    except PolicyDenied:
        return text
    store.mark_owner_notifications_seen(
        [row.id for row in rows],
        seen_at=_utc_iso_now(now),
    )
    return text
