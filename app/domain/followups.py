"""Prospect follow-up persistence (§12.1): stateful tasks, no send."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, field_validator

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.events import Channel, build_follow_up_event, persist_tool_outcome
from app.domain.followup_voice import compose_follow_up_draft
from app.domain.humanity import lint_customer_reply
from app.domain.policies.freshness import overlay_stale, stamp_freshness
from app.domain.sales import FitLevel, SalesState
from app.domain.tools import ToolOutcome
from app.domain.value import ValueKind, persist_business_value

MAX_PENDING_PER_LEAD = 1
MAX_OUTBOUND_PER_LEAD_PER_DAY = 1
FOLLOW_UP_DUE_OFFSET_DAYS = 1
REASON_MEETING_OFFERED = "meeting_offered"
REASON_MEETING_BOOKED = "meeting_booked"
STATUS_PENDING = "pending"
STATUS_CANCELLED = "cancelled"
STATUS_RECOVERED = "recovered"

SENDABLE_CHANNELS = frozenset({"whatsapp", "instagram"})
ALLOWLISTED_SEND_REASONS = frozenset(
    {
        "due_pending",
        "no_row",
        "not_pending",
        "cancelled",
        "recovered",
        "not_due",
        "kill_switch",
        "conversation_killed",
        "human_takeover",
        "channel_not_sendable",
        "poor_fit",
        "frequency_capped",
        "meeting_booked",
    }
)


def _allowlisted_reason(value: str) -> str:
    if value not in ALLOWLISTED_SEND_REASONS:
        raise ValueError(f"invalid follow-up send reason: {value}")
    return value


class FollowUpSendDecision(BaseModel):
    allowed: bool
    reason: str
    messages_counted: bool = False

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        return _allowlisted_reason(value)


class FollowUpScanResult(BaseModel):
    lead_id: str
    allowed: bool
    reason: str

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        return _allowlisted_reason(value)


def local_day_bounds_utc_iso(*, now: datetime, timezone: str) -> tuple[str, str] | None:
    try:
        tz = ZoneInfo(timezone)
    except (ValueError, OSError, KeyError, ZoneInfoNotFoundError):
        return None
    if now.tzinfo is None:
        local = now.replace(tzinfo=UTC).astimezone(tz)
    else:
        local = now.astimezone(tz)
    first = local.replace(hour=0, minute=0, second=0, microsecond=0)
    next_day = first + timedelta(days=1)
    return (
        first.astimezone(UTC).isoformat(),
        next_day.astimezone(UTC).isoformat(),
    )


def follow_up_due_on(
    *,
    now: datetime,
    timezone: str,
    offset_days: int = FOLLOW_UP_DUE_OFFSET_DAYS,
) -> str:
    """Return YYYY-MM-DD in *timezone* for today + *offset_days*."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_now = now.astimezone(ZoneInfo(timezone))
    due_date = local_now.date() + timedelta(days=offset_days)
    return due_date.isoformat()


def evaluate_follow_up_send(
    store,
    *,
    lead_id: str,
    sales: SalesState,
    timezone: str,
    kill_switch: bool,
    now: datetime | None = None,
) -> FollowUpSendDecision:
    """Decide whether a follow-up send would be legal. Never sends."""
    if kill_switch:
        return FollowUpSendDecision(allowed=False, reason="kill_switch")

    row = store.get_follow_up(lead_id)
    if row is None:
        return FollowUpSendDecision(allowed=False, reason="no_row")

    meeting = store.get_meeting(lead_id)
    if meeting is not None and meeting.status in {"booked", "cancellation_requested"}:
        return FollowUpSendDecision(allowed=False, reason=REASON_MEETING_BOOKED)

    if row.status == STATUS_CANCELLED:
        return FollowUpSendDecision(allowed=False, reason="cancelled")

    if row.status == STATUS_RECOVERED:
        return FollowUpSendDecision(allowed=False, reason="recovered")

    if row.status != STATUS_PENDING:
        return FollowUpSendDecision(allowed=False, reason="not_pending")

    if store.is_conversation_killed(lead_id):
        return FollowUpSendDecision(allowed=False, reason="conversation_killed")

    if store.is_human_takeover(lead_id):
        return FollowUpSendDecision(allowed=False, reason="human_takeover")

    if row.channel not in SENDABLE_CHANNELS:
        return FollowUpSendDecision(allowed=False, reason="channel_not_sendable")

    if sales.fit == FitLevel.POOR:
        return FollowUpSendDecision(allowed=False, reason="poor_fit")

    effective_now = now or datetime.now(UTC)
    today = follow_up_due_on(now=effective_now, timezone=timezone, offset_days=0)
    if row.due_at > today:
        return FollowUpSendDecision(allowed=False, reason="not_due")

    bounds = local_day_bounds_utc_iso(now=effective_now, timezone=timezone)
    if bounds is None:
        return FollowUpSendDecision(allowed=False, reason="frequency_capped")
    occurred_from, occurred_to = bounds
    outbound_count = store.count_canonical_events_for_lead(
        lead_id=lead_id,
        event_type="message_out",
        occurred_from=occurred_from,
        occurred_to=occurred_to,
    )
    if outbound_count >= MAX_OUTBOUND_PER_LEAD_PER_DAY:
        return FollowUpSendDecision(
            allowed=False,
            reason="frequency_capped",
            messages_counted=True,
        )

    return FollowUpSendDecision(
        allowed=True,
        reason="due_pending",
        messages_counted=True,
    )


