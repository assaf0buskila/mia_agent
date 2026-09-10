from __future__ import annotations

import asyncio
import json
import logging
import re
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_transcription_port
from app.core.config import Settings, get_settings
from app.core.demo import demo_mode_active
from app.core.public_website import public_website_guard
from app.db.session import get_session_factory
from app.db.store import LeadStore
from app.domain.attribution import sanitize_attribution
from app.domain.behavior import CLIENT_BEHAVIOR_KINDS, sanitize_client_behavior
from app.domain.events import (
    Channel,
    build_attribution_event,
    build_behavior_event,
    build_message_out_event,
)
from app.domain.handoff.tokens import click_to_chat_url
from app.domain.tools import AdapterHttpError
from app.integrations.transcribe import TranscriptionError, TranscriptionPort, TranscriptResult
from app.surfaces.site_public import site_opening
from app.surfaces.site_v2 import (
    begin_site_message,
    complete_site_message_error,
    create_site_session,
    finish_site_session,
    require_site_credential,
    run_site_v2_turn,
    site_delivery_status,
)

router = APIRouter(prefix="/v1/website", tags=["website"])
_log = logging.getLogger("mia.comm")
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
    session_credential: str


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    client_message_id: str = Field(default="", max_length=120)
    name: str = Field(default="", max_length=80)
    phone: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=120)
    date: str = Field(default="", max_length=40)


class MessageOut(BaseModel):
    lead_id: str
    next_action: str
    message: str
    delivery_status: str = "none"
    whatsapp_url: str | None = None


class VoiceMessageOut(MessageOut):
    heard: str


class WebsiteConfigOut(BaseModel):
    website_url: str
    public_base_url: str
    widget: str
    opening: str
    demo: bool
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


def _run_v2_message_transaction(
    *,
    settings: Settings,
    session_id: str,
    credential: str | None,
    client_message_id: str,
    payload: dict[str, str],
    text: str,
    transcript: TranscriptResult | None = None,
    error_message: str = "",
) -> tuple[dict[str, object], bool]:
    """Own the complete synchronous transaction on one worker thread."""
    with get_session_factory()() as worker_db, worker_db.begin():
        _state, replay = begin_site_message(
            worker_db,
            session_id=session_id,
            credential=credential,
            client_message_id=client_message_id,
            payload=payload,
        )
        if replay is not None:
            return replay, True
        if transcript is not None:
            LeadStore(worker_db).save_transcript(
                provider="website_v2",
                provider_event_id=f"{session_id}:v2:{client_message_id}",
                channel=Channel.WEBSITE.value,
                external_id=session_id,
                actor_role="prospect",
                transcript=text,
                stt_provider=transcript.stt_provider,
                stt_model=transcript.stt_model,
                language=transcript.language,
                duration_ms=transcript.duration_ms,
                confidence=transcript.confidence,
            )
        if error_message:
            out = complete_site_message_error(
                worker_db,
                session_id=session_id,
                credential=credential,
                client_message_id=client_message_id,
                message=error_message,
            )
        else:
            out = run_site_v2_turn(
                worker_db,
                settings=settings,
                session_id=session_id,
                credential=credential,
                client_message_id=client_message_id,
                text=text,
                name=payload.get("name", ""),
                phone=payload.get("phone", ""),
                email=payload.get("email", ""),
                date=payload.get("date", ""),
            )
        return {
            "lead_id": "",
            "next_action": out.next_action,
            "message": out.message,
            "delivery_status": out.delivery_status or "none",
            "whatsapp_url": out.whatsapp_url,
        }, False


def _create_v2_handoff_transaction(
    *, session_id: str, credential: str | None, settings: Settings
) -> HandoffOut:
    with get_session_factory()() as worker_db, worker_db.begin():
        row = require_site_credential(worker_db, session_id, credential, lock=True)
        try:
            state = json.loads(row.state_json or "{}")
        except (TypeError, ValueError):
            state = {}
        if not isinstance(state, dict) or not state.get("captured"):
            raise HTTPException(status_code=409, detail="phone or email required")
        store = LeadStore(worker_db)
        raw_token, expires_at = store.issue_handoff_token(session_id, session_id)
        _persist_behavior(store, session_id=session_id, payload={"kind": "whatsapp_handoff"})
        return HandoffOut(
            token=raw_token,
            expires_at=expires_at,
            whatsapp_url=click_to_chat_url(settings.whatsapp_click_to_chat, raw_token) or None,
            notification_status=site_delivery_status(worker_db, row),
        )


