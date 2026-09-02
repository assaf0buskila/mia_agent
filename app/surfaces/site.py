"""Site selling surface. Identify the visitor, then sell. No invented prices."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config import Settings
from app.domain.handoff import click_to_chat_url
from app.integrations.base import MessagePort, OutboundMessage
from app.surfaces.crm import ContactRecord, ContactsCrm, log_contact
from app.surfaces.identity import CapturedFields, apply_form

SITE_OPENING = (
    "שלום, אני מיה. ספרו לי בקצרה מה אתם מחפשים, "
    "ואיך אפשר לחזור אליכם בטלפון או באימייל."
)
ASK_CONTACT = "כדי שאסף יוכל להמשיך אתכם, צריך טלפון או אימייל."
ASK_NEED = "מה הכי חשוב לכם שנפתור קודם?"
AFTER_CAPTURE = (
    "תודה. העברתי לאסף את מה שסיפרתם, והוא ימשיך איתכם בוואטסאפ. "
    "אני לא ממציאה מחיר או התחייבות מכאן."
)
NO_PRICE = "אין לי מחיר לתת כאן. אסף ידבר איתכם על זה."


@dataclass
class SiteSession:
    session_id: str
    fields: CapturedFields = field(default_factory=CapturedFields)
    turns: list[tuple[str, str]] = field(default_factory=list)
    pinged: bool = False
    finalized: bool = False


@dataclass
class SiteTurn:
    reply: str
    next_action: str
    whatsapp_url: str | None
    crm_wrote: bool
    owner_pinged: bool
    fields: CapturedFields


class SiteBook:
    def __init__(self) -> None:
        self._sessions: dict[str, SiteSession] = {}

    def open(self, session_id: str) -> SiteSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        session = SiteSession(session_id=session_id)
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> SiteSession | None:
        return self._sessions.get(session_id)

    def exists(self, session_id: str) -> bool:
        return session_id in self._sessions


_BOOK = SiteBook()


def site_book() -> SiteBook:
    return _BOOK


def reset_site_book() -> None:
    _BOOK._sessions.clear()


def site_opening() -> str:
    return SITE_OPENING


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
) -> SiteTurn:
    """Identify, capture, then ping Assaf. No CRM or WhatsApp without phone or email."""
    store = book or _BOOK
    session = store.get(session_id)
    if session is None:
        raise KeyError(session_id)
    session.fields = apply_form(
        session.fields,
        name=name,
        phone=phone,
        email=email,
        date=date,
        text=text,
    )
    if not session.fields.want and text.strip() and not _is_contact_only(text):
        session.fields = CapturedFields(
            name=session.fields.name,
            phone=session.fields.phone,
            email=session.fields.email,
            date=session.fields.date,
            business=session.fields.business,
            want=text.strip()[:200],
            language=session.fields.language,
            summary=session.fields.summary,
        )
    session.turns.append(("visitor", text.strip()))
    reply, action = _site_reply(session.fields, text)
    session.turns.append(("mia", reply))
    crm_wrote = False
    owner_pinged = False
    wa_url = None
    if session.fields.has_phone_or_email():
        record = _contact_from_session(session)
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
        if owner_port is not None and not session.pinged:
            owner_pinged = _ping_assaf(settings, owner_port, session)
            if owner_pinged:
                session.pinged = True
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
    )


def _contact_from_session(session: SiteSession) -> ContactRecord:
    fields = session.fields
    return ContactRecord(
        name=fields.name,
        phone=fields.phone,
        email=fields.email,
        date=fields.date,
        source="website",
        language=fields.language,
        want=fields.want,
        status="פתוח",
        summary=_conversation_summary(session),
        next_step="אסף בוואטסאפ",
    )


def _conversation_summary(session: SiteSession) -> str:
    lines = [f"{role}: {text}" for role, text in session.turns[-8:] if text]
    return " | ".join(lines)[:400]


def _site_reply(fields: CapturedFields, text: str) -> tuple[str, str]:
    if _asks_price(text):
        return NO_PRICE, "no_price"
    if not fields.has_phone_or_email():
        if fields.want:
            return ASK_CONTACT, "ask_contact"
        return ASK_NEED, "ask_need"
    return AFTER_CAPTURE, "handoff"


def _asks_price(text: str) -> bool:
    lowered = text.lower()
    return any(word in text or word in lowered for word in ("מחיר", "כמה עולה", "price", "cost"))


def _is_contact_only(text: str) -> bool:
    stripped = text.strip()
    if "@" in stripped and len(stripped) < 80:
        return True
    digits = "".join(ch for ch in stripped if ch.isdigit())
    return len(digits) >= 9 and len(stripped) <= 24


def _ping_assaf(settings: Settings, port: MessagePort, session: SiteSession) -> bool:
    owners = settings.telegram_owner_user_id_set()
    if not owners:
        return False
    body = format_owner_ping(session)
    sent_any = False
    for owner_id in owners:
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
        except RuntimeError:
            continue
    return sent_any


async def ping_assaf_async(
    settings: Settings, port: MessagePort, session: SiteSession
) -> bool:
    owners = settings.telegram_owner_user_id_set()
    if not owners:
        return False
    body = format_owner_ping(session)
    sent_any = False
    for owner_id in owners:
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
        except RuntimeError:
            continue
    return sent_any


def format_owner_ping(session: SiteSession) -> str:
    fields = session.fields
    lines = [
        "שיחה מהאתר",
        f"שם: {fields.name or '—'}",
        f"טלפון: {fields.phone or '—'}",
        f"אימייל: {fields.email or '—'}",
        f"תאריך: {fields.date or '—'}",
        f"מה רוצים: {fields.want or '—'}",
        f"סיכום: {_conversation_summary(session) or '—'}",
    ]
    return "\n".join(lines)