def lead_recent_messages_outcome(*, present: bool, now: datetime) -> ToolOutcome:
    base_status = "ok" if present else "empty"
    stamp = stamp_freshness(
        "lead_recent_messages",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="lead_recent_messages",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=1 if present else 0,
        freshness=stamp.status,
    )


def website_session_events_outcome(*, present: bool, now: datetime) -> ToolOutcome:
    base_status = "ok" if present else "empty"
    stamp = stamp_freshness(
        "website_session_events",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="website_session_events",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=1 if present else 0,
        freshness=stamp.status,
    )


def scan_due_follow_ups(
    store,
    *,
    timezone: str,
    kill_switch: bool,
    now: datetime | None = None,
) -> list[FollowUpScanResult]:
    """Evaluate due pending follow-ups and persist scan fields. Never sends."""
    effective_now = now or datetime.now(UTC)
    try:
        today = follow_up_due_on(now=effective_now, timezone=timezone, offset_days=0)
    except (ValueError, OSError, KeyError, ZoneInfoNotFoundError):
        return []

    rows = store.list_due_pending_follow_ups(due_on=today)
    results: list[FollowUpScanResult] = []
    for row in rows:
        try:
            assert_allowed(
                RiskAction(name="follow_up_scan", risk=RiskLevel.R1_LOW_WRITE),
                kill_switch=False,
            )
        except PolicyDenied:
            continue

        try:
            sales = store.get_sales(row.lead_id)
        except KeyError:
            continue

        decision = evaluate_follow_up_send(
            store,
            lead_id=row.lead_id,
            sales=sales,
            timezone=timezone,
            kill_switch=kill_switch,
            now=effective_now,
        )
        draft = ""
        if decision.allowed:
            candidate = compose_follow_up_draft(reason=row.reason)
            if lint_customer_reply(candidate).ok and len(candidate) <= 500:
                draft = candidate
        store.save_follow_up_scan(
            lead_id=row.lead_id,
            send_ready=decision.allowed,
            block_reason=decision.reason,
            draft=draft,
        )
        if decision.messages_counted:
            persist_tool_outcome(
                store,
                provider="followup_scan",
                channel=Channel(row.channel),
                inbound_provider_event_id=f"{row.lead_id}:followup-scan:{today}",
                conversation_id="",
                lead_id=row.lead_id,
                outcome=lead_recent_messages_outcome(present=True, now=effective_now),
            )
        results.append(
            FollowUpScanResult(
                lead_id=row.lead_id,
                allowed=decision.allowed,
                reason=decision.reason,
            )
        )
    return results


FOLLOW_UP_SCOPE = "follow_up"


def follow_up_claim_key(inbound_id: str) -> str:
    return f"{inbound_id}:followup"


def claim_follow_up_persist(*, store, inbound_id: str) -> bool:
    if not inbound_id:
        return False
    return store.claim_operation(scope=FOLLOW_UP_SCOPE, key=follow_up_claim_key(inbound_id))