def _finish_v2_session_transaction(*, session_id: str, credential: str | None) -> bool:
    with get_session_factory()() as worker_db, worker_db.begin():
        return finish_site_session(worker_db, session_id, credential)


def _require_v2_session(
    db: Session, session_id: str, credential: str | None, *, lock: bool = False
):
    store = LeadStore(db)
    if not store.website_session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    try:
        return require_site_credential(db, session_id, credential, lock=lock)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=401,
                detail="session credential required; start a new session",
            ) from exc
        raise


def _persist_behavior(store: LeadStore, *, session_id: str, payload: dict[str, str]) -> None:
    store.save_canonical_event(
        provider="website_v2",
        event=build_behavior_event(session_id=session_id, lead_id="", payload=payload),
    )


def _safe_acquisition_context(raw: dict[str, str | None]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        try:
            cleaned.update(sanitize_attribution({key: value}))
        except ValueError:
            continue
    result: dict[str, str] = {}
    for key, value in cleaned.items():
        decoded = unquote(value)
        if any(word in decoded.casefold() for word in ("token", "secret", "password")):
            continue
        if any(ord(char) < 32 for char in decoded) or "@" in decoded:
            continue
        if key.startswith("utm_") and not re.fullmatch(r"[\w\- ]{1,80}", decoded):
            continue
        result[key] = value
    return result


def _normalize_voice_mime(content_type: str | None) -> str:
    raw = (content_type or "").split(";")[0].strip().lower()
    if not raw:
        return "audio/webm"
    if raw not in _VOICE_MIME_ALLOW:
        raise HTTPException(status_code=415, detail="unsupported audio type")
    if raw in {"video/webm", "application/octet-stream"}:
        return "audio/webm"
    return raw


_AUDIO_MAGIC: tuple[tuple[bytes, int, str], ...] = (
    (b"OggS", 0, "audio/ogg"),
    (b"\x1a\x45\xdf\xa3", 0, "audio/webm"),
    (b"ftyp", 4, "audio/mp4"),
    (b"moov", 4, "audio/mp4"),
    (b"RIFF", 0, "audio/wav"),
    (b"ID3", 0, "audio/mpeg"),
)


def sniff_audio_container(audio: bytes) -> str:
    if not audio:
        return ""
    for signature, offset, mime in _AUDIO_MAGIC:
        if audio[offset : offset + len(signature)] == signature:
            return mime
    if len(audio) >= 2 and audio[0] == 0xFF and (audio[1] & 0xF6) == 0xF0:
        return "audio/aac"
    if len(audio) >= 2 and audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    return ""


def _voice_filename(mime: str) -> str:
    return {
        "audio/mp4": "note.mp4",
        "audio/mpeg": "note.mp3",
        "audio/mp3": "note.mp3",
        "audio/ogg": "note.ogg",
        "audio/wav": "note.wav",
        "audio/x-wav": "note.wav",
        "audio/aac": "note.m4a",
        "audio/m4a": "note.m4a",
    }.get(mime, "note.webm")


def _log_voice_failure(
    *, session_id: str, reason: str, mime: str, size_bytes: int, detail: str = ""
) -> None:
    _log.warning(
        "website voice failed session=%s reason=%s mime=%s bytes=%s detail=%s",
        session_id,
        reason,
        mime,
        size_bytes,
        detail or "-",
    )


async def _read_audio_capped(upload: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="audio too large")
        chunks.append(chunk)
    return b"".join(chunks)


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
    "/sessions", response_model=SessionOut, dependencies=[Depends(public_website_guard("session"))]
)
def create_session(
    db: Session = Depends(get_db),
    utm_source: str | None = Query(None, max_length=200),
    utm_medium: str | None = Query(None, max_length=200),
    utm_campaign: str | None = Query(None, max_length=200),
    utm_content: str | None = Query(None, max_length=200),
    landing_page: str | None = Query(None, max_length=200),
    referrer: str | None = Query(None, max_length=200),
    page_section: str | None = Query(None, max_length=200),
) -> SessionOut:
    session_id = str(uuid4())
    store = LeadStore(db)
    customer_id = store.open_website_session(session_id)
    lead_id = ""
    safe_attribution = _safe_acquisition_context(
        {
            "utm_source": utm_source,
            "utm_medium": utm_medium,
            "utm_campaign": utm_campaign,
            "utm_content": utm_content,
            "landing_page": landing_page,
            "referrer": referrer,
        }
    )
    credential, _row = create_site_session(
        db,
        session_id=session_id,
        page={
            "utm_source": utm_source or "",
            "utm_medium": utm_medium or "",
            "utm_campaign": utm_campaign or "",
            "utm_content": utm_content or "",
            "landing_page": landing_page or "",
            "referrer": referrer or "",
            "page_section": page_section or "",
        },
    )
    store.save_canonical_event(
        provider="website_v2",
        event=build_message_out_event(
            provider="website_v2",
            channel=Channel.WEBSITE,
            inbound_provider_event_id=f"{session_id}:open",
            conversation_id=session_id,
            text=site_opening(),
        ),
    )
    _persist_behavior(store, session_id=session_id, payload={"kind": "mia_opened"})
    if page_section:
        payload = sanitize_client_behavior(kind="section_view", section=page_section)
        if payload is not None:
            _persist_behavior(store, session_id=session_id, payload=payload)
    if safe_attribution:
        store.save_canonical_event(
                provider="website_v2",
                event=build_attribution_event(
                    provider="website_v2",
                    channel=Channel.WEBSITE,
                    conversation_id=session_id,
                    lead_id=None,
                    payload=safe_attribution,
                ),
        )
    return SessionOut(
        session_id=session_id,
        lead_id=lead_id,
        customer_id=customer_id,
        session_credential=credential,
    )


