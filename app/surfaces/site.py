"""Site selling surface. Identify the visitor, then sell. No invented prices."""

from __future__ import annotations

import inspect
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from app.core.config import Settings
from app.core.errors import MiaError
from app.domain.handoff.tokens import click_to_chat_url
from app.domain.memory import ConversationTurn
from app.domain.tools import AdapterHttpError
from app.integrations.base import MessagePort, OutboundMessage
from app.integrations.sales_reply import SalesReplyPort
from app.services.notifications import OwnerTelegramDelivery
from app.surfaces.crm import ContactRecord, ContactsCrm, log_contact
from app.surfaces.identity import CapturedFields, apply_form
from app.surfaces.site_policy import (
    ASK_NEED_HE,
    ASSAFWEB_HOOK_HE,
    SITE_ACTIONS,
    PublishedFact,
    append_burst,
    classify_site_intent,
    decide_site_turn,
    is_filler,
    is_frustrated,
    is_nonlead,
    never_silent,
    pick_language,
    should_retrieve_published_facts,
)
from app.surfaces.site_reply import phrase_site_reply

# Durable at-most-once for the owner ping, injected by the API layer so this surface
# keeps no database dependency. claim(recipient_id) -> may I send to this owner;
# release(recipient_id) -> that send genuinely failed, let a later turn retry.
OwnerPingClaim = Callable[[str], bool]
OwnerPingRelease = Callable[[str], None]
OwnerPingConfirmed = Callable[[str], bool]

SITE_OPENING = "שלום, אני מיה. ספרו לי בקצרה מה אתם מחפשים."
ASK_CONTACT = "כדי שאסף יוכל להמשיך אתכם, צריך טלפון או אימייל."
ASK_NEED = ASK_NEED_HE
AFTER_CAPTURE = (
    "תודה. העברתי לאסף את מה שסיפרתם, והוא ימשיך איתכם בוואטסאפ. "
    "אני לא ממציאה מחיר או התחייבות מכאן."
)
NO_PRICE = "אין מחיר מפורסם באתר assafweb.com לתת כאן. אסף יגיד."


@dataclass
class SiteSession:
    session_id: str
    fields: CapturedFields = field(default_factory=CapturedFields)
    turns: list[tuple[str, str]] = field(default_factory=list)
    burst_parts: list[tuple[float, str]] = field(default_factory=list)
    pinged: bool = False
    finalized: bool = False
    confirmed: bool = False
    selling_stopped: bool = False
    complaint_open: bool = False
    awaiting_ping: bool = False
    language: str = ""
    tools_ran: tuple[str, ...] = ()
    need_seen: bool = False
    nonlead: bool = False
    business_known: bool = False
    friction_known: bool = False
    value_shown: bool = False
    contact_requested: bool = False
    contact_captured: bool = False
    discovery_questions: int = 0
    last_question_topic: str = ""
    asked_topics: tuple[str, ...] = ()
    business_summary: str = ""
    friction_summary: str = ""
    page_path: str = ""
    page_section: str = ""
    acquisition_context: dict[str, str] = field(default_factory=dict)
    crm_written: bool = False
    conversion_reported: bool = False


@dataclass
class SiteTurn:
    reply: str
    next_action: str
    whatsapp_url: str | None
    crm_wrote: bool
    owner_pinged: bool
    fields: CapturedFields
    tools_ran: tuple[str, ...] = ()
    # What this turn actually cost. Surfaced so the live website turn can write an
    # ai_run row; the table used to be fed only by the muted WhatsApp path.
    tokens_in: int = 0
    tokens_out: int = 0
    model_reply_used: bool = False