def complete_follow_up_persist(*, store, inbound_id: str) -> None:
    if not inbound_id:
        return
    store.complete_operation(
        scope=FOLLOW_UP_SCOPE,
        key=follow_up_claim_key(inbound_id),
        result_json='{"ok": true}',
    )


def apply_follow_up_policy(
    store,
    *,
    lead_id: str,
    channel: Channel,
    action: str,
    sales: SalesState,
    timezone: str,
    kill_switch: bool,
    now: datetime | None = None,
    inbound_id: str = "",
) -> None:
    """Persist or cancel lead follow-ups. Never sends; swallows PolicyDenied only."""
    claimed = False
    if inbound_id:
        if not claim_follow_up_persist(store=store, inbound_id=inbound_id):
            return
        claimed = True
    try:
        action_key = str(action).lower().strip()
        effective_now = now or datetime.now(UTC)

        if action_key in ("stop", "disqualify", "handoff"):
            _close_pending(
                store,
                lead_id=lead_id,
                channel=channel,
                status=STATUS_CANCELLED,
                occurred_at=effective_now,
            )
            return

        if action_key != "offer_meeting":
            _close_pending(
                store,
                lead_id=lead_id,
                channel=channel,
                status=STATUS_RECOVERED,
                occurred_at=effective_now,
            )
            return

        if sales.willingness_to_meet is False:
            return

        if kill_switch:
            return

        try:
            assert_allowed(
                RiskAction(name="follow_up_persist", risk=RiskLevel.R1_LOW_WRITE),
                kill_switch=kill_switch,
            )
        except PolicyDenied:
            return

        due_at = follow_up_due_on(now=effective_now, timezone=timezone)
        row = store.get_follow_up(lead_id)
        pending = 1 if row is not None and row.status == STATUS_PENDING else 0
        if pending >= MAX_PENDING_PER_LEAD:
            return

        if row is not None and row.status in (STATUS_CANCELLED, STATUS_RECOVERED):
            store.upsert_follow_up(
                lead_id=lead_id,
                channel=channel.value,
                reason=REASON_MEETING_OFFERED,
                status=STATUS_PENDING,
                due_at=due_at,
            )
            return

        store.upsert_follow_up(
            lead_id=lead_id,
            channel=channel.value,
            reason=REASON_MEETING_OFFERED,
            status=STATUS_PENDING,
            due_at=due_at,
        )
        store.save_canonical_event(
            provider=channel.value,
            event=build_follow_up_event(
                provider=channel.value,
                channel=channel,
                lead_id=lead_id,
                reason=REASON_MEETING_OFFERED,
                status=STATUS_PENDING,
                occurred_at=effective_now,
            ),
        )
    finally:
        if claimed:
            complete_follow_up_persist(store=store, inbound_id=inbound_id)


def cancel_follow_up_for_booked(
    store,
    *,
    lead_id: str,
    channel: Channel,
    occurred_at: datetime,
) -> None:
    """Close only a pending meeting-offered follow-up after verified booking."""
    _close_pending(
        store,
        lead_id=lead_id,
        channel=channel,
        status=STATUS_CANCELLED,
        occurred_at=occurred_at,
        reason=REASON_MEETING_BOOKED,
    )


def _close_pending(
    store,
    *,
    lead_id: str,
    channel: Channel,
    status: str,
    occurred_at: datetime,
    reason: str = REASON_MEETING_OFFERED,
) -> None:
    row = store.get_follow_up(lead_id)
    if (
        row is None
        or row.status != STATUS_PENDING
        or row.reason != REASON_MEETING_OFFERED
    ):
        return
    store.upsert_follow_up(
        lead_id=lead_id,
        channel=row.channel,
        reason=reason,
        status=status,
        due_at=row.due_at,
    )
    store.save_canonical_event(
        provider=channel.value,
        event=build_follow_up_event(
            provider=channel.value,
            channel=channel,
            lead_id=lead_id,
            reason=reason,
            status=status,
            occurred_at=occurred_at,
        ),
    )
    if status == STATUS_RECOVERED:
        persist_business_value(
            store,
            provider=channel.value,
            channel=channel,
            lead_id=lead_id,
            kind=ValueKind.RECOVERED,
        )