@router.post(
    "/sessions/{session_id}/handoff",
    response_model=HandoffOut,
    dependencies=[Depends(public_website_guard("handoff"))],
)
async def create_handoff(
    session_id: str,
    session_credential: str | None = Header(None, alias="X-Mia-Session-Credential"),
    db: Session = Depends(get_db),
) -> HandoffOut:
    _require_v2_session(db, session_id, session_credential)
    db.rollback()
    return await asyncio.to_thread(
        _create_v2_handoff_transaction,
        session_id=session_id,
        credential=session_credential,
        settings=get_settings(),
    )


@router.post(
    "/sessions/{session_id}/events",
    response_model=BehaviorEventOut,
    dependencies=[Depends(public_website_guard("event"))],
)
def post_behavior_event(
    session_id: str,
    body: BehaviorEventIn,
    session_credential: str | None = Header(None, alias="X-Mia-Session-Credential"),
    db: Session = Depends(get_db),
) -> BehaviorEventOut:
    if body.kind not in CLIENT_BEHAVIOR_KINDS:
        raise HTTPException(status_code=422, detail="invalid behavior kind")
    _require_v2_session(db, session_id, session_credential)
    payload = sanitize_client_behavior(
        kind=body.kind, path=body.path, section=body.section, cta=body.cta
    )
    if payload is None:
        return BehaviorEventOut(accepted=False, kind=body.kind)
    _persist_behavior(LeadStore(db), session_id=session_id, payload=payload)
    return BehaviorEventOut(accepted=True, kind=body.kind)


@router.post(
    "/sessions/{session_id}/end",
    response_model=EndSessionOut,
    dependencies=[Depends(public_website_guard("end"))],
)
async def end_session(
    session_id: str,
    session_credential: str | None = Header(None, alias="X-Mia-Session-Credential"),
    db: Session = Depends(get_db),
) -> EndSessionOut:
    _require_v2_session(db, session_id, session_credential)
    db.rollback()
    finalized = await asyncio.to_thread(
        _finish_v2_session_transaction, session_id=session_id, credential=session_credential
    )
    return EndSessionOut(accepted=True, finalized=finalized)


