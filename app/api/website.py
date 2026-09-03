from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import (
    get_db,
    get_sheets_port,
    get_telegram_port,
    get_transcription_port,
)
from app.core.config import Settings, get_settings
from app.core.demo import demo_mode_active
from app.core.logging import log_comm
from app.core.public_website import public_website_guard
from app.db.store import LeadStore
from app.domain.ai_runs import elapsed_ms
from app.domain.behavior import CLIENT_BEHAVIOR_KINDS, sanitize_client_behavior
from app.domain.events import (
    Channel,
    build_behavior_event,
    build_message_in_event,
    build_message_out_event,
    persist_tool_outcome,
    stamp_correlation,
    transcription_outcome,
)
from app.domain.handoff import click_to_chat_url
from app.domain.tools import AdapterHttpError
from app.integrations.base import MessagePort
from app.integrations.sheets import SheetsPort
from app.integrations.transcribe import (
    TranscriptionError,
    TranscriptionPort,
    TranscriptResult,
)
from app.surfaces.crm import build_contacts_crm
from app.surfaces.site import (
    ping_assaf_async,
    run_site_turn,
    site_book,
    site_opening,
)
from app.surfaces.site_policy import (
    KNOWLEDGE_TOOL,
    PublishedFact,
    classify_site_intent,
    facts_from_knowledge_hits,
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
    name: str = Field(default="", max_length=80)
    phone: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=120)
    date: str = Field(default="", max_length=40)


class MessageOut(BaseModel):
    lead_id: str
    next_action: str
    message: str
    whatsapp_url: str | None = None


class VoiceMessageOut(MessageOut):
    heard: str


class WebsiteConfigOut(BaseModel):
    website_url: str
    public_base_url: str
    widget: str
    opening: str
    demo: bool
    # WhatsApp is offered only after phone or email exists. Config never pre-shows it.
    whatsapp_url: str | None = None


class HandoffOut(BaseModel):
    token: str
    expires_at: str
    whatsapp_url: str | None
    notification_status: str


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
    payload: dict[str, str],
) -> None:
    store.save_canonical_event(
        provider="website",
        event=build_behavior_event(
            session_id=session_id,
            lead_id="",
            payload=payload,
        ),
    )


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
    del sheets
    session_id = f"web_{uuid4().hex[:16]}"
    customer_id = store.open_website_session(session_id)
    site_book().open(session_id)
    incoming = build_message_in_event(
        provider="website",
        channel=Channel.WEBSITE,
        provider_event_id=f"{session_id}:open",
        conversation_id=session_id,
        text="",
        actor_role="prospect",
        lead_id=None,
    )
    store.save_canonical_event(provider="website", event=incoming)
    del settings, utm_source, utm_medium, utm_campaign, utm_content, landing_page, referrer
    return SessionOut(session_id=session_id, lead_id="", customer_id=customer_id)


def process_website_message(
    store: LeadStore,
    *,
    session_id: str,
    text: str,
    settings: Settings,
    sheets: SheetsPort,
    audio_meta: TranscriptResult | None = None,
    stt_latency_ms: int = 0,
    name: str = "",
    phone: str = "",
    email: str = "",
    date: str = "",
    owner_port: MessagePort | None = None,
    voice_failed: bool = False,
) -> MessageOut:
    del owner_port
    if not store.website_session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    if site_book().get(session_id) is None:
        site_book().open(session_id)
    turn_started = perf_counter()
    facts, tools_ran = _published_facts_for_turn(store, text, voice_failed=voice_failed)
    crm = build_contacts_crm(settings, sheets)
    turn = run_site_turn(
        session_id=session_id,
        text=text,
        settings=settings,
        crm=crm,
        name=name,
        phone=phone,
        email=email,
        date=date,
        facts=facts,
        tools_ran=tools_ran,
        voice_failed=voice_failed,
    )
    run_id = f"run_{uuid4().hex[:12]}"
    provider_event_id = f"{session_id}:{uuid4().hex[:12]}"
    website_message_in = build_message_in_event(
        provider="website",
        channel=Channel.WEBSITE,
        provider_event_id=provider_event_id,
        conversation_id=session_id,
        text=text,
        actor_role="prospect",
        lead_id=None,
    )
    stamp_correlation(website_message_in, run_id)
    store.save_canonical_event(
        provider="website",
        event=website_message_in,
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
                lead_id=None,
                outcome=transcription_outcome(
                    transcribed=True,
                    latency_ms=stt_latency_ms,
                ),
                correlation_id=run_id,
            )
    message = (turn.reply or "").strip()
    if not message:
        from app.surfaces.site_policy import never_silent

        message = never_silent("", "he")
    if message:
        website_message_out = build_message_out_event(
            provider="website",
            channel=Channel.WEBSITE,
            inbound_provider_event_id=provider_event_id,
            conversation_id=session_id,
            text=message,
            lead_id=None,
        )
        stamp_correlation(website_message_out, run_id)
        store.save_canonical_event(provider="website", event=website_message_out)
    log_comm(
        channel=Channel.WEBSITE.value,
        provider="website",
        actor_type="business_lead",
        direction="in",
        external_message_id=provider_event_id,
        conversation_id=session_id,
        policy_result=turn.next_action,
        latency_ms=elapsed_ms(turn_started),
        success=True,
        automation_mode=settings.automation_mode.value,
    )
    return MessageOut(
        lead_id="",
        next_action=turn.next_action,
        message=message,
        whatsapp_url=turn.whatsapp_url,
    )


