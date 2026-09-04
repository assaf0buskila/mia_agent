import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from app.domain.attribution import ATTRIBUTION_KEYS
from app.domain.behavior import (
    ALL_BEHAVIOR_KINDS,
    CLIENT_BEHAVIOR_KINDS,
    behavior_provider_event_id,
)
from app.domain.sales import MEDDPICC_MISSING_ORDER
from app.domain.tools import (
    ALLOWLISTED_TOOL_STATUSES,
    ALLOWLISTED_TOOLS,
    ToolOutcome,
    clamp_tool_freshness,
)

_BEHAVIOR_PAYLOAD_KEYS = frozenset({"kind", "path", "section", "cta"})

_MESSAGE_TEXT_MAX = 2000

_CORRELATION_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class Channel(StrEnum):
    WEBSITE = "website"
    INSTAGRAM = "instagram"
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    GMAIL = "gmail"
    CALENDAR = "calendar"
    LINKEDIN = "linkedin"
    INTERNAL = "internal"


ALLOWLISTED_WEBHOOK_ENVELOPE_KINDS = frozenset({"text", "audio", "empty", "referral", "image"})


def webhook_envelope_kind(item: dict[str, str]) -> str:
    item_id = item.get("id", "")
    if item_id.startswith("igref:"):
        return "referral"
    if item.get("source") == "audio":
        return "audio"
    if item.get("photo_file_id"):
        return "image"
    if not item.get("text", "").strip():
        return "empty"
    return "text"


def sanitize_webhook_channel(value: str) -> str:
    try:
        return Channel(value).value
    except ValueError:
        return ""