@router.post(
    "/sessions/{session_id}/messages",
    response_model=MessageOut,
    dependencies=[Depends(public_website_guard("message"))],
)
async def post_message(
    session_id: str,
    body: MessageIn,
    session_credential: str | None = Header(None, alias="X-Mia-Session-Credential"),
    db: Session = Depends(get_db),
) -> MessageOut:
    _require_v2_session(db, session_id, session_credential)
    if not body.client_message_id.strip():
        raise HTTPException(status_code=422, detail="client_message_id required")
    db.rollback()
    result, _replayed = await asyncio.to_thread(
        _run_v2_message_transaction,
        settings=get_settings(),
        session_id=session_id,
        credential=session_credential,
        client_message_id=body.client_message_id,
        payload={
            "text": body.text,
            "name": body.name,
            "phone": body.phone,
            "email": body.email,
            "date": body.date,
        },
        text=body.text,
    )
    return MessageOut.model_validate(result)


def _voice_error_message() -> str:
    return "לא הצלחתי לשמוע את ההקלטה. אפשר לנסות שוב או לכתוב לי."


@router.post(
    "/sessions/{session_id}/voice",
    response_model=VoiceMessageOut,
    dependencies=[Depends(public_website_guard("voice"))],
)
async def post_voice(
    session_id: str,
    session_credential: str | None = Header(None, alias="X-Mia-Session-Credential"),
    client_message_id: str = Form(""),
    db: Session = Depends(get_db),
    transcribe_port: TranscriptionPort = Depends(get_transcription_port),
    file: UploadFile = File(...),
) -> VoiceMessageOut:
    _require_v2_session(db, session_id, session_credential)
    if not client_message_id.strip():
        raise HTTPException(status_code=422, detail="client_message_id required")
    db.rollback()
    claimed = _normalize_voice_mime(file.content_type)
    audio = await _read_audio_capped(file)
    if not audio:
        raise HTTPException(status_code=400, detail="empty audio")
    audio_bytes = len(audio)
    payload = {"audio_sha256": sha256(audio).hexdigest(), "mime": claimed}
    sniffed = sniff_audio_container(audio)
    mime = sniffed or claimed
    if sniffed and sniffed != claimed:
        _log.info(
            "website voice container relabelled session=%s claimed=%s actual=%s bytes=%s",
            session_id,
            claimed,
            sniffed,
            audio_bytes,
        )
    try:
        try:
            transcript = await transcribe_port.transcribe(
                audio=audio, mime_type=mime, filename=_voice_filename(mime)
            )
        except RuntimeError:
            _log_voice_failure(
                session_id=session_id,
                reason="stt_not_configured",
                mime=mime,
                size_bytes=audio_bytes,
            )
            failed, _replayed = await asyncio.to_thread(
                _run_v2_message_transaction,
                settings=get_settings(),
                session_id=session_id,
                credential=session_credential,
                client_message_id=client_message_id,
                payload=payload,
                text="",
                error_message=_voice_error_message(),
            )
            return VoiceMessageOut(heard="", **MessageOut.model_validate(failed).model_dump())
        except (TranscriptionError, AdapterHttpError) as exc:
            _log_voice_failure(
                session_id=session_id,
                reason="provider_error",
                mime=mime,
                size_bytes=audio_bytes,
                detail=type(exc).__name__,
            )
            failed, _replayed = await asyncio.to_thread(
                _run_v2_message_transaction,
                settings=get_settings(),
                session_id=session_id,
                credential=session_credential,
                client_message_id=client_message_id,
                payload=payload,
                text="",
                error_message=_voice_error_message(),
            )
            return VoiceMessageOut(heard="", **MessageOut.model_validate(failed).model_dump())
    finally:
        del audio
    text = (transcript.text or "").strip()
    if not text:
        _log_voice_failure(
            session_id=session_id, reason="empty_transcript", mime=mime, size_bytes=audio_bytes
        )
        failed, _replayed = await asyncio.to_thread(
            _run_v2_message_transaction,
            settings=get_settings(),
            session_id=session_id,
            credential=session_credential,
            client_message_id=client_message_id,
            payload=payload,
            text="",
            error_message=_voice_error_message(),
        )
        return VoiceMessageOut(heard="", **MessageOut.model_validate(failed).model_dump())
    text = text[:4000]
    out, replayed = await asyncio.to_thread(
        _run_v2_message_transaction,
        settings=get_settings(),
        session_id=session_id,
        credential=session_credential,
        client_message_id=client_message_id,
        payload=payload,
        text=text,
        transcript=transcript,
    )
    return VoiceMessageOut(
        heard="" if replayed else text,
        **MessageOut.model_validate(out).model_dump(),
    )