def _published_facts_for_turn(
    store: LeadStore,
    text: str,
    *,
    voice_failed: bool,
) -> tuple[tuple[PublishedFact, ...], tuple[str, ...]]:
    """Look up assafweb.com facts only when the turn needs them. Never GSC or JSON-LD."""
    if voice_failed or not text.strip():
        return (), ()
    intent = classify_site_intent(text)
    if intent not in {"price", "need", "other", "metric"}:
        return (), ()
    try:
        from app.brain.context import retrieve_knowledge
        from app.brain.embeddings import build_embedding_port
        from app.brain.store import BrainStore

        hits = retrieve_knowledge(
            BrainStore(store.session),
            query=text,
            embedding_port=build_embedding_port(get_settings()),
            limit=3,
        )
    except Exception:
        return (), ()
    return facts_from_knowledge_hits(hits), (KNOWLEDGE_TOOL,)


async def _maybe_ping_owner(
    *,
    session_id: str,
    settings: Settings,
    owner_port: MessagePort | None,
    force: bool = False,
) -> bool:
    if owner_port is None:
        return False
    session = site_book().get(session_id)
    if session is None or session.pinged or not session.fields.has_phone_or_email():
        return False
    if not force and not session.awaiting_ping and not session.confirmed:
        return False
    sent = await ping_assaf_async(settings, owner_port, session)
    if sent:
        session.pinged = True
        session.awaiting_ping = False
    return sent


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
        opening=site_opening(),
        demo=demo_mode_active(live),
        whatsapp_url=None,
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
async def create_handoff(
    session_id: str,
    db: Session = Depends(get_db),
    owner_port: MessagePort = Depends(get_telegram_port),
) -> HandoffOut:
    store = LeadStore(db)
    if not store.website_session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    session = site_book().get(session_id)
    if session is None or not session.fields.has_phone_or_email():
        raise HTTPException(status_code=409, detail="phone or email required")
    settings = get_settings()
    raw_token, expires_at = store.issue_handoff_token(session_id, session_id)
    _persist_behavior(
        store,
        session_id=session_id,
        payload={"kind": "whatsapp_handoff"},
    )
    pinged = await _maybe_ping_owner(
        session_id=session_id,
        settings=settings,
        owner_port=owner_port,
        force=True,
    )
    notification_status = "delivered" if pinged else "failed"
    log_comm(
        channel=Channel.WEBSITE.value,
        provider="telegram",
        actor_type="owner_notification",
        direction="out",
        external_message_id="website_whatsapp_handoff",
        policy_result=notification_status,
        success=pinged,
        automation_mode=settings.automation_mode.value,
    )
    whatsapp_url = click_to_chat_url(settings.whatsapp_click_to_chat, raw_token) or None
    return HandoffOut(
        token=raw_token,
        expires_at=expires_at,
        whatsapp_url=whatsapp_url,
        notification_status=notification_status,
    )


@router.post("/sessions/{session_id}/events", response_model=BehaviorEventOut)
def post_behavior_event(
    session_id: str,
    body: BehaviorEventIn,
    db: Session = Depends(get_db),
) -> BehaviorEventOut:
    if body.kind not in CLIENT_BEHAVIOR_KINDS:
        raise HTTPException(status_code=422, detail="invalid behavior kind")
    store = LeadStore(db)
    if not store.website_session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    payload = sanitize_client_behavior(
        kind=body.kind,
        path=body.path,
        section=body.section,
        cta=body.cta,
    )
    if payload is None:
        return BehaviorEventOut(accepted=False, kind=body.kind)
    _persist_behavior(store, session_id=session_id, payload=payload)
    return BehaviorEventOut(accepted=True, kind=body.kind)


