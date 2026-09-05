"""Owner lead review persistence: sanitized pipeline snapshot, no send."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.approvals import extract_approval_lead_id
from app.domain.deals import STAGE_MEETING_OFFERED, STAGE_PROPOSAL
from app.domain.lead_label import lead_display
from app.domain.meetings.state import (
    STATUS_BOOKED,
    STATUS_CANCELLATION_REQUESTED,
    STATUS_OFFERED,
)
from app.domain.sales import (
    MEDDPICC_MISSING_ORDER,
    FitLevel,
    NextAction,
    compute_missing_fields,
    select_next_action,
)

if TYPE_CHECKING:
    from app.db.store import LeadStore

extract_review_lead_id = extract_approval_lead_id

ALLOWLISTED_FIT = frozenset(item.value for item in FitLevel)
ALLOWLISTED_NEXT_ACTION = frozenset(item.value for item in NextAction)
ALLOWLISTED_FOLLOW_UP_STATUS = frozenset({"pending", "cancelled", "recovered", ""})
ALLOWLISTED_MEETING_STATUS = frozenset(
    {STATUS_OFFERED, STATUS_BOOKED, STATUS_CANCELLATION_REQUESTED, ""}
)
ALLOWLISTED_DEAL_STAGE = frozenset({STAGE_MEETING_OFFERED, STAGE_PROPOSAL, ""})

_FIT_HE: dict[str, str] = {
    FitLevel.UNKNOWN.value: "לא ידועה",
    FitLevel.POOR.value: "חלשה",
    FitLevel.POSSIBLE.value: "אפשרית",
    FitLevel.GOOD.value: "טובה",
}
_NEXT_ACTION_HE: dict[str, str] = {
    NextAction.UNDERSTAND_WORKFLOW.value: "הבנת תהליך",
    NextAction.DEEPEN_PAIN.value: "העמקת כאב",
    NextAction.QUANTIFY.value: "כימות",
    NextAction.REFLECT.value: "שיקוף",
    NextAction.OFFER_HYPOTHESIS.value: "השערת אוטומציה",
    NextAction.QUALIFY.value: "כישור",
    NextAction.OFFER_MEETING.value: "הצעת פגישה",
    NextAction.OFFER_WHATSAPP.value: "הצעת וואטסאפ",
    NextAction.HANDOFF.value: "העברה",
    NextAction.HANDLE_OBJECTION.value: "התנגדות",
    NextAction.DISQUALIFY.value: "פסילה",
    NextAction.STOP.value: "עצירה",
}
_MISSING_FIELD_HE: dict[str, str] = {
    "decision_maker": "מקבל החלטות",
    "timeline": "לוח זמנים",
    "metric": "מדד",
}
_FOLLOW_UP_STATUS_HE: dict[str, str] = {
    "pending": "ממתין",
    "cancelled": "בוטל",
    "recovered": "התאושש",
}
_DEAL_STAGE_HE: dict[str, str] = {
    STAGE_MEETING_OFFERED: "פגישה הוצעה",
    STAGE_PROPOSAL: "הצעה",
}
_DATE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class LeadReviewSnapshot(BaseModel):
    lead_id: str
    stage: str = ""
    fit: str
    pain_level: int = Field(ge=0, le=5)
    next_action: str
    missing_fields: str = ""
    follow_up_status: str = ""
    follow_up_due_at: str = ""
    meeting_status: str = ""
    deal_stage: str = ""
    conversation_killed: bool = False
    headline: str = ""
    display_name: str = ""


def _format_iso_date(value: str) -> str:
    match = _DATE_ISO.fullmatch(value)
    if match is None:
        return value
    year, month, day = match.groups()
    return f"{day}.{month}.{year}"


def build_lead_review_snapshot(store: LeadStore, *, lead_id: str) -> LeadReviewSnapshot | None:
    lead = store.get_lead(lead_id)
    if lead is None:
        return None
    try:
        sales = store.get_sales(lead_id)
    except KeyError:
        return None
    missing_ordered = [
        name
        for name in MEDDPICC_MISSING_ORDER
        if name in compute_missing_fields(sales)
    ]
    follow_up_status = ""
    follow_up_due_at = ""
    follow_up = store.get_follow_up(lead_id)
    if follow_up is not None:
        follow_up_status = follow_up.status
        follow_up_due_at = follow_up.due_at or ""
    meeting_status = ""
    meeting = store.get_meeting(lead_id)
    if meeting is not None:
        meeting_status = meeting.status
    deal_stage = ""
    deal = store.get_deal(lead_id)
    if deal is not None:
        deal_stage = deal.stage
    return LeadReviewSnapshot(
        lead_id=lead_id,
        stage=lead.stage or "",
        fit=sales.fit.value,
        pain_level=int(sales.pain_level),
        next_action=select_next_action(sales).value,
        missing_fields=",".join(missing_ordered),
        follow_up_status=follow_up_status,
        follow_up_due_at=follow_up_due_at,
        meeting_status=meeting_status,
        deal_stage=deal_stage,
        conversation_killed=store.is_conversation_killed(lead_id),
        headline=sales.headline or "",
        display_name=sales.display_name or "",
    )


def format_lead_review(snapshot: LeadReviewSnapshot) -> str:
    missing_names = [
        name.strip()
        for name in snapshot.missing_fields.split(",")
        if name.strip() in MEDDPICC_MISSING_ORDER
    ]
    missing_he = ", ".join(_MISSING_FIELD_HE[name] for name in missing_names)
    follow_up_line = "אין"
    if snapshot.follow_up_status:
        status_he = _FOLLOW_UP_STATUS_HE.get(
            snapshot.follow_up_status, snapshot.follow_up_status
        )
        if snapshot.follow_up_due_at:
            follow_up_line = f"{status_he} {_format_iso_date(snapshot.follow_up_due_at)}"
        else:
            follow_up_line = status_he
    meeting_line = {
        STATUS_OFFERED: "הוצעה",
        STATUS_BOOKED: "נקבעה",
        STATUS_CANCELLATION_REQUESTED: "בקשת ביטול",
    }.get(snapshot.meeting_status, "אין")
    deal_line = _DEAL_STAGE_HE.get(snapshot.deal_stage, "אין")
    if snapshot.deal_stage == "":
        deal_line = "אין"
    killed_line = "כן" if snapshot.conversation_killed else "לא"
    who = lead_display(snapshot.lead_id, snapshot.headline, snapshot.display_name)
    lines = [
        f"סקירת ליד {who}",
        f"שלב: {snapshot.stage or ''}",
        f"התאמה: {_FIT_HE.get(snapshot.fit, snapshot.fit)}",
        f"כאב: P{snapshot.pain_level}",
        f"פעולה הבאה: {_NEXT_ACTION_HE.get(snapshot.next_action, snapshot.next_action)}",
        f"חסר: {missing_he or 'אין'}",
        f"מעקב: {follow_up_line}",
        f"פגישה: {meeting_line}",
        f"עסקה: {deal_line}",
        f"שיחה חסומה: {killed_line}",
        "לא ביצעתי כלום ולא שלחתי הודעה.",
    ]
    return "\n".join(lines)


def apply_lead_review_policy(
    store: LeadStore,
    *,
    snapshot: LeadReviewSnapshot,
    kill_switch: bool,
    demo_active: bool,
) -> None:
    if demo_active or kill_switch:
        return
    try:
        assert_allowed(
            RiskAction(name="lead_review_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    store.upsert_lead_review(
        lead_id=snapshot.lead_id,
        stage=snapshot.stage,
        fit=snapshot.fit,
        pain_level=snapshot.pain_level,
        next_action=snapshot.next_action,
        missing_fields=snapshot.missing_fields,
        follow_up_status=snapshot.follow_up_status,
        follow_up_due_at=snapshot.follow_up_due_at,
        meeting_status=snapshot.meeting_status,
        deal_stage=snapshot.deal_stage,
        conversation_killed=snapshot.conversation_killed,
    )


def apply_owner_lead_review(
    store: LeadStore,
    *,
    text: str,
    kill_switch: bool,
    demo_active: bool,
) -> str | None:
    """Return Hebrew snapshot ack, unknown-lead ack, or None when demo / no lead_id."""
    if demo_active:
        return None
    lead_id = extract_review_lead_id(text)
    if lead_id is None:
        query = _strip_review_phrases(text)
        if not query:
            return None
        hits = store.find_leads(query)
        if len(hits) != 1:
            return format_lead_matches(store, query)
        lead_id = hits[0].lead_id
    snapshot = build_lead_review_snapshot(store, lead_id=lead_id)
    if snapshot is None:
        return (
            "מה שהבנתי: סקירת ליד. לא מצאתי את הליד. אני לא מבצעת כלום."
        )
    apply_lead_review_policy(
        store,
        snapshot=snapshot,
        kill_switch=kill_switch,
        demo_active=demo_active,
    )
    return format_lead_review(snapshot)


def format_lead_matches(store: LeadStore, query: str) -> str:
    """Name/headline search for the owner console. Never invents a name."""
    needle = query.strip()
    hits = store.find_leads(needle) if needle else []
    if len(hits) == 1:
        snapshot = build_lead_review_snapshot(store, lead_id=hits[0].lead_id)
        if snapshot is None:
            return "לא מצאתי את הליד. אני לא מבצעת כלום."
        return format_lead_review(snapshot)
    recent = store.list_sales_snapshots(limit=5)
    if not hits:
        lines = [
            "לא מצאתי ליד בשם הזה. לא ניחשתי.",
        ]
        if recent:
            lines.append("אחרונים:")
            lines.extend(
                lead_display(item.lead_id, item.headline, item.display_name)
                for item in recent
            )
        else:
            lines.append("אין לידים עדיין.")
        return "\n".join(lines)
    lines = ["כמה לידים מתאימים:"]
    lines.extend(
        lead_display(item.lead_id, item.headline, item.display_name) for item in hits
    )
    return "\n".join(lines)


def _strip_review_phrases(text: str) -> str:
    cleaned = text
    for phrase in (
        "lead review",
        "review lead",
        "tell me about lead",
        "tell me about the lead",
        "סקירת ליד",
        "מה המצב של הליד",
        "איפה הליד",
        "תספרי לי על ליד",
        "תספרי לי על הליד",
        "ספר לי על הליד",
        "תספר לי על הליד",
        "מי זה",
    ):
        cleaned = re.sub(re.escape(phrase), " ", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()
