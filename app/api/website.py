from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agents.client.graph import compile_client_graph
from app.agents.shared.state import empty_client_state
from app.api.deps import (
    get_calendar_booking_port,
    get_calendar_port,
    get_db,
    get_sheets_port,
    get_transcription_port,
)
from app.brain.context import assemble_visitor_context, render_visitor_knowledge_block
from app.brain.embeddings import build_embedding_port
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.channels.website import message_to_client_state
from app.core.config import Settings, get_settings
from app.core.demo import SYNTHETIC_ATTRIBUTION, demo_mode_active
from app.core.logging import log_comm
from app.core.public_website import public_website_guard
from app.db.store import LeadStore
from app.domain.ai_runs import elapsed_ms, persist_ai_run
from app.domain.approvals import apply_approval_policy
from app.domain.attribution import sanitize_attribution
from app.domain.behavior import CLIENT_BEHAVIOR_KINDS, sanitize_client_behavior
from app.domain.briefs import apply_meeting_brief_policy
from app.domain.calendar_booking import resolve_meeting_reply
from app.domain.conversation_kill import apply_conversation_kill_policy
from app.domain.deals import apply_deal_policy
from app.domain.events import (
    Channel,
    build_attribution_event,
    build_behavior_event,
    build_message_in_event,
    build_message_out_event,
    persist_tool_outcome,
    sheets_mirror_outcome,
    stamp_correlation,
    transcription_outcome,
)
from app.domain.followups import apply_follow_up_policy
from app.domain.handoff import click_to_chat_digits, compose_handoff_text
from app.domain.meetings import apply_meeting_policy
from app.domain.sales import NextAction
from app.domain.website_handoff_brief import (
    apply_website_whatsapp_handoff_brief,
)
from app.graph.replies import WEBSITE_REPLIES
from app.integrations.calendar import CalendarPort
from app.integrations.calendar_booking import CalendarBookingPort
from app.integrations.research import build_research_port
from app.integrations.sales_reply import build_sales_reply_port
from app.integrations.sheets import (
    DealMirrorRow,
    FollowUpMirrorRow,
    LeadMirrorRow,
    MeetingMirrorRow,
    SheetsPort,
    SourceMirrorRow,
    activity_mirror_row_from_persisted,
    build_sheets_port,
    claim_sheets_mirror,
    complete_sheets_mirror,
    maybe_mirror_weekly_kpi,
    mirror_activity,
    mirror_deal,
    mirror_follow_up,
    mirror_lead,
    mirror_meeting,
    mirror_source,
)
from app.integrations.transcribe import (
    TranscriptionPort,
    TranscriptResult,
)

router = APIRouter(prefix="/v1/website", tags=["website"])
_WIDGET_PATH = Path(__file__).resolve().parent.parent / "web" / "ask_mia.js"
_MAX_AUDIO_BYTES = 16_000_000
_VOICE_MIME_ALLOW = frozenset(
    {
        "audio/webm",
        "audio/mp4",
        "audio/mpeg",
        "audio/mp3",
        "audio/ogg",
        "audio/wav",
        "audio/x-wav",
        "audio/aac",
        "audio/m4a",
        "video/webm",
        "application/octet-stream",
    }
)
_WIDGET_PREVIEW = """<!doctype html>
<html lang="he" dir="rtl">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ask Mia</title>
<body style="margin:1rem;background:#fff;color:#1a1a1a;font:16px/1.5 system-ui,sans-serif">
<p>תצוגת מיה המקומית. הכפתור בפינה. זה לא האתר.</p>
<script src="/v1/website/widget.js" defer></script>
</body>
</html>
"""


class SessionOut(BaseModel):
    session_id: str
    lead_id: str
    customer_id: str


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class MessageOut(BaseModel):
    lead_id: str
    next_action: str
    message: str


class VoiceMessageOut(MessageOut):
    heard: str


class WebsiteConfigOut(BaseModel):
    website_url: str
    public_base_url: str
    widget: str
    opening: str
    demo: bool