@router.post(
    "/sessions/{session_id}/end",
    response_model=EndSessionOut,
    dependencies=[Depends(public_website_guard("end"))],
)
async def end_session(
    session_id: str,
    db: Session = Depends(get_db),
    owner_port: MessagePort = Depends(get_telegram_port),
) -> EndSessionOut:
    store = LeadStore(db)
    if not store.website_session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    session = site_book().get(session_id)
    if session is None:
        site_book().open(session_id)
        session = site_book().get(session_id)
    assert session is not None
    visitor_turns = [role for role, _text in session.turns if role == "visitor"]
    if session.finalized or not visitor_turns:
        return EndSessionOut(accepted=True, finalized=False)
    session.finalized = True
    await _maybe_ping_owner(
        session_id=session_id,
        settings=get_settings(),
        owner_port=owner_port,
    )
    return EndSessionOut(accepted=True, finalized=True)


@router.post(
    "/sessions/{session_id}/messages",
    response_model=MessageOut,
    dependencies=[Depends(public_website_guard("message"))],
)
async def post_message(
    session_id: str,
    body: MessageIn,
    db: Session = Depends(get_db),
    sheets: SheetsPort = Depends(get_sheets_port),
    owner_port: MessagePort = Depends(get_telegram_port),
) -> MessageOut:
    settings = get_settings()
    store = LeadStore(db)
    if not store.website_session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    out = process_website_message(
        store,
        session_id=session_id,
        text=body.text,
        settings=settings,
        sheets=sheets,
        name=body.name,
        phone=body.phone,
        email=body.email,
        date=body.date,
    )
    await _maybe_ping_owner(
        session_id=session_id,
        settings=settings,
        owner_port=owner_port,
    )
    return out


@router.post(
    "/sessions/{session_id}/voice",
    response_model=VoiceMessageOut,
    dependencies=[Depends(public_website_guard("voice"))],
)
async def post_voice(
    session_id: str,
    db: Session = Depends(get_db),
    sheets: SheetsPort = Depends(get_sheets_port),
    transcribe_port: TranscriptionPort = Depends(get_transcription_port),
    owner_port: MessagePort = Depends(get_telegram_port),
    file: UploadFile = File(...),
) -> VoiceMessageOut:
    settings = get_settings()
    store = LeadStore(db)
    if not store.website_session_exists(session_id):
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
        except RuntimeError:
            out = process_website_message(
                store,
                session_id=session_id,
                text="",
                settings=settings,
                sheets=sheets,
                voice_failed=True,
            )
            await _maybe_ping_owner(
                session_id=session_id,
                settings=settings,
                owner_port=owner_port,
            )
            return VoiceMessageOut(
                lead_id=out.lead_id,
                next_action=out.next_action,
                message=out.message,
                whatsapp_url=out.whatsapp_url,
                heard="",
            )
        except (TranscriptionError, AdapterHttpError) as exc:
            raise HTTPException(status_code=503, detail="transcription unavailable") from exc
    finally:
        del audio
    text = (result.text or "").strip()
    if not text:
        out = process_website_message(
            store,
            session_id=session_id,
            text="",
            settings=settings,
            sheets=sheets,
            voice_failed=True,
        )
        await _maybe_ping_owner(
            session_id=session_id,
            settings=settings,
            owner_port=owner_port,
        )
        return VoiceMessageOut(
            lead_id=out.lead_id,
            next_action=out.next_action,
            message=out.message,
            whatsapp_url=out.whatsapp_url,
            heard="",
        )
    if len(text) > 4000:
        text = text[:4000]
    out = process_website_message(
        store,
        session_id=session_id,
        text=text,
        settings=settings,
        sheets=sheets,
        audio_meta=result,
        stt_latency_ms=elapsed_ms(started),
    )
    await _maybe_ping_owner(
        session_id=session_id,
        settings=settings,
        owner_port=owner_port,
    )
    return VoiceMessageOut(
        lead_id=out.lead_id,
        next_action=out.next_action,
        message=out.message,
        whatsapp_url=out.whatsapp_url,
        heard=text,
    )