class SiteBook:
    """Process-local session store. Guarded: turns run off the event loop."""

    def __init__(self) -> None:
        self._sessions: dict[str, SiteSession] = {}
        self._lock = threading.Lock()

    def open(self, session_id: str) -> SiteSession:
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing
            session = SiteSession(session_id=session_id)
            self._sessions[session_id] = session
            return session

    def get(self, session_id: str) -> SiteSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def replace(self, session: SiteSession) -> None:
        """Publish a completed snapshot without mutating an object another turn holds."""
        with self._lock:
            self._sessions[session.session_id] = session

    def exists(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions


_BOOK = SiteBook()


def site_book() -> SiteBook:
    return _BOOK


def reset_site_book() -> None:
    with _BOOK._lock:
        _BOOK._sessions.clear()


def site_opening() -> str:
    return SITE_OPENING


# Enough recent turns to write an honest owner summary. The model's own history comes
# from the canonical events, not from here.
_STATE_TURN_LIMIT = 12


def dump_site_session(session: SiteSession) -> str:
    """Everything that would otherwise die with the process."""
    fields = session.fields
    return json.dumps(
        {
            "fields": {
                "name": fields.name,
                "phone": fields.phone,
                "email": fields.email,
                "date": fields.date,
                "business": fields.business,
                "want": fields.want,
                "language": fields.language,
                "summary": fields.summary,
            },
            "pinged": session.pinged,
            # Without this a restart between two /end calls repeats finalization and
            # every handoff effect that hangs off it.
            "finalized": session.finalized,
            "confirmed": session.confirmed,
            "selling_stopped": session.selling_stopped,
            "complaint_open": session.complaint_open,
            "need_seen": session.need_seen,
            "nonlead": session.nonlead,
            "business_known": session.business_known,
            "friction_known": session.friction_known,
            "value_shown": session.value_shown,
            "contact_requested": session.contact_requested,
            "contact_captured": session.contact_captured,
            "conversion_reported": session.conversion_reported,
            "discovery_questions": session.discovery_questions,
            "last_question_topic": session.last_question_topic,
            "asked_topics": list(session.asked_topics),
            "business_summary": session.business_summary,
            "friction_summary": session.friction_summary,
            "page_path": session.page_path,
            "page_section": session.page_section,
            "acquisition_context": session.acquisition_context,
            "crm_written": session.crm_written,
            "language": session.language,
            "tools_ran": list(session.tools_ran),
            "turns": [[role, text] for role, text in session.turns[-_STATE_TURN_LIMIT:]],
        },
        ensure_ascii=False,
    )


def load_site_session(session: SiteSession, raw: str) -> bool:
    """Rehydrate after a restart. Malformed or partial state never breaks the turn."""
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    stored = data.get("fields")
    if isinstance(stored, dict):
        session.fields = CapturedFields(
            name=str(stored.get("name", "")),
            phone=str(stored.get("phone", "")),
            email=str(stored.get("email", "")),
            date=str(stored.get("date", "")),
            business=str(stored.get("business", "")),
            want=str(stored.get("want", "")),
            language=str(stored.get("language", "")),
            summary=str(stored.get("summary", "")),
        )
    session.pinged = bool(data.get("pinged"))
    session.finalized = bool(data.get("finalized"))
    session.confirmed = bool(data.get("confirmed"))
    session.selling_stopped = bool(data.get("selling_stopped"))
    session.complaint_open = bool(data.get("complaint_open"))
    session.need_seen = bool(data.get("need_seen"))
    session.nonlead = bool(data.get("nonlead"))
    session.business_known = bool(data.get("business_known"))
    session.friction_known = bool(data.get("friction_known"))
    session.value_shown = bool(data.get("value_shown"))
    session.contact_requested = bool(data.get("contact_requested"))
    session.contact_captured = bool(data.get("contact_captured"))
    session.conversion_reported = bool(data.get("conversion_reported"))
    try:
        session.discovery_questions = max(0, int(data.get("discovery_questions", 0) or 0))
    except (TypeError, ValueError):
        session.discovery_questions = 0
    session.last_question_topic = str(data.get("last_question_topic", "") or "")
    asked = data.get("asked_topics")
    if isinstance(asked, list):
        session.asked_topics = tuple(str(item) for item in asked)
    session.business_summary = str(data.get("business_summary", "") or "")
    session.friction_summary = str(data.get("friction_summary", "") or "")
    session.page_path = str(data.get("page_path", "") or "")
    session.page_section = str(data.get("page_section", "") or "")
    acquisition = data.get("acquisition_context")
    if isinstance(acquisition, dict):
        session.acquisition_context = {
            str(key): str(value)
            for key, value in acquisition.items()
            if isinstance(key, str) and isinstance(value, str)
        }
    session.crm_written = bool(data.get("crm_written"))
    session.language = str(data.get("language", "") or "")
    tools = data.get("tools_ran")
    if isinstance(tools, list):
        session.tools_ran = tuple(str(item) for item in tools)
    turns = data.get("turns")
    if isinstance(turns, list):
        session.turns = [
            (str(pair[0]), str(pair[1]))
            for pair in turns
            if isinstance(pair, list | tuple) and len(pair) == 2
        ]
    return True


def run_site_turn(
    *,
    session_id: str,
    text: str,
    settings: Settings,
    crm: ContactsCrm,
    owner_port: MessagePort | None = None,
    name: str = "",
    phone: str = "",
    email: str = "",
    date: str = "",
    book: SiteBook | None = None,
    facts: tuple[PublishedFact, ...] = (),
    tools_ran: tuple[str, ...] = (),
    now: float | None = None,
    voice_failed: bool = False,
    turns: tuple[ConversationTurn, ...] = (),
    reply_port: SalesReplyPort | None = None,
    defer: Callable[[Callable[[], None]], None] | None = None,
    claim_owner_ping: OwnerPingClaim | None = None,
    release_owner_ping: OwnerPingRelease | None = None,
) -> SiteTurn:
    """Answer first. CRM or WhatsApp only with phone or email. No invented prices."""
    store = book or _BOOK
    session = store.get(session_id)
    if session is None:
        raise KeyError(session_id)
    clock = time.monotonic() if now is None else now
    raw = text
    session.fields = apply_form(
        session.fields,
        name=name,
        phone=phone,
        email=email,
        date=date,
        text="" if voice_failed else raw,
        website=True,
    )
    thought = raw.strip()
    if not voice_failed:
        session.burst_parts, thought = append_burst(session.burst_parts, raw, now=clock)
    session.language = pick_language(thought or raw, session.language or session.fields.language)
    if (
        not voice_failed
        and not session.fields.want
        and thought
        and not _is_contact_only(thought)
        and _looks_like_need(thought)
    ):
        session.fields = CapturedFields(
            name=session.fields.name,
            phone=session.fields.phone,
            email=session.fields.email,
            date=session.fields.date,
            business=session.fields.business,
            want=thought[:200],
            language=session.language,
            summary=session.fields.summary,
        )
    elif session.language and not session.fields.language:
        session.fields = CapturedFields(
            name=session.fields.name,
            phone=session.fields.phone,
            email=session.fields.email,
            date=session.fields.date,
            business=session.fields.business,
            want=session.fields.want,
            language=session.language,
            summary=session.fields.summary,
        )
    if not voice_failed:
        session.turns.append(("visitor", raw.strip()))
    intent = classify_site_intent(thought or raw)
    if is_nonlead(raw):
        session.nonlead = True
    # Burst stitching is for phrasing context; state transitions must use the
    # current human message so three rapid messages cannot close two topics at once.
    _update_conversion_state(session, raw, intent)
    if tools_ran:
        session.tools_ran = tuple(dict.fromkeys((*session.tools_ran, *tools_ran)))
    named_tools = session.tools_ran if intent == "tool_status" else tools_ran
    # How far into the conversation we are, and whether they have told us it is going
    # badly. Both move the ladder off "ask another question".
    visitor_turns = sum(1 for role, _text in session.turns if role == "visitor")
    frustrated = not voice_failed and is_frustrated(thought or raw)
    if (
        intent == "need"
        and not is_nonlead(raw)
        and not should_retrieve_published_facts(raw, intent)
    ):
        session.need_seen = True
    decision = decide_site_turn(
        thought=thought or raw,
        language=session.language,
        has_contact=session.fields.has_phone_or_email(),
        already_confirmed=session.confirmed,
        selling_stopped=session.selling_stopped,
        already_pinged=session.pinged,
        facts=facts,
        tools_ran=named_tools,
        voice_failed=voice_failed,
        complaint_open=session.complaint_open,
        visitor_turns=visitor_turns,
        frustrated=frustrated,
        need_seen=session.need_seen,
        business_known=session.business_known,
        friction_known=session.friction_known,
        value_shown=session.value_shown,
        contact_requested=session.contact_requested,
        discovery_questions=session.discovery_questions,
        last_question_topic=session.last_question_topic,
        business_summary=session.business_summary,
        friction_summary=session.friction_summary,
        asked_topics=session.asked_topics,
        nonlead=session.nonlead,
    )
    if decision.stop_selling:
        session.selling_stopped = True
    if intent == "complaint":
        session.complaint_open = True
    if decision.confirm_contact:
        session.confirmed = True
    session.awaiting_ping = bool(decision.ping_assaf and not session.pinged)
    action = decision.action
    if action not in SITE_ACTIONS:
        action = "answer"
    if action in {"ask_need", "ask_contact"} and decision.ask_contact:
        session.contact_requested = True
    if decision.value_only:
        session.value_shown = True
        session.contact_requested = True
    if decision.question_topic:
        topic = decision.question_topic
        session.last_question_topic = topic
        if topic not in session.asked_topics:
            session.discovery_questions += 1
            session.asked_topics = (*session.asked_topics, topic)
    if session.fields.has_phone_or_email():
        session.contact_captured = True
    # `decide_site_turn` already chose the action. The port only phrases it, and falls
    # back to the exact canned line on every failure path.
    usage: dict[str, int] = {}
    phrased = phrase_site_reply(
        action=action,
        canned=decision.reply,
        latest_message=thought or raw,
        language=session.language,
        turns=turns,
        facts=facts,
        port=None if decision.question_topic or intent in {"legal", "abuse"} else reply_port,
        visitor_turns=visitor_turns,
        frustrated=frustrated,
        usage=usage,
        known_facts=tuple(
            item
            for item in (
                f"business_context: {session.business_summary}" if session.business_known else "",
                f"friction: {session.friction_summary}" if session.friction_known else "",
                "contact: captured" if session.contact_captured else "",
                "value_already_shown" if session.value_shown else "",
            )
            if item
        ),
        open_questions=tuple(
            item
            for item in (
                "business_context" if not session.business_known else "",
                "friction" if not session.friction_known else "",
                "contact" if session.value_shown and not session.contact_captured else "",
            )
            if item
        ),
        asked_actions=session.asked_topics,
        page_path=session.page_path,
        page_section=session.page_section,
        value_only=decision.value_only,
        answer_only=True,
    )
    reply = never_silent(phrased, session.language)
    if decision.value_only and not session.fields.has_phone_or_email():
        cta = (
            ASK_CONTACT
            if session.language != "en"
            else "To pass this to Assaf, I need a phone or email."
        )
        if cta not in reply:
            reply = f"{reply.rstrip('?!')} {cta}"
    session.turns.append(("mia", reply))
    crm_wrote = False
    owner_pinged = False
    wa_url = None
    if (
        session.fields.has_phone_or_email()
        and (decision.write_sheet or decision.ping_assaf)
        and not session.crm_written
    ):
        record = _contact_from_session(session)
        if defer is not None:
            # Two Google Sheets round trips at 20s each. The visitor never waits on them.
            def write_contact() -> None:
                if session.crm_written:
                    return
                log_contact(
                    crm,
                    record,
                    who="מיה",
                    channel="website",
                    action="שיחת אתר",
                    result="נרשם",
                )
                session.crm_written = True

            defer(write_contact)
        else:
            log_contact(
                crm,
                record,
                who="מיה",
                channel="website",
                action="שיחת אתר",
                result="נרשם",
            )
            session.crm_written = True
        crm_wrote = True
    if session.fields.has_phone_or_email() and action in {"confirm_contact", "handoff"}:
        wa_url = click_to_chat_url(settings.whatsapp_click_to_chat) or None
    if (
        session.fields.has_phone_or_email()
        and owner_port is not None
        and not session.pinged
        and decision.ping_assaf
    ):
        owner_pinged = _ping_assaf(
            settings,
            owner_port,
            session,
            claim=claim_owner_ping,
            release=release_owner_ping,
        )
        if owner_pinged:
            session.pinged = True
            session.confirmed = True
    return SiteTurn(
        reply=reply,
        next_action=action,
        whatsapp_url=wa_url,
        crm_wrote=crm_wrote,
        owner_pinged=owner_pinged,
        fields=session.fields,
        tools_ran=tools_ran,
        tokens_in=usage.get("tokens_in", 0),
        tokens_out=usage.get("tokens_out", 0),
        model_reply_used=bool(usage.get("model_reply_used", 0)),
    )


def _update_conversion_state(session: SiteSession, text: str, intent: str) -> None:
    """Close a topic from the visitor's substantive answer, not keyword luck."""
    clean = " ".join(text.split())[:240]
    if (
        session.nonlead
        or session.selling_stopped
        or session.complaint_open
        or not clean
        or _is_contact_only(clean)
        or is_nonlead(clean)
        or should_retrieve_published_facts(clean, intent)
        or intent
        in {
            "complaint",
            "legal",
            "abuse",
            "stop_sell",
            "bot",
            "privilege",
            "tool_status",
            "off_topic",
            "price",
            "metric",
            "ask_assaf",
            "voice_q",
            "voice_product",
        }
    ):
        return
    if is_filler(clean):
        return
    substantive = len(clean) >= 8
    if session.last_question_topic == "business" and substantive:
        session.business_known = True
        session.business_summary = clean
        session.last_question_topic = ""
    elif session.last_question_topic == "friction" and substantive:
        session.friction_known = True
        session.friction_summary = clean
        session.last_question_topic = ""
    business_markers = (
        "יש לי עסק",
        "אני עושה",
        "אני בעל",
        "אני מנהל",
        "אני מנהלת",
        "my business",
        "i run",
        "i own",
        "salon",
        "clinic",
        "סטודיו",
        "ציפורניים",
        "לק ג'ל",
        "nail",
    )
    if (
        not session.business_known
        and substantive
        and (intent == "need" or any(mark in clean.lower() for mark in business_markers))
    ):
        session.business_known = True
        session.business_summary = clean
    friction_markers = (
        "וואטסאפ",
        "whatsapp",
        "תורים",
        "הודעות",
        "לקוחות",
        "מתאמת",
        "מנהלת",
        "עונה",
        "עבודה ידנית",
        "ידנית",
        "manual",
        "missed calls",
        "מפספסים שיחות",
    )
    if (
        not session.friction_known
        and substantive
        and any(mark in clean.lower() for mark in friction_markers)
    ):
        if session.business_known:
            session.friction_known = True
            session.friction_summary = clean
    if session.friction_known:
        session.fields = CapturedFields(
            name=session.fields.name,
            phone=session.fields.phone,
            email=session.fields.email,
            date=session.fields.date,
            business=session.business_summary or session.fields.business,
            want=session.friction_summary or session.fields.want,
            language=session.fields.language,
            summary=session.fields.summary,
        )


def _looks_like_need(text: str) -> bool:
    return classify_site_intent(text) in {"need", "ask_assaf", "other", "voice_product"}


def _contact_from_session(session: SiteSession) -> ContactRecord:
    fields = session.fields
    return ContactRecord(
        name=fields.name,
        phone=fields.phone,
        email=fields.email,
        date=fields.date,
        source="website",
        language=fields.language or session.language,
        want=fields.want,
        status="פתוח",
        summary=_conversation_summary(session),
        next_step="אסף בוואטסאפ",
    )


def _conversation_summary(session: SiteSession) -> str:
    lines = [f"{role}: {text}" for role, text in session.turns[-8:] if text]
    return " | ".join(lines)[:400]


_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _is_contact_only(text: str) -> bool:
    stripped = text.strip()
    if "@" in stripped and _EMAIL_PATTERN.fullmatch(stripped):
        return True
    digits = "".join(ch for ch in stripped if ch.isdigit())
    starts_valid = stripped.startswith("+") or stripped.startswith("0")
    return len(digits) >= 9 and len(stripped) <= 24 and starts_valid


def _ping_assaf(
    settings: Settings,
    port: MessagePort,
    session: SiteSession,
    *,
    claim: OwnerPingClaim | None = None,
    release: OwnerPingRelease | None = None,
) -> bool:
    owners = settings.telegram_owner_user_id_set()
    if not owners:
        return False
    body = format_owner_ping(session)
    sent_any = False
    for owner_id in owners:
        if claim is not None and not claim(owner_id):
            continue
        try:
            result = port.send(  # type: ignore[unused-coroutine]
                OutboundMessage(
                    conversation_id=owner_id,
                    text=body,
                    channel="telegram",
                    idempotency_key=f"site-ping:{session.session_id}",
                )
            )
            if inspect.iscoroutine(result):
                # The sync sales function cannot execute an async transport. Close the
                # untouched coroutine and give the claim back; the API owns delivery.
                result.close()
                if release is not None:
                    release(owner_id)
                continue
            sent_any = True
        except (RuntimeError, MiaError):
            if release is not None:
                release(owner_id)
            continue
    return sent_any


def _telegram_rejection_status(exc: BaseException) -> int | None:
    cause = exc.__cause__
    if isinstance(cause, AdapterHttpError):
        return cause.status_code
    return None


def _is_retryable_telegram_rejection(status: int | None) -> bool:
    return status in {408, 409, 425, 429} or bool(status is not None and status >= 500)


async def ping_assaf_async(
    settings: Settings,
    port: MessagePort,
    session: SiteSession,
    *,
    claim: OwnerPingClaim | None = None,
    release: OwnerPingRelease | None = None,
    confirmed: OwnerPingConfirmed | None = None,
    max_rejection_retries: int = 1,
) -> bool:
    """Compatibility result: true only for observed or durably confirmed acceptance."""
    delivery = await ping_assaf_delivery_async(
        settings,
        port,
        session,
        claim=claim,
        release=release,
        confirmed=confirmed,
        max_rejection_retries=max_rejection_retries,
    )
    return bool(delivery.delivered)


async def ping_assaf_delivery_async(
    settings: Settings,
    port: MessagePort,
    session: SiteSession,
    *,
    claim: OwnerPingClaim | None = None,
    release: OwnerPingRelease | None = None,
    confirmed: OwnerPingConfirmed | None = None,
    max_rejection_retries: int = 1,
) -> OwnerTelegramDelivery:
    owners = settings.telegram_owner_user_id_set()
    if not owners:
        return OwnerTelegramDelivery(no_attempt=True)
    body = format_owner_ping(session)
    delivered: list[str] = []
    rejected: list[str] = []
    ambiguous: list[str] = []
    for owner_id in owners:
        if claim is not None and not claim(owner_id):
            # A retained claim can mean accepted, ambiguous, or still in flight. Only
            # the accepted receipt is evidence that Assaf actually got the message.
            if confirmed is not None and confirmed(owner_id):
                delivered.append(owner_id)
            else:
                ambiguous.append(owner_id)
            continue
        attempts = 0
        while True:
            try:
                await port.send(
                    OutboundMessage(
                        conversation_id=owner_id,
                        text=body,
                        channel="telegram",
                        idempotency_key=f"site-ping:{session.session_id}",
                    )
                )
                delivered.append(owner_id)
                break
            except (RuntimeError, MiaError, httpx.HTTPError) as exc:
                rejection_status = _telegram_rejection_status(exc)
                explicit_rejection = isinstance(exc, RuntimeError) or rejection_status is not None
                if not explicit_rejection:
                    # A timeout or malformed success response can happen after
                    # Telegram accepted the request. Retain the claim and never resend.
                    ambiguous.append(owner_id)
                    break
                if release is not None:
                    release(owner_id)
                if not _is_retryable_telegram_rejection(rejection_status) or attempts >= max(
                    0, max_rejection_retries
                ):
                    rejected.append(owner_id)
                    break
                attempts += 1
                if claim is not None and not claim(owner_id):
                    if confirmed is not None and confirmed(owner_id):
                        delivered.append(owner_id)
                    else:
                        ambiguous.append(owner_id)
                    break
    return OwnerTelegramDelivery(
        delivered=tuple(delivered),
        rejected=tuple(rejected),
        ambiguous=tuple(ambiguous),
    )


_UNKNOWN = "—"


def _missing_for_owner(session: SiteSession) -> list[str]:
    """What Mia did not get. Naming the hole beats implying the record is complete."""
    fields = session.fields
    missing: list[str] = []
    if not fields.name.strip():
        missing.append("שם")
    if not (session.business_summary.strip() or fields.business.strip()):
        missing.append("עסק")
    if not fields.has_phone_or_email():
        missing.append("טלפון או אימייל")
    if not (session.friction_summary.strip() or fields.want.strip() or fields.summary.strip()):
        missing.append("מה צריך")
    return missing


def _recommended_next_action(session: SiteSession) -> str:
    """Deterministic from state. Never a guess about how good the lead is."""
    fields = session.fields
    if session.complaint_open:
        return "תלונה פתוחה. תדבר איתם, בלי מכירה."
    if session.selling_stopped:
        return "אמרו שלא מעוניינים. אל תדחוף."
    if fields.phone.strip():
        return "תכתוב להם בוואטסאפ למספר שלמעלה."
    if fields.email.strip():
        return "תשלח מייל לכתובת שלמעלה."
    return "אין דרך ליצור קשר. אין למי לפנות."


def _last_visitor_line(session: SiteSession) -> str:
    for role, text in reversed(session.turns):
        if role == "visitor" and text.strip() and not _is_contact_only(text):
            return text.strip()[:200]
    return ""


def _open_questions_for_owner(session: SiteSession) -> str:
    open_topics: list[str] = []
    if not session.business_known:
        open_topics.append("מה העסק עושה")
    if not session.friction_known:
        open_topics.append("מה מפריע היום")
    return ", ".join(open_topics) or "לא ידוע על פרט חסר בשאלות ההיכרות"


def _discussion_for_owner(session: SiteSession) -> str:
    discussed = [
        f"{'לקוח' if role == 'visitor' else 'מיה'}: {text.strip()}"
        for role, text in session.turns
        if role in {"visitor", "mia"}
        and text.strip()
        and not (role == "visitor" and _is_contact_only(text))
    ]
    return " | ".join(discussed[-4:])[:500] or _UNKNOWN


def _visitor_questions_for_owner(session: SiteSession) -> str:
    question_starts = ("מה ", "איך ", "כמה ", "מתי ", "האם ", "אפשר ", "can ", "how ")
    questions = [
        text.strip()
        for role, text in session.turns
        if role == "visitor"
        and text.strip()
        and not _is_contact_only(text)
        and ("?" in text or text.strip().casefold().startswith(question_starts))
    ]
    return " | ".join(questions[-2:])[:300] or _UNKNOWN


def format_owner_ping(session: SiteSession) -> str:
    """A factual brief, not a clipped transcript.

    Every line is either a field Mia actually captured or a deterministic consequence
    of the session state. Nothing here estimates budget, intent or lead quality — if
    Mia did not learn something, the brief says so instead of filling the gap.
    """
    fields = session.fields
    business = session.business_summary.strip() or fields.business.strip()
    friction = session.friction_summary.strip() or fields.want.strip()
    missing = _missing_for_owner(session)
    lines = [
        "ליד חדש מהאתר — ליד חם",
        f"שם: {fields.name.strip() or _UNKNOWN}",
        f"טלפון: {fields.phone.strip() or _UNKNOWN}",
        f"אימייל: {fields.email.strip() or _UNKNOWN}",
        f"תאריך: {fields.date.strip() or _UNKNOWN}",
        f"עסק: {business or _UNKNOWN}",
        f"מה מפריע: {friction or _UNKNOWN}",
        f"על מה דיברו: {_discussion_for_owner(session)}",
        f"שאלות שהלקוח העלה: {_visitor_questions_for_owner(session)}",
        f"להשלמה עם אסף: {_open_questions_for_owner(session)}",
        f"חסר: {', '.join(missing) if missing else 'כלום'}",
        f"המלצה: {_recommended_next_action(session)}",
    ]
    last = _last_visitor_line(session)
    if last:
        lines.append(f"במילים שלהם: {last}")
    return "\n".join(lines)


# Re-export for leftover tests that import the hook line.
ASSAFWEB_HOOK = ASSAFWEB_HOOK_HE