class HandoffOut(BaseModel):
    token: str
    expires_at: str
    whatsapp_url: str | None


class BehaviorEventIn(BaseModel):
    kind: str = Field(max_length=40)
    path: str | None = Field(None, max_length=200)
    section: str | None = Field(None, max_length=200)
    cta: str | None = Field(None, max_length=200)


class BehaviorEventOut(BaseModel):
    accepted: bool
    kind: str


class EndSessionOut(BaseModel):
    accepted: bool
    finalized: bool


def _normalize_voice_mime(content_type: str | None) -> str:
    raw = (content_type or "").split(";")[0].strip().lower()
    if not raw:
        return "audio/webm"
    if raw not in _VOICE_MIME_ALLOW:
        raise HTTPException(status_code=415, detail="unsupported audio type")
    if raw in {"video/webm", "application/octet-stream"}:
        return "audio/webm"
    return raw


def _voice_filename(mime: str) -> str:
    if mime == "audio/mp4":
        return "note.mp4"
    if mime in {"audio/mpeg", "audio/mp3"}:
        return "note.mp3"
    if mime == "audio/ogg":
        return "note.ogg"
    if mime in {"audio/wav", "audio/x-wav"}:
        return "note.wav"
    if mime in {"audio/aac", "audio/m4a"}:
        return "note.m4a"
    return "note.webm"


