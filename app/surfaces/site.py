"""Site selling surface. Identify the visitor, then sell. No invented prices."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.errors import MiaError
from app.domain.handoff import click_to_chat_url
from app.domain.memory import ConversationTurn
from app.integrations.base import MessagePort, OutboundMessage
from app.integrations.sales_reply import SalesReplyPort
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
    is_frustrated,
    never_silent,
    pick_language,
)
from app.surfaces.site_reply import phrase_site_reply

# Durable at-most-once for the owner ping, injected by the API layer so this surface
# keeps no database dependency. claim(recipient_id) -> may I send to this owner;
# release(recipient_id) -> that send genuinely failed, let a later turn retry.
OwnerPingClaim = Callable[[str], bool]
OwnerPingRelease = Callable[[str], None]

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
    )
    thought = raw.strip()
    if not voice_failed:
        session.burst_parts, thought = append_burst(
            session.burst_parts, raw, now=clock
        )
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
    if tools_ran:
        session.tools_ran = tuple(dict.fromkeys((*session.tools_ran, *tools_ran)))
    named_tools = session.tools_ran if intent == "tool_status" else tools_ran
    # How far into the conversation we are, and whether they have told us it is going
    # badly. Both move the ladder off "ask another question".
    visitor_turns = sum(1 for role, _text in session.turns if role == "visitor")
    frustrated = not voice_failed and is_frustrated(thought or raw)
    if intent == "need":
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
        port=reply_port,
        visitor_turns=visitor_turns,
        frustrated=frustrated,
        usage=usage,
    )
    reply = never_silent(phrased, session.language)
    session.turns.append(("mia", reply))
    crm_wrote = False
    owner_pinged = False
    wa_url = None
    if (
        session.fields.has_phone_or_email()
        and (decision.write_sheet or decision.ping_assaf)
    ):
        record = _contact_from_session(session)
        if defer is not None:
            # Two Google Sheets round trips at 20s each. The visitor never waits on them.
            defer(
                lambda: log_contact(
                    crm,
                    record,
                    who="מיה",
                    channel="website",
                    action="שיחת אתר",
                    result="נרשם",
                )
            )
            written = record
        else:
            written = log_contact(
                crm,
                record,
                who="מיה",
                channel="website",
                action="שיחת אתר",
                result="נרשם",
            )
        crm_wrote = True
        wa_url = click_to_chat_url(settings.whatsapp_click_to_chat) or None
        if owner_port is not None and not session.pinged and decision.ping_assaf:
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
                log_contact(
                    crm,
                    ContactRecord(
                        name=written.name,
                        phone=written.phone,
                        email=written.email,
                        date=written.date,
                        business=written.business,
                        source=written.source,
                        language=written.language,
                        want=written.want,
                        status=written.status,
                        summary=written.summary,
                        next_step=written.next_step,
                        created=written.created,
                        pinged="כן",
                    ),
                    who="מיה",
                    channel="website",
                    action="פינג לאסף",
                    result="נשלח",
                )
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


def _is_contact_only(text: str) -> bool:
    stripped = text.strip()
    if "@" in stripped and len(stripped) < 80:
        return True
    digits = "".join(ch for ch in stripped if ch.isdigit())
    return len(digits) >= 9 and len(stripped) <= 24


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
    already_delivered = False
    for owner_id in owners:
        if claim is not None and not claim(owner_id):
            already_delivered = True
            continue
        try:
            port.send(  # type: ignore[unused-coroutine]
                OutboundMessage(
                    conversation_id=owner_id,
                    text=body,
                    channel="telegram",
                    idempotency_key=f"site-ping:{session.session_id}",
                )
            )
            sent_any = True
        except (RuntimeError, MiaError):
            if release is not None:
                release(owner_id)
            continue
    return sent_any or already_delivered


async def ping_assaf_async(
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
    already_delivered = False
    for owner_id in owners:
        if claim is not None and not claim(owner_id):
            # Someone already delivered this handoff to this owner. Not a failure,
            # and not a reason to send it twice.
            already_delivered = True
            continue
        try:
            await port.send(
                OutboundMessage(
                    conversation_id=owner_id,
                    text=body,
                    channel="telegram",
                    idempotency_key=f"site-ping:{session.session_id}",
                )
            )
            sent_any = True
        except (RuntimeError, MiaError):
            # A genuine transport failure gives the claim back, so a later turn can
            # still reach Assaf instead of the lead going quiet forever.
            if release is not None:
                release(owner_id)
            continue
    return sent_any or already_delivered


_UNKNOWN = "—"


def _missing_for_owner(session: SiteSession) -> list[str]:
    """What Mia did not get. Naming the hole beats implying the record is complete."""
    fields = session.fields
    missing: list[str] = []
    if not fields.name.strip():
        missing.append("שם")
    if not fields.business.strip():
        missing.append("עסק")
    if not fields.has_phone_or_email():
        missing.append("טלפון או אימייל")
    if not (fields.want.strip() or fields.summary.strip()):
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
        if role == "visitor" and text.strip():
            return text.strip()[:200]
    return ""


def format_owner_ping(session: SiteSession) -> str:
    """A factual brief, not a clipped transcript.

    Every line is either a field Mia actually captured or a deterministic consequence
    of the session state. Nothing here estimates budget, intent or lead quality — if
    Mia did not learn something, the brief says so instead of filling the gap.
    """
    fields = session.fields
    need = (fields.want.strip() or fields.summary.strip())
    missing = _missing_for_owner(session)
    lines = [
        "ליד חדש מהאתר",
        f"שם: {fields.name.strip() or _UNKNOWN}",
        f"עסק: {fields.business.strip() or _UNKNOWN}",
        f"טלפון: {fields.phone.strip() or _UNKNOWN}",
        f"אימייל: {fields.email.strip() or _UNKNOWN}",
        f"תאריך: {fields.date.strip() or _UNKNOWN}",
        f"מה צריך: {need or _UNKNOWN}",
        f"חסר: {', '.join(missing) if missing else 'כלום'}",
        f"המלצה: {_recommended_next_action(session)}",
    ]
    last = _last_visitor_line(session)
    if last:
        lines.append(f"במילים שלהם: {last}")
    return "\n".join(lines)


# Re-export for leftover tests that import the hook line.
ASSAFWEB_HOOK = ASSAFWEB_HOOK_HE