def sanitize_webhook_envelope_kind(value: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if text in ALLOWLISTED_WEBHOOK_ENVELOPE_KINDS else ""


class EventType(StrEnum):
    MESSAGE_IN = "message_in"
    MESSAGE_OUT = "message_out"
    LEAD_CREATED = "lead_created"
    ATTRIBUTION = "attribution"
    BEHAVIOR = "behavior"
    QUALIFICATION_UPDATED = "qualification_updated"
    MEETING_OFFERED = "meeting_offered"
    MEETING_BOOKED = "meeting_booked"
    MEETING_RESCHEDULED = "meeting_rescheduled"
    MEETING_CANCELLATION_REQUESTED = "meeting_cancellation_requested"
    HANDOFF = "handoff"
    TOOL_RESULT = "tool_result"
    APPROVAL_REQUIRED = "approval_required"
    FOLLOW_UP = "follow_up"
    DEAL_UPDATED = "deal_updated"
    MEETING_BRIEF = "meeting_brief"
    MEETING_DEBRIEF = "meeting_debrief"
    BUSINESS_VALUE = "business_value"


class CanonicalEvent(BaseModel):
    """Channel-agnostic event. Providers map into this; the sales engine never sees SDK objects."""

    event_id: str
    event_type: EventType
    channel: Channel
    occurred_at: datetime
    idempotency_key: str
    lead_id: str | None = None
    conversation_id: str | None = None
    actor_role: str = "unknown"
    payload: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str = ""
    payload_version: str = ""


CANONICAL_PAYLOAD_VERSION = "1"
ALLOWLISTED_PAYLOAD_VERSIONS = frozenset({CANONICAL_PAYLOAD_VERSION})


def new_correlation_id() -> str:
    return f"cor_{uuid4().hex[:20]}"


def sanitize_correlation_id(value: str) -> str:
    text = value.strip()
    return text if _CORRELATION_RE.fullmatch(text) else ""


def stamp_correlation(event: CanonicalEvent, correlation_id: str) -> CanonicalEvent:
    event.correlation_id = sanitize_correlation_id(correlation_id)
    return event


def sanitize_payload_version(value: str) -> str:
    if "\n" in value or "\r" in value:
        return ""
    text = value.strip()
    return text if text in ALLOWLISTED_PAYLOAD_VERSIONS else ""


def stamp_payload_version(event: CanonicalEvent) -> CanonicalEvent:
    cleaned = sanitize_payload_version(event.payload_version)
    event.payload_version = cleaned or CANONICAL_PAYLOAD_VERSION
    return event


def build_message_in_event(
    *,
    provider: str,
    channel: Channel,
    provider_event_id: str,
    conversation_id: str,
    text: str,
    actor_role: str,
    lead_id: str | None = None,
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build MESSAGE_IN from inbound text. Payload is text-only; no SDK objects or secrets."""
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.MESSAGE_IN,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=conversation_id,
        actor_role=actor_role,
        payload={"text": text[:_MESSAGE_TEXT_MAX]},
        source={"provider": provider},
    )


_QUALIFICATION_PAYLOAD_KEYS = frozenset({
    "fit",
    "pain_level",
    "workflow_known",
    "impact_confirmed",
    "reflected",
    "hypothesis_offered",
    "buying_reality_known",
    "authority_known",
    "timeline_known",
    "metric_known",
    "missing_fields",
    "willingness_to_meet",
    "owner_required",
    "whatsapp_handoff_offered",
    "active_objection",
})

_ALLOWED_MISSING_FIELDS = frozenset(MEDDPICC_MISSING_ORDER)


def _sanitize_qualification_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clean = {key: payload[key] for key in _QUALIFICATION_PAYLOAD_KEYS if key in payload}
    missing = clean.get("missing_fields")
    if isinstance(missing, list):
        clean["missing_fields"] = [
            name
            for name in missing
            if isinstance(name, str) and name in _ALLOWED_MISSING_FIELDS
        ]
    elif "missing_fields" in clean:
        del clean["missing_fields"]
    return clean


def build_attribution_event(
    *,
    provider: str,
    channel: Channel,
    lead_id: str,
    conversation_id: str,
    payload: dict[str, Any],
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build ATTRIBUTION from sanitized website or Instagram attribution payload."""
    provider_event_id = f"{lead_id}:attribution"
    clean_payload = {
        key: payload[key] for key in ATTRIBUTION_KEYS if key in payload
    }
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.ATTRIBUTION,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=conversation_id,
        actor_role="system",
        payload=clean_payload,
        source={"provider": provider},
    )


def build_lead_created_event(
    *,
    provider: str,
    channel: Channel,
    lead_id: str,
    conversation_id: str,
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build LEAD_CREATED when a new lead row is opened. Payload is stage-only; no PII."""
    provider_event_id = f"{lead_id}:created"
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.LEAD_CREATED,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=conversation_id,
        actor_role="system",
        payload={"stage": "open"},
        source={"provider": provider},
    )


def build_qualification_updated_event(
    *,
    provider: str,
    channel: Channel,
    run_id: str,
    lead_id: str,
    conversation_id: str,
    payload: dict[str, Any],
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build QUALIFICATION_UPDATED after SalesState changes. Strips unknown keys from payload."""
    provider_event_id = f"{run_id}:qual"
    clean_payload = _sanitize_qualification_payload(payload)
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.QUALIFICATION_UPDATED,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=conversation_id,
        actor_role="system",
        payload=clean_payload,
        source={"provider": provider},
    )


def build_meeting_offered_event(
    *,
    provider: str,
    channel: Channel,
    run_id: str,
    lead_id: str,
    conversation_id: str,
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build MEETING_OFFERED when graph selects offer_meeting. Decision only; no slots or PII."""
    provider_event_id = f"{run_id}:meet"
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.MEETING_OFFERED,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=conversation_id,
        actor_role="system",
        payload={"next_action": "offer_meeting"},
        source={"provider": provider},
    )


def build_meeting_booked_event(
    *,
    provider: str,
    channel: Channel,
    lead_id: str,
    conversation_id: str,
    scheduled_at: str,
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build MEETING_BOOKED on explicit confirmation. Status + UTC time only; no PII."""
    if not scheduled_at:
        raise ValueError("scheduled_at required")
    try:
        normalized = scheduled_at.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        utc_iso = parsed.astimezone(UTC).isoformat()
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid scheduled_at") from exc
    provider_event_id = f"{lead_id}:booked"
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.MEETING_BOOKED,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=conversation_id,
        actor_role="system",
        payload={"status": "booked", "scheduled_at": utc_iso},
        source={"provider": provider},
    )


def build_meeting_rescheduled_event(
    *,
    provider: str,
    channel: Channel,
    lead_id: str,
    conversation_id: str,
    target_booking_key: str,
    scheduled_at: str,
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build first-write event for one deterministic reschedule target."""
    if not target_booking_key or not scheduled_at:
        raise ValueError("target booking key and scheduled_at required")
    try:
        parsed = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("scheduled_at must be timezone-aware")
        utc_iso = parsed.astimezone(UTC).isoformat()
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid scheduled_at") from exc
    provider_event_id = f"{lead_id}:rescheduled:{target_booking_key}"
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.MEETING_RESCHEDULED,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=conversation_id,
        actor_role="system",
        payload={"status": "booked", "scheduled_at": utc_iso},
        source={"provider": provider},
    )


def build_meeting_cancellation_requested_event(
    *,
    provider: str,
    channel: Channel,
    lead_id: str,
    conversation_id: str,
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build local-only cancellation request event without provider details."""
    provider_event_id = f"{lead_id}:cancellation_requested"
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.MEETING_CANCELLATION_REQUESTED,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=conversation_id,
        actor_role="system",
        payload={"status": "cancellation_requested"},
        source={"provider": provider},
    )


_BUSINESS_VALUE_KINDS = frozenset({"qualified", "booked", "recovered", "handoff"})


def build_business_value_event(
    *,
    provider: str,
    channel: Channel,
    lead_id: str,
    kind: str,
    conversation_id: str = "",
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build BUSINESS_VALUE count event. Kind + empty ILS only; no PII or inferred value."""
    if kind not in _BUSINESS_VALUE_KINDS:
        raise ValueError(f"unknown business value kind: {kind}")
    provider_event_id = f"{lead_id}:value:{kind}"
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.BUSINESS_VALUE,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=conversation_id or None,
        actor_role="system",
        payload={"kind": kind, "estimated_value_ils": ""},
        source={"provider": provider},
    )


def build_handoff_event(
    *,
    provider: str,
    channel: Channel,
    run_id: str,
    lead_id: str,
    conversation_id: str,
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build HANDOFF when graph selects owner handoff. Decision only; no message text or PII."""
    provider_event_id = f"{run_id}:handoff"
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.HANDOFF,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=conversation_id,
        actor_role="system",
        payload={"next_action": "handoff"},
        source={"provider": provider},
    )


def build_behavior_event(
    *,
    session_id: str,
    lead_id: str,
    payload: dict[str, str],
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build BEHAVIOR for website funnel tracking. Sanitized payload only; no PII."""
    kind = payload.get("kind", "")
    if kind not in ALL_BEHAVIOR_KINDS:
        raise ValueError(f"unknown behavior kind: {kind}")
    clean = {
        key: payload[key]
        for key in _BEHAVIOR_PAYLOAD_KEYS
        if key in payload and isinstance(payload[key], str)
    }
    provider_event_id = behavior_provider_event_id(session_id, clean)
    actor_role = "prospect" if kind in CLIENT_BEHAVIOR_KINDS else "system"
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.BEHAVIOR,
        channel=Channel.WEBSITE,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=session_id,
        actor_role=actor_role,
        payload=clean,
        source={"provider": "website"},
    )


_FOLLOW_UP_PAYLOAD_KEYS = frozenset({"status", "reason"})
_FOLLOW_UP_REASONS = frozenset({"meeting_offered", "meeting_booked"})
_FOLLOW_UP_STATUSES = frozenset({"pending", "cancelled", "recovered"})


_DEAL_UPDATED_PAYLOAD_KEYS = frozenset({"stage", "source", "attribution_confidence"})
_DEAL_UPDATED_STAGES = frozenset({"meeting_offered", "proposal"})
_DEAL_UPDATED_CONFIDENCE = frozenset({"utm", "ig", "meta_ad", "unknown"})


def build_deal_updated_event(
    *,
    provider: str,
    channel: Channel,
    lead_id: str,
    stage: str,
    source: str,
    attribution_confidence: str,
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build DEAL_UPDATED when a deal row is upserted. Stage/confidence only; no values or PII."""
    if stage not in _DEAL_UPDATED_STAGES:
        raise ValueError(f"unknown deal stage: {stage}")
    if attribution_confidence not in _DEAL_UPDATED_CONFIDENCE:
        raise ValueError(f"unknown attribution confidence: {attribution_confidence}")
    provider_event_id = f"{lead_id}:deal:{stage}"
    raw_payload = {
        "stage": stage,
        "source": source,
        "attribution_confidence": attribution_confidence,
    }
    clean_payload = {
        key: value
        for key, value in raw_payload.items()
        if key in _DEAL_UPDATED_PAYLOAD_KEYS
    }
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.DEAL_UPDATED,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=None,
        actor_role="system",
        payload=clean_payload,
        source={"provider": provider},
    )


def build_follow_up_event(
    *,
    provider: str,
    channel: Channel,
    lead_id: str,
    reason: str,
    status: str,
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build FOLLOW_UP when a prospect follow-up row is created, cancelled, or recovered."""
    if reason not in _FOLLOW_UP_REASONS:
        raise ValueError(f"unknown follow-up reason: {reason}")
    if status not in _FOLLOW_UP_STATUSES:
        raise ValueError(f"unknown follow-up status: {status}")
    suffix = "" if status == "pending" else f":{status}"
    provider_event_id = f"{lead_id}:followup:{reason}{suffix}"
    raw_payload = {"status": status, "reason": reason}
    clean_payload = {
        key: value for key, value in raw_payload.items() if key in _FOLLOW_UP_PAYLOAD_KEYS
    }
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.FOLLOW_UP,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=None,
        actor_role="system",
        payload=clean_payload,
        source={"provider": provider},
    )


_MEETING_BRIEF_PAYLOAD_KEYS = frozenset({
    "channel",
    "fit",
    "pain_level",
    "workflow_known",
    "impact_confirmed",
    "reflected",
    "hypothesis_offered",
    "buying_reality_known",
    "authority_known",
    "timeline_known",
    "metric_known",
    "willingness_to_meet",
    "owner_required",
    "active_objection",
    "missing_fields",
    "owner_questions",
    "next_action",
})


def _sanitize_meeting_brief_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clean = {key: payload[key] for key in _MEETING_BRIEF_PAYLOAD_KEYS if key in payload}
    try:
        Channel(str(clean.get("channel", "")))
    except ValueError:
        clean.pop("channel", None)
    if "pain_level" in clean:
        try:
            pain = int(clean["pain_level"])
            clean["pain_level"] = max(0, min(5, pain))
        except (TypeError, ValueError):
            del clean["pain_level"]
    if clean.get("next_action") != "offer_meeting":
        clean["next_action"] = "offer_meeting"
    if "willingness_to_meet" in clean and clean["willingness_to_meet"] not in (
        True,
        False,
        None,
    ):
        del clean["willingness_to_meet"]
    missing = clean.get("missing_fields")
    if isinstance(missing, list):
        clean["missing_fields"] = [
            name
            for name in missing
            if isinstance(name, str) and name in _ALLOWED_MISSING_FIELDS
        ]
    elif "missing_fields" in clean:
        del clean["missing_fields"]
    questions = clean.get("owner_questions")
    if isinstance(questions, list):
        clean["owner_questions"] = [
            name
            for name in questions
            if isinstance(name, str) and name in _ALLOWED_MISSING_FIELDS
        ]
    elif "owner_questions" in clean:
        del clean["owner_questions"]
    return clean


_APPROVAL_REQUIRED_ACTION = "proposal_handoff"
_APPROVAL_REQUIRED_RISK = "R3"
_APPROVAL_REQUIRED_DECISION = "pending"
_WEBSITE_EDIT_ACTION = "website_edit"


def build_approval_required_event(
    *,
    provider: str,
    channel: Channel,
    lead_id: str | None = None,
    action: str | None = None,
    risk: str | None = None,
    decision: str | None = None,
    resource_id: str | None = None,
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build APPROVAL_REQUIRED when an approval row is persisted."""
    effective_action = action or _APPROVAL_REQUIRED_ACTION
    effective_risk = risk or _APPROVAL_REQUIRED_RISK
    effective_decision = decision or _APPROVAL_REQUIRED_DECISION
    effective_resource_id = resource_id if resource_id is not None else (lead_id or "")
    if effective_action == _WEBSITE_EDIT_ACTION:
        provider_event_id = f"{effective_resource_id}:approval:website_edit"
    else:
        provider_event_id = f"{lead_id}:approval:proposal_handoff"
    clean_payload = {
        "action": effective_action,
        "risk": effective_risk,
        "decision": effective_decision,
    }
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.APPROVAL_REQUIRED,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=None,
        actor_role="system",
        payload=clean_payload,
        source={"provider": provider},
    )


def build_meeting_brief_event(
    *,
    provider: str,
    channel: Channel,
    lead_id: str,
    payload: dict[str, Any],
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build MEETING_BRIEF when offer_meeting persists a pre-meeting snapshot."""
    provider_event_id = f"{lead_id}:brief:offer_meeting"
    clean_payload = _sanitize_meeting_brief_payload(payload)
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.MEETING_BRIEF,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=None,
        actor_role="system",
        payload=clean_payload,
        source={"provider": provider},
    )


_MEETING_DEBRIEF_OUTCOMES = frozenset({"held", "no_show", "unclear"})
_MEETING_DEBRIEF_NEXT_STEPS = frozenset({"none", "follow_up", "proposal"})
_MEETING_DEBRIEF_PAYLOAD_KEYS = frozenset({"outcome", "next_step"})


def _sanitize_meeting_debrief_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clean = {
        key: payload[key]
        for key in _MEETING_DEBRIEF_PAYLOAD_KEYS
        if key in payload
    }
    outcome = clean.get("outcome")
    if outcome not in _MEETING_DEBRIEF_OUTCOMES:
        raise ValueError(f"unknown debrief outcome: {outcome}")
    next_step = clean.get("next_step")
    if next_step not in _MEETING_DEBRIEF_NEXT_STEPS:
        clean["next_step"] = "none"
    return clean


def build_meeting_debrief_event(
    *,
    provider: str,
    channel: Channel,
    lead_id: str,
    outcome: str,
    next_step: str = "none",
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build MEETING_DEBRIEF when owner post-meeting summary persists."""
    provider_event_id = f"{lead_id}:debrief"
    clean_payload = _sanitize_meeting_debrief_payload(
        {"outcome": outcome, "next_step": next_step}
    )
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.MEETING_DEBRIEF,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=None,
        actor_role="system",
        payload=clean_payload,
        source={"provider": provider},
    )


def build_tool_result_event(
    *,
    provider: str,
    channel: Channel,
    inbound_provider_event_id: str,
    conversation_id: str,
    outcome: ToolOutcome,
    lead_id: str | None = None,
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build TOOL_RESULT for allowlisted tool outcomes. No PII, URLs, or raw tool data."""
    if outcome.tool not in ALLOWLISTED_TOOLS:
        raise ValueError(f"unknown tool: {outcome.tool}")
    if outcome.status not in ALLOWLISTED_TOOL_STATUSES:
        raise ValueError(f"unknown tool status: {outcome.status}")
    if outcome.result_count < 0:
        raise ValueError("result_count must be >= 0")
    provider_event_id = f"{inbound_provider_event_id}:tool:{outcome.tool}"
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.TOOL_RESULT,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=conversation_id,
        actor_role="system",
        payload={
            "tool": outcome.tool,
            "status": outcome.status,
            "result_count": outcome.result_count,
        },
        source={"provider": provider},
    )


def persist_tool_outcome(
    store: Any,
    *,
    provider: str,
    channel: Channel,
    inbound_provider_event_id: str,
    conversation_id: str,
    lead_id: str | None,
    outcome: ToolOutcome | None,
    correlation_id: str = "",
    latency_ms: int = 0,
) -> None:
    if outcome is None:
        return
    event = build_tool_result_event(
        provider=provider,
        channel=channel,
        inbound_provider_event_id=inbound_provider_event_id,
        conversation_id=conversation_id,
        lead_id=lead_id,
        outcome=outcome,
    )
    sanitized = sanitize_correlation_id(correlation_id)
    if sanitized:
        stamp_correlation(event, sanitized)
    store.save_canonical_event(
        provider=provider,
        event=event,
    )
    effective = latency_ms if latency_ms else outcome.latency_ms
    clamped = max(0, min(int(effective), 86_400_000))
    store.save_tool_run(
        provider_event_id=f"{inbound_provider_event_id}:tool:{outcome.tool}",
        provider=provider,
        channel=channel.value,
        lead_id=lead_id,
        conversation_id=conversation_id,
        tool=outcome.tool,
        status=outcome.status,
        result_count=outcome.result_count,
        latency_ms=clamped,
        cost_usd=0,
        freshness=clamp_tool_freshness(outcome.freshness),
        correlation_id=sanitized,
    )


def sheets_mirror_outcome(written_count: int, *, latency_ms: int = 0) -> ToolOutcome:
    if written_count > 0:
        return ToolOutcome(
            tool="sheets_mirror", status="ok", result_count=written_count, latency_ms=latency_ms
        )
    return ToolOutcome(
        tool="sheets_mirror", status="denied", result_count=0, latency_ms=latency_ms
    )


_SHEETS_TAB_MIRROR_TOOLS = frozenset({
})


def transcription_outcome(*, transcribed: bool, latency_ms: int = 0) -> ToolOutcome:
    if transcribed:
        return ToolOutcome(
            tool="voice_transcribe", status="ok", result_count=1, latency_ms=latency_ms
        )
    return ToolOutcome(
        tool="voice_transcribe", status="empty", result_count=0, latency_ms=latency_ms
    )


def build_message_out_event(
    *,
    provider: str,
    channel: Channel,
    inbound_provider_event_id: str,
    conversation_id: str,
    text: str,
    lead_id: str | None = None,
    occurred_at: datetime | None = None,
) -> CanonicalEvent:
    """Build MESSAGE_OUT after Mia delivers a reply. Pairs with IN via `{inbound_id}:out`."""
    provider_event_id = f"{inbound_provider_event_id}:out"
    return CanonicalEvent(
        event_id=f"evt_{provider_event_id}",
        event_type=EventType.MESSAGE_OUT,
        channel=channel,
        occurred_at=occurred_at or datetime.now(UTC),
        idempotency_key=provider_event_id,
        lead_id=lead_id,
        conversation_id=conversation_id,
        actor_role="mia",
        payload={"text": text[:_MESSAGE_TEXT_MAX]},
        source={"provider": provider},
    )