async def _read_audio_capped(upload: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_AUDIO_BYTES:
            del chunks
            raise HTTPException(status_code=413, detail="audio too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _persist_behavior(
    store: LeadStore,
    *,
    session_id: str,
    lead_id: str,
    payload: dict[str, str],
) -> None:
    store.save_canonical_event(
        provider="website",
        event=build_behavior_event(
            session_id=session_id,
            lead_id=lead_id,
            payload=payload,
        ),
    )


def _session_attribution_raw(
    settings: Settings,
    *,
    utm_source: str | None,
    utm_medium: str | None,
    utm_campaign: str | None,
    utm_content: str | None,
    landing_page: str | None,
    referrer: str | None,
) -> dict[str, str | None]:
    if demo_mode_active(settings):
        return dict(SYNTHETIC_ATTRIBUTION)
    return {
        "utm_source": utm_source,
        "utm_medium": utm_medium,
        "utm_campaign": utm_campaign,
        "utm_content": utm_content,
        "landing_page": landing_page,
        "referrer": referrer,
    }


def process_website_session(
    store: LeadStore,
    *,
    settings: Settings,
    sheets: SheetsPort | None = None,
    utm_source: str | None = None,
    utm_medium: str | None = None,
    utm_campaign: str | None = None,
    utm_content: str | None = None,
    landing_page: str | None = None,
    referrer: str | None = None,
) -> SessionOut:
    session_id = f"web_{uuid4().hex[:16]}"
    customer_id, lead_id = store.open_channel_lead(channel=Channel.WEBSITE, external_id=session_id)
    attribution = sanitize_attribution(
        _session_attribution_raw(
            settings,
            utm_source=utm_source,
            utm_medium=utm_medium,
            utm_campaign=utm_campaign,
            utm_content=utm_content,
            landing_page=landing_page,
            referrer=referrer,
        )
    )
    if attribution:
        store.save_canonical_event(
            provider="website",
            event=build_attribution_event(
                provider="website",
                channel=Channel.WEBSITE,
                lead_id=lead_id,
                conversation_id=session_id,
                payload=attribution,
            ),
        )
    if not demo_mode_active(settings):
        sheets_port = sheets if sheets is not None else build_sheets_port(settings)
        if claim_sheets_mirror(store=store, inbound_id=session_id, tab="session"):
            started = perf_counter()
            source_written = False
            if attribution:
                source_written = mirror_source(
                    sheets=sheets_port,
                    row=SourceMirrorRow(
                        lead_id=lead_id,
                        utm_source=attribution.get("utm_source", ""),
                        utm_medium=attribution.get("utm_medium", ""),
                        utm_campaign=attribution.get("utm_campaign", ""),
                        utm_content=attribution.get("utm_content", ""),
                        landing_page=attribution.get("landing_page", ""),
                        referrer=attribution.get("referrer", ""),
                    ),
                    kill_switch=settings.kill_switch,
                )
            kpi_written = maybe_mirror_weekly_kpi(
                store=store,
                sheets=sheets_port,
                settings=settings,
                kill_switch=settings.kill_switch,
            )
            persist_tool_outcome(
                store,
                provider="website",
                channel=Channel.WEBSITE,
                inbound_provider_event_id=session_id,
                conversation_id=session_id,
                lead_id=lead_id,
                outcome=sheets_mirror_outcome(
                    int(source_written) + int(kpi_written),
                    latency_ms=elapsed_ms(started),
                ),
            )
            complete_sheets_mirror(store=store, inbound_id=session_id, tab="session")
    _persist_behavior(
        store,
        session_id=session_id,
        lead_id=lead_id,
        payload={"kind": "mia_opened"},
    )
    return SessionOut(session_id=session_id, lead_id=lead_id, customer_id=customer_id)


def _website_knowledge_lookup(
    store: LeadStore, settings: Settings
) -> Callable[[str], tuple[str, ...]]:
    """Knowledge-only lookup for the visitor's latest message (see `app.brain.context`).

    Shares the same SQLAlchemy session as `store` rather than opening a second one.
    `assemble_visitor_context` never touches owner memory (hard safety invariant); any
    failure here is the caller's problem to swallow, not this function's.
    """
    brain_store = BrainStore(store.session)
    embedding_port = build_embedding_port(settings)

    def lookup(query: str) -> tuple[str, ...]:
        context = assemble_visitor_context(
            brain_store, query=query, embedding_port=embedding_port
        )
        return render_visitor_knowledge_block(context)

    return lookup


def process_website_message(
    store: LeadStore,
    *,
    session_id: str,
    text: str,
    settings: Settings,
    calendar: CalendarPort,
    calendar_booking: CalendarBookingPort,
    sheets: SheetsPort,
    audio_meta: TranscriptResult | None = None,
    stt_latency_ms: int = 0,
) -> MessageOut:
    turn_started = perf_counter()
    _customer_id, lead_id = store.open_channel_lead(channel=Channel.WEBSITE, external_id=session_id)
    run_id = f"run_{uuid4().hex[:12]}"
    provider_event_id = f"{session_id}:{uuid4().hex[:12]}"
    website_message_in = build_message_in_event(
        provider="website",
        channel=Channel.WEBSITE,
        provider_event_id=provider_event_id,
        conversation_id=session_id,
        text=text,
        actor_role="prospect",
        lead_id=lead_id,
    )
    stamp_correlation(website_message_in, run_id)
    store.save_canonical_event(
        provider="website",
        event=website_message_in,
    )
    _persist_behavior(
        store,
        session_id=session_id,
        lead_id=lead_id,
        payload={"kind": "conversation_started"},
    )
    if audio_meta is not None:
        store.save_transcript(
            provider="website",
            provider_event_id=provider_event_id,
            channel=Channel.WEBSITE.value,
            external_id=session_id,
            actor_role="prospect",
            transcript=text,
            stt_provider=audio_meta.stt_provider,
            stt_model=audio_meta.stt_model,
            language=audio_meta.language,
            duration_ms=audio_meta.duration_ms,
            confidence=audio_meta.confidence,
        )
        if text.strip():
            persist_tool_outcome(
                store,
                provider="website",
                channel=Channel.WEBSITE,
                inbound_provider_event_id=provider_event_id,
                conversation_id=session_id,
                lead_id=lead_id,
                outcome=transcription_outcome(
                    transcribed=True,
                    latency_ms=stt_latency_ms,
                ),
                correlation_id=run_id,
            )
    page_payload = store.latest_behavior_payload(session_id, "page_viewed")
    section_payload = store.latest_behavior_payload(session_id, "section_viewed")
    page_path = page_payload.get("path", "") if page_payload else ""
    page_section = section_payload.get("section", "") if section_payload else ""
    # Website visitors are client trust, always. Derived here, at the transport edge.
    graph = compile_client_graph(
        store,
        principal=Principal.client(source="website", actor_id=session_id),
        reply_port=build_sales_reply_port(settings),
        settings=settings,
        knowledge_lookup=_website_knowledge_lookup(store, settings),
    )
    started = perf_counter()
    result = graph.invoke(
        message_to_client_state(
            run_id=run_id,
            session_id=session_id,
            lead_id=lead_id,
            text=text,
            kill_switch=settings.kill_switch,
            page_path=page_path,
            page_section=page_section,
            inbound_id=provider_event_id,
            meeting_first=settings.website_meeting_first,
        )
    )
    persist_ai_run(
        store,
        run_id=run_id,
        lead_id=lead_id,
        channel=Channel.WEBSITE.value,
        next_action=result.get("next_action", ""),
        kill_switch=settings.kill_switch,
        sales_model=settings.sales_model,
        openai_api_key=settings.openai_api_key,
        sales_fallback_model=settings.sales_fallback_model,
        gemini_api_key=settings.gemini_api_key,
        sales_gemini_model=settings.sales_gemini_model,
        latency_ms=elapsed_ms(started),
        tokens_in=result.get("tokens_in", 0),
        tokens_out=result.get("tokens_out", 0),
        automation_mode=settings.automation_mode.value,
    )
    apply_follow_up_policy(
        store,
        lead_id=lead_id,
        channel=Channel.WEBSITE,
        action=result.get("next_action", ""),
        sales=store.get_sales(lead_id),
        timezone=settings.calendar_timezone,
        kill_switch=settings.kill_switch,
        inbound_id=provider_event_id,
    )
    opt_out_outcome = apply_conversation_kill_policy(
        store,
        lead_id=lead_id,
        action=result.get("next_action", ""),
    )
    research_port = build_research_port(settings)
    meeting_research_outcome = apply_meeting_brief_policy(
        store,
        lead_id=lead_id,
        channel=Channel.WEBSITE,
        action=result.get("next_action", ""),
        sales=store.get_sales(lead_id),
        kill_switch=settings.kill_switch,
        research_port=research_port,
    )
    apply_meeting_policy(
        store,
        lead_id=lead_id,
        channel=Channel.WEBSITE,
        action=result.get("next_action", ""),
        kill_switch=settings.kill_switch,
    )
    apply_approval_policy(
        store,
        lead_id=lead_id,
        channel=Channel.WEBSITE,
        action=result.get("next_action", ""),
        sales=store.get_sales(lead_id),
        kill_switch=settings.kill_switch,
    )
    apply_deal_policy(
        store,
        lead_id=lead_id,
        channel=Channel.WEBSITE,
        action=result.get("next_action", ""),
        kill_switch=settings.kill_switch,
    )
    message, calendar_outcomes, _meeting_changed = resolve_meeting_reply(
        store,
        lead_id=lead_id,
        channel=Channel.WEBSITE,
        provider="website",
        conversation_id=session_id,
        inbound_provider_event_id=provider_event_id,
        message=text,
        base_reply=result.get("reply", ""),
        next_action=result.get("next_action", ""),
        calendar=calendar,
        booking_port=calendar_booking,
        kill_switch=settings.kill_switch,
        timezone=settings.calendar_timezone,
        demo_active=demo_mode_active(settings),
    )
    if not demo_mode_active(settings):
        if claim_sheets_mirror(store=store, inbound_id=provider_event_id, tab="sales"):
            started = perf_counter()
            sales = store.get_sales(lead_id)
            sheets_written = mirror_lead(
                sheets=sheets,
                row=LeadMirrorRow(
                    lead_id=lead_id,
                    channel="website",
                    stage=store.get_lead_stage(lead_id),
                    fit=sales.fit.value,
                    pain_level=int(sales.pain_level),
                    next_action=result.get("next_action", ""),
                ),
                kill_switch=settings.kill_switch,
            )
            fu_written = False
            fu = store.get_follow_up(lead_id)
            if fu is not None:
                fu_written = mirror_follow_up(
                    sheets=sheets,
                    row=FollowUpMirrorRow(
                        lead_id=lead_id,
                        due_at=fu.due_at,
                        channel=fu.channel,
                        status=fu.status,
                        result=fu.reason,
                    ),
                    kill_switch=settings.kill_switch,
                )
            deal_written = False
            deal = store.get_deal(lead_id)
            if deal is not None:
                deal_written = mirror_deal(
                    sheets=sheets,
                    row=DealMirrorRow(
                        lead_id=lead_id,
                        stage=deal.stage,
                        source=deal.source,
                        attribution_confidence=deal.attribution_confidence,
                        expected_value=deal.expected_value,
                        closed_value=deal.closed_value,
                    ),
                    kill_switch=settings.kill_switch,
                )
            meeting_written = False
            meeting = store.get_meeting(lead_id)
            if meeting is not None:
                meeting_written = mirror_meeting(
                    sheets=sheets,
                    row=MeetingMirrorRow(
                        lead_id=lead_id,
                        status=meeting.status,
                        source=meeting.source,
                        scheduled_at=meeting.scheduled_at,
                        calendar_event_id=meeting.calendar_event_id,
                        summary=meeting.summary,
                    ),
                    kill_switch=settings.kill_switch,
                )
            activity_written = False
            ai = store.get_ai_run(run_id)
            if ai is not None:
                activity_row = activity_mirror_row_from_persisted(
                    run_id=ai.run_id,
                    lead_id=ai.lead_id,
                    channel=ai.channel,
                    next_action=ai.next_action,
                    model=ai.model,
                    kill_switch=ai.kill_switch,
                    cost_usd=ai.cost_usd,
                    timezone=settings.calendar_timezone,
                )
                if activity_row is not None:
                    activity_written = mirror_activity(
                        sheets=sheets,
                        row=activity_row,
                        kill_switch=settings.kill_switch,
                    )
            kpi_written = maybe_mirror_weekly_kpi(
                store=store,
                sheets=sheets,
                settings=settings,
                kill_switch=settings.kill_switch,
            )
            persist_tool_outcome(
                store,
                provider="website",
                channel=Channel.WEBSITE,
                inbound_provider_event_id=provider_event_id,
                conversation_id=session_id,
                lead_id=lead_id,
                outcome=sheets_mirror_outcome(
                    int(sheets_written)
                    + int(fu_written)
                    + int(deal_written)
                    + int(meeting_written)
                    + int(activity_written)
                    + int(kpi_written),
                    latency_ms=elapsed_ms(started),
                ),
                correlation_id=run_id,
            )
            complete_sheets_mirror(store=store, inbound_id=provider_event_id, tab="sales")
    for calendar_outcome in calendar_outcomes:
        persist_tool_outcome(
            store,
            provider="website",
            channel=Channel.WEBSITE,
            inbound_provider_event_id=provider_event_id,
            conversation_id=session_id,
            lead_id=lead_id,
            outcome=calendar_outcome,
            correlation_id=run_id,
        )
    if meeting_research_outcome is not None:
        persist_tool_outcome(
            store,
            provider="website",
            channel=Channel.WEBSITE,
            inbound_provider_event_id=provider_event_id,
            conversation_id=session_id,
            lead_id=lead_id,
            outcome=meeting_research_outcome,
            correlation_id=run_id,
        )
    if opt_out_outcome is not None:
        persist_tool_outcome(
            store,
            provider="website",
            channel=Channel.WEBSITE,
            inbound_provider_event_id=provider_event_id,
            conversation_id=session_id,
            lead_id=lead_id,
            outcome=opt_out_outcome,
            correlation_id=run_id,
        )
    if message:
        website_message_out = build_message_out_event(
            provider="website",
            channel=Channel.WEBSITE,
            inbound_provider_event_id=provider_event_id,
            conversation_id=session_id,
            text=message,
            lead_id=lead_id,
        )
        stamp_correlation(website_message_out, run_id)
        store.save_canonical_event(
            provider="website",
            event=website_message_out,
        )
    if result.get("next_action") == NextAction.OFFER_WHATSAPP.value:
        _persist_behavior(
            store,
            session_id=session_id,
            lead_id=lead_id,
            payload={"kind": "whatsapp_handoff_offered"},
        )
    log_comm(
        channel=Channel.WEBSITE.value,
        provider="website",
        actor_type="business_lead",
        direction="in",
        external_message_id=provider_event_id,
        lead_id=lead_id,
        conversation_id=session_id,
        takeover_state=store.get_takeover_state(lead_id),
        policy_result=result.get("next_action", ""),
        latency_ms=elapsed_ms(turn_started),
        success=True,
        automation_mode=settings.automation_mode.value,
    )
    return MessageOut(
        lead_id=lead_id,
        next_action=result.get("next_action", ""),
        message=message,
    )


@router.get("/widget.js")
def website_widget() -> Response:
    return Response(
        content=_WIDGET_PATH.read_bytes(),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/preview")
def website_widget_preview() -> Response:
    return Response(
        content=_WIDGET_PREVIEW,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/config", response_model=WebsiteConfigOut)
def website_config() -> WebsiteConfigOut:
    live = get_settings()
    return WebsiteConfigOut(
        website_url=live.website_url,
        public_base_url=live.public_base_url,
        widget="ask_mia",
        opening=WEBSITE_REPLIES[NextAction.UNDERSTAND_WORKFLOW],
        demo=demo_mode_active(live),
    )


@router.post(
    "/sessions",
    response_model=SessionOut,
    dependencies=[Depends(public_website_guard("session"))],
)
def create_session(
    db: Session = Depends(get_db),
    sheets: SheetsPort = Depends(get_sheets_port),
    utm_source: str | None = Query(None, max_length=200),
    utm_medium: str | None = Query(None, max_length=200),
    utm_campaign: str | None = Query(None, max_length=200),
    utm_content: str | None = Query(None, max_length=200),
    landing_page: str | None = Query(None, max_length=200),
    referrer: str | None = Query(None, max_length=200),
) -> SessionOut:
    settings = get_settings()
    store = LeadStore(db)
    return process_website_session(
        store,
        settings=settings,
        sheets=sheets,
        utm_source=utm_source,
        utm_medium=utm_medium,
        utm_campaign=utm_campaign,
        utm_content=utm_content,
        landing_page=landing_page,
        referrer=referrer,
    )


@router.post(
    "/sessions/{session_id}/handoff",
    response_model=HandoffOut,
    dependencies=[Depends(public_website_guard("handoff"))],
)
def create_handoff(
    session_id: str,
    db: Session = Depends(get_db),
) -> HandoffOut:
    store = LeadStore(db)
    lead_id = store.get_website_lead_id(session_id)
    if lead_id is None:
        raise HTTPException(status_code=404, detail="session not found")
    settings = get_settings()
    raw_token, expires_at = store.issue_handoff_token(lead_id, session_id)
    _persist_behavior(
        store,
        session_id=session_id,
        lead_id=lead_id,
        payload={"kind": "whatsapp_handoff"},
    )
    apply_website_whatsapp_handoff_brief(
        store,
        lead_id=lead_id,
        session_id=session_id,
        settings=settings,
    )
    whatsapp_url: str | None = None
    digits = click_to_chat_digits(settings.whatsapp_click_to_chat)
    if digits:
        whatsapp_url = f"https://wa.me/{digits}?text={quote(compose_handoff_text(raw_token))}"
    return HandoffOut(token=raw_token, expires_at=expires_at, whatsapp_url=whatsapp_url)


@router.post("/sessions/{session_id}/events", response_model=BehaviorEventOut)
def post_behavior_event(
    session_id: str,
    body: BehaviorEventIn,
    db: Session = Depends(get_db),
) -> BehaviorEventOut:
    if body.kind not in CLIENT_BEHAVIOR_KINDS:
        raise HTTPException(status_code=422, detail="invalid behavior kind")
    store = LeadStore(db)
    lead_id = store.get_website_lead_id(session_id)
    if lead_id is None:
        raise HTTPException(status_code=404, detail="session not found")
    payload = sanitize_client_behavior(
        kind=body.kind,
        path=body.path,
        section=body.section,
        cta=body.cta,
    )
    if payload is None:
        return BehaviorEventOut(accepted=False, kind=body.kind)
    _persist_behavior(
        store,
        session_id=session_id,
        lead_id=lead_id,
        payload=payload,
    )
    return BehaviorEventOut(accepted=True, kind=body.kind)


@router.post(
    "/sessions/{session_id}/end",
    response_model=EndSessionOut,
    # Same bind as the other public POSTs: /end is unauthenticated and triggers
    # finalization -- a summary plus a Telegram push to Assaf. Left open it is both a
    # spam vector into his phone and unmetered work per request.
    dependencies=[Depends(public_website_guard("end"))],
)
def end_session(
    session_id: str,
    db: Session = Depends(get_db),
) -> EndSessionOut:
    store = LeadStore(db)
    lead_id = store.get_website_lead_id(session_id)
    if lead_id is None:
        raise HTTPException(status_code=404, detail="session not found")
    result = compile_client_graph(
        store,
        settings=get_settings(),
        principal=Principal.client(source="website", actor_id=session_id),
    ).invoke(
        empty_client_state(
            run_id=f"end:{session_id}",
            conversation_id=session_id,
            visitor_id=session_id,
            lead_id=lead_id,
            turn_kind="session_end",
        )
    )
    return EndSessionOut(
        accepted=True,
        finalized=bool(result.get("finalized")),
    )


@router.post(
    "/sessions/{session_id}/messages",
    response_model=MessageOut,
    dependencies=[Depends(public_website_guard("message"))],
)
def post_message(
    session_id: str,
    body: MessageIn,
    db: Session = Depends(get_db),
    calendar: CalendarPort = Depends(get_calendar_port),
    calendar_booking: CalendarBookingPort = Depends(get_calendar_booking_port),
    sheets: SheetsPort = Depends(get_sheets_port),
) -> MessageOut:
    settings = get_settings()
    store = LeadStore(db)
    if store.get_website_lead_id(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return process_website_message(
        store,
        session_id=session_id,
        text=body.text,
        settings=settings,
        calendar=calendar,
        calendar_booking=calendar_booking,
        sheets=sheets,
    )


@router.post(
    "/sessions/{session_id}/voice",
    response_model=VoiceMessageOut,
    dependencies=[Depends(public_website_guard("voice"))],
)
async def post_voice(
    session_id: str,
    db: Session = Depends(get_db),
    calendar: CalendarPort = Depends(get_calendar_port),
    calendar_booking: CalendarBookingPort = Depends(get_calendar_booking_port),
    sheets: SheetsPort = Depends(get_sheets_port),
    transcribe_port: TranscriptionPort = Depends(get_transcription_port),
    file: UploadFile = File(...),
) -> VoiceMessageOut:
    settings = get_settings()
    if settings.kill_switch:
        raise HTTPException(status_code=503, detail="killed")
    store = LeadStore(db)
    if store.get_website_lead_id(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    mime = _normalize_voice_mime(file.content_type)
    audio = await _read_audio_capped(file)
    if not audio:
        raise HTTPException(status_code=400, detail="empty audio")
    started = perf_counter()
    try:
        try:
            result = await transcribe_port.transcribe(
                audio=audio,
                mime_type=mime,
                filename=_voice_filename(mime),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="transcription unavailable") from exc
    finally:
        del audio
    text = (result.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="empty transcript")
    if len(text) > 4000:
        text = text[:4000]
    out = process_website_message(
        store,
        session_id=session_id,
        text=text,
        settings=settings,
        calendar=calendar,
        calendar_booking=calendar_booking,
        sheets=sheets,
        audio_meta=result,
        stt_latency_ms=elapsed_ms(started),
    )
    return VoiceMessageOut(
        lead_id=out.lead_id,
        next_action=out.next_action,
        message=out.message,
        heard=text,
    )
