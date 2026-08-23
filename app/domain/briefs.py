"""Pre-meeting brief persistence (§12.2): snapshot on offer_meeting, optional research."""

from __future__ import annotations

import json
from datetime import datetime
from time import perf_counter
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.ai_runs import elapsed_ms
from app.domain.approvals import extract_approval_lead_id
from app.domain.company import sanitize_company_domain
from app.domain.events import Channel, build_meeting_brief_event
from app.domain.meeting_slots import normalize_scheduled_at_utc, to_utc_aware
from app.domain.sales import MEDDPICC_MISSING_ORDER, FitLevel, NextAction, SalesState
from app.domain.tools import AdapterHttpError, ToolOutcome
from app.integrations.calendar import format_slot_time
from app.integrations.research import MAX_QUERY_LEN, MAX_TITLE_LEN, sanitize_snippets

if TYPE_CHECKING:
    from app.db.store import LeadStore
    from app.integrations.research import ResearchPort

extract_meeting_brief_lead_id = extract_approval_lead_id

OWNER_QUESTION_KEYS = ("decision_maker", "timeline", "metric")
_MEETING_STATUS_BOOKED = "booked"
_NEXT_ACTION = "offer_meeting"
_MEETING_RESEARCH_TOOL = "meeting_research"
_FIT_HE = {
    FitLevel.UNKNOWN.value: "לא ידועה",
    FitLevel.POOR.value: "חלשה",
    FitLevel.POSSIBLE.value: "אפשרית",
    FitLevel.GOOD.value: "טובה",
}
_NEXT_ACTION_HE = {
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
_MISSING_FIELD_HE = {
    "decision_maker": "מקבל החלטות",
    "timeline": "לוח זמנים",
    "metric": "מדד",
}


def build_meeting_brief(*, sales: SalesState, channel: str) -> dict[str, Any]:
    """Build sanitized meeting brief from SalesState. No PII or message text."""
    missing_fields = [
        name
        for name in sales.missing_fields
        if isinstance(name, str) and name in MEDDPICC_MISSING_ORDER
    ]
    missing_fields = [name for name in MEDDPICC_MISSING_ORDER if name in missing_fields]
    owner_questions = [name for name in missing_fields if name in OWNER_QUESTION_KEYS]
    return {
        "channel": channel,
        "fit": sales.fit.value,
        "pain_level": int(sales.pain_level),
        "workflow_known": sales.workflow_known,
        "impact_confirmed": sales.impact_confirmed,
        "reflected": sales.reflected,
        "hypothesis_offered": sales.hypothesis_offered,
        "buying_reality_known": sales.buying_reality_known,
        "authority_known": sales.authority_known,
        "timeline_known": sales.timeline_known,
        "metric_known": sales.metric_known,
        "willingness_to_meet": sales.willingness_to_meet,
        "owner_required": sales.owner_required,
        "active_objection": (
            sales.active_objection.value if sales.active_objection is not None else None
        ),
        "missing_fields": missing_fields,
        "owner_questions": owner_questions,
        "next_action": _NEXT_ACTION,
    }


def _host_from_https_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    return hostname.lower()


def _research_sources_from_snippets(snippets: list) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    for snippet in snippets:
        host = sanitize_company_domain(_host_from_https_url(snippet.url))
        if not host:
            continue
        title = " ".join(snippet.title.split())[:MAX_TITLE_LEN]
        if not title:
            continue
        sources.append({"title": title, "host": host})
        if len(sources) >= 2:
            break
    return sources


def _parse_existing_brief_payload(existing_payload_json: str | None) -> dict[str, Any]:
    if not existing_payload_json:
        return {}
    try:
        payload = json.loads(existing_payload_json)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _sanitize_stored_sources(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    sources: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        raw_title = item.get("title")
        raw_host = item.get("host")
        if not isinstance(raw_title, str) or not isinstance(raw_host, str):
            continue
        title = " ".join(raw_title.split())[:MAX_TITLE_LEN]
        host = sanitize_company_domain(raw_host)
        if not title or host is None:
            continue
        sources.append({"title": title, "host": host})
        if len(sources) >= 2:
            break
    return sources


def _research_already_attempted(existing: dict[str, Any], domain: str) -> bool:
    return (
        existing.get("company_domain") == domain
        and existing.get("research_attempted") is True
    )


def _run_meeting_research(
    *,
    domain: str,
    port: ResearchPort,
    kill_switch: bool,
) -> tuple[list[dict[str, str]], ToolOutcome]:
    try:
        assert_allowed(
            RiskAction(name="meeting_research_read", risk=RiskLevel.R0_READ),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return [], ToolOutcome(
            tool=_MEETING_RESEARCH_TOOL,
            status="denied",
            result_count=0,
        )
    try:
        started = perf_counter()
        raw = port.search(domain[:MAX_QUERY_LEN])
        latency = elapsed_ms(started)
        snippets = sanitize_snippets(raw)
        sources = _research_sources_from_snippets(snippets)
        if sources:
            return sources, ToolOutcome(
                tool=_MEETING_RESEARCH_TOOL,
                status="ok",
                result_count=len(sources),
                latency_ms=latency,
            )
        return [], ToolOutcome(
            tool=_MEETING_RESEARCH_TOOL,
            status="empty",
            result_count=0,
            latency_ms=latency,
        )
    except AdapterHttpError as exc:
        return [], ToolOutcome(
            tool=_MEETING_RESEARCH_TOOL,
            status=exc.tool_status(),
            result_count=0,
            latency_ms=elapsed_ms(started),
        )
    except (RuntimeError, PolicyDenied, ValueError, OSError):
        return [], ToolOutcome(
            tool=_MEETING_RESEARCH_TOOL,
            status="error",
            result_count=0,
            latency_ms=elapsed_ms(started),
        )


def _build_storage_payload(
    *,
    base_brief: dict[str, Any],
    domain: str,
    existing: dict[str, Any],
    research_port: ResearchPort | None,
    kill_switch: bool,
) -> tuple[dict[str, Any], ToolOutcome | None]:
    if not domain:
        return dict(base_brief), None

    storage = dict(base_brief)
    storage["company_domain"] = domain

    if _research_already_attempted(existing, domain):
        storage["research_attempted"] = True
        storage["research_sources"] = _sanitize_stored_sources(
            existing.get("research_sources")
        )
        return storage, None

    if research_port is None:
        return storage, None

    sources, outcome = _run_meeting_research(
        domain=domain,
        port=research_port,
        kill_switch=kill_switch,
    )
    storage["research_attempted"] = True
    storage["research_sources"] = sources
    return storage, outcome


def apply_meeting_brief_policy(
    store,
    *,
    lead_id: str,
    channel: Channel,
    action: str,
    sales: SalesState,
    kill_switch: bool,
    research_port: ResearchPort | None = None,
) -> ToolOutcome | None:
    """Persist meeting brief on offer_meeting. Never sends; swallows PolicyDenied only."""
    action_key = str(action).lower().strip()
    if action_key != _NEXT_ACTION:
        return None

    domain = sanitize_company_domain(sales.company_domain or "") or ""
    base_brief = build_meeting_brief(sales=sales, channel=channel.value)
    existing_row = store.get_meeting_brief(lead_id)
    existing_payload = _parse_existing_brief_payload(
        existing_row.payload_json if existing_row is not None else None
    )

    if kill_switch:
        if domain and research_port is not None:
            return ToolOutcome(
                tool=_MEETING_RESEARCH_TOOL,
                status="denied",
                result_count=0,
            )
        return None

    try:
        assert_allowed(
            RiskAction(name="meeting_brief_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return None

    storage_payload, research_outcome = _build_storage_payload(
        base_brief=base_brief,
        domain=domain,
        existing=existing_payload,
        research_port=research_port,
        kill_switch=kill_switch,
    )
    storage_payload = _preserve_booked_stamp(storage_payload, existing_payload)
    event = build_meeting_brief_event(
        provider=channel.value,
        channel=channel,
        lead_id=lead_id,
        payload=base_brief,
    )
    store.upsert_meeting_brief(
        lead_id=lead_id,
        channel=channel.value,
        payload_json=json.dumps(storage_payload),
    )
    store.save_canonical_event(
        provider=channel.value,
        event=event,
    )
    return research_outcome


def _format_brief_scheduled_at(scheduled_at: str, timezone: str) -> str:
    normalized = normalize_scheduled_at_utc(scheduled_at)
    if normalized is None:
        return ""
    start = to_utc_aware(datetime.fromisoformat(normalized))
    if start is None:
        return ""
    return format_slot_time(start, timezone)


def format_owner_meeting_brief(
    payload: dict[str, Any],
    *,
    lead_id: str,
    timezone: str,
) -> str:
    """Hebrew owner pull from stored brief payload only. No PII or links."""
    lines = [f"תקציר פגישה {lead_id}"]
    channel = payload.get("channel")
    if isinstance(channel, str) and channel:
        lines.append(f"ערוץ: {channel}")
    fit = payload.get("fit")
    if isinstance(fit, str) and fit:
        lines.append(f"התאמה: {_FIT_HE.get(fit, fit)}")
    pain = payload.get("pain_level")
    if isinstance(pain, int):
        lines.append(f"כאב: P{pain}")
    next_action = payload.get("next_action")
    if isinstance(next_action, str) and next_action:
        lines.append(
            f"פעולה: {_NEXT_ACTION_HE.get(next_action, next_action)}"
        )
    missing_fields = payload.get("missing_fields")
    owner_questions = payload.get("owner_questions")
    missing_names: list[str] = []
    if isinstance(missing_fields, list):
        missing_names = [
            name
            for name in missing_fields
            if isinstance(name, str) and name in MEDDPICC_MISSING_ORDER
        ]
    question_names: list[str] = []
    if isinstance(owner_questions, list):
        question_names = [
            name
            for name in owner_questions
            if isinstance(name, str) and name in OWNER_QUESTION_KEYS
        ]
    if question_names:
        questions_he = ", ".join(
            _MISSING_FIELD_HE[name] for name in question_names
        )
        lines.append(f"שאלות: {questions_he}")
    elif missing_names:
        missing_he = ", ".join(
            _MISSING_FIELD_HE[name] for name in missing_names
        )
        lines.append(f"חסר: {missing_he}")
    objection = payload.get("active_objection")
    if isinstance(objection, str) and objection:
        lines.append(f"התנגדות: {objection}")
    meeting_status = payload.get("meeting_status")
    if meeting_status == _MEETING_STATUS_BOOKED:
        lines.append("סטטוס: נקבעה")
    scheduled_at = payload.get("scheduled_at")
    if isinstance(scheduled_at, str) and scheduled_at:
        when = _format_brief_scheduled_at(scheduled_at, timezone)
        if when:
            lines.append(f"מועד: {when}")
    for source in _sanitize_stored_sources(payload.get("research_sources")):
        lines.append(f"{source['title']} ({source['host']})")
    lines.append("לא ביצעתי כלום ולא שלחתי הודעה.")
    return "\n".join(lines)


def _preserve_booked_stamp(
    payload: dict[str, Any], existing: dict[str, Any]
) -> dict[str, Any]:
    """Keep booked time if a later offer_meeting upsert rebuilds the snapshot."""
    merged = dict(payload)
    merged.pop("meet_link", None)
    if existing.get("meeting_status") != _MEETING_STATUS_BOOKED:
        return merged
    merged["meeting_status"] = _MEETING_STATUS_BOOKED
    raw = existing.get("scheduled_at")
    if isinstance(raw, str):
        normalized = normalize_scheduled_at_utc(raw)
        if normalized is not None:
            merged["scheduled_at"] = normalized
    return merged


def persist_booked_meeting_brief(
    store: LeadStore,
    *,
    lead_id: str,
    scheduled_at: str,
    kill_switch: bool,
    demo_active: bool,
) -> None:
    """Stamp existing offer brief with booked status and time. No canonical event."""
    if demo_active or kill_switch:
        return
    try:
        assert_allowed(
            RiskAction(name="meeting_brief_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    normalized = normalize_scheduled_at_utc(scheduled_at)
    if normalized is None:
        return
    row = store.get_meeting_brief(lead_id)
    if row is None:
        return
    existing = _parse_existing_brief_payload(row.payload_json)
    merged = dict(existing)
    merged.pop("meet_link", None)
    merged["meeting_status"] = _MEETING_STATUS_BOOKED
    merged["scheduled_at"] = normalized
    store.upsert_meeting_brief(
        lead_id=lead_id,
        channel=row.channel,
        payload_json=json.dumps(merged),
    )


def apply_owner_meeting_brief(
    store: LeadStore,
    *,
    text: str,
    timezone: str,
    kill_switch: bool,
    demo_active: bool,
) -> str | None:
    """Return Hebrew brief ack, unknown-lead ack, or None when demo / no lead_id."""
    del kill_switch
    if demo_active:
        return None
    lead_id = extract_meeting_brief_lead_id(text)
    if lead_id is None:
        return None
    row = store.get_meeting_brief(lead_id)
    if row is None or store.get_lead(lead_id) is None:
        return (
            "מה שהבנתי: תקציר פגישה. לא מצאתי תקציר לליד הזה. "
            "אני לא מבצעת כלום."
        )
    payload = _parse_existing_brief_payload(row.payload_json)
    return format_owner_meeting_brief(payload, lead_id=lead_id, timezone=timezone)
