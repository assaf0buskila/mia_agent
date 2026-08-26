"""Owner-facing facts about a finished website conversation, read from what we already have.

The website-final Telegram card used to carry four fields, two of which were the ids.
Everything the owner actually wants — who, what business, what hurts, when, whether money
came up — was already in the database: the sales state the ladder maintains, the canonical
message turns, and the meeting row. This module reads those and nothing else.

Three rules, from the spec:

* Never fabricate. Every function returns "" when the fact was not established, and an
  empty value is dropped by `render_conversation_summary`. A missing line is correct
  output, not a gap to fill.
* No LLM. Everything here is a flag lookup, a needle match against an allowlist, or a
  sanitised fragment of the prospect's own words. Deterministic, free, and testable.
* Budget only when it was actually discussed.

Prospect text is untrusted. Anything quoted back goes through `sanitize_label`, which
strips urls, emails, digit runs and currency and caps the length, so a turn can never
become an instruction or leak an identifier. Contact details and money amounts are the two
exceptions, and they are not free text: they are emitted only as a whole regex match of a
well-formed address, phone number, or amount.
"""

from __future__ import annotations

import re

from app.domain.lead_label import derive_display_name, derive_headline, sanitize_label
from app.domain.memory import ConversationTurn, counterpart_turns
from app.domain.sales import FitLevel, ObjectionKind, SalesState

MAX_FIELD_CHARS = 80
_MAX_TURNS_SCANNED = 12
_MAX_SERVICES = 3

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.IGNORECASE)
_PHONE = re.compile(r"(?<![\d])(\+?\d[\d\- ]{7,15}\d)(?![\d])")

# What they want, in their words. The needle is dropped and the rest is sanitised.
_WANT_NEEDLES: tuple[str, ...] = (
    "אני רוצה",
    "אנחנו רוצים",
    "רוצה",
    "אני צריך",
    "אני צריכה",
    "צריך",
    "צריכה",
    "מחפש",
    "מחפשת",
    "i want",
    "we want",
    "i need",
    "we need",
    "looking for",
    "i'd like",
)

# Allowlisted service labels. A needle only ever selects a label from this table, so no
# prospect text is interpolated into the service line.
_SERVICE_NEEDLES: tuple[tuple[str, str], ...] = (
    ("מלאי", "inventory automation"),
    ("inventory", "inventory automation"),
    ("שיטס", "spreadsheet automation"),
    ("גוגל שיט", "spreadsheet automation"),
    ("sheets", "spreadsheet automation"),
    ("spreadsheet", "spreadsheet automation"),
    ("אקסל", "spreadsheet automation"),
    ("excel", "spreadsheet automation"),
    ("חשבונית", "invoicing automation"),
    ("חשבוניות", "invoicing automation"),
    ("invoice", "invoicing automation"),
    ("וואטסאפ", "WhatsApp automation"),
    ("whatsapp", "WhatsApp automation"),
    ("לידים", "lead handling"),
    ("leads", "lead handling"),
    ("אתר", "website build"),
    ("website", "website build"),
    ("צאט", "chat assistant"),
    ("צ'אט", "chat assistant"),
    ("chatbot", "chat assistant"),
    ("בוט", "chat assistant"),
)

_TIMELINE_NEEDLES: tuple[tuple[str, str], ...] = (
    ("מיד", "immediately"),
    ("דחוף", "urgent"),
    ("asap", "ASAP"),
    ("מחר", "tomorrow"),
    ("השבוע", "this week"),
    ("this week", "this week"),
    ("שבוע הבא", "next week"),
    ("next week", "next week"),
    ("שבועיים", "within two weeks"),
    ("החודש", "this month"),
    ("this month", "this month"),
    ("תוך חודש", "within a month"),
    ("within a month", "within a month"),
    ("חודש הבא", "next month"),
    ("next month", "next month"),
    ("רבעון", "this quarter"),
    ("quarter", "this quarter"),
    ("שנה הבאה", "next year"),
    ("next year", "next year"),
)

_CURRENCY_LABELS: tuple[tuple[str, str], ...] = (
    ("₪", "ILS"),
    ('ש"ח', "ILS"),
    ("שח", "ILS"),
    ("שקל", "ILS"),
    ("$", "USD"),
    ("דולר", "USD"),
    ("usd", "USD"),
    ("€", "EUR"),
    ("eur", "EUR"),
)
_BUDGET_WORDS: tuple[str, ...] = (
    "תקציב",
    "budget",
    "מחיר",
    "כמה זה",
    "כמה עולה",
    "עולה",
    "לשלם",
    "price",
    "cost",
    "how much",
)
_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k|אלף)?", re.IGNORECASE)

_BUDGET_DISCUSSED = "discussed, no figure given"
_BUDGET_ASKED = "asked about price, no figure given"
_TIMELINE_STATED = "stated in conversation"


def _clip(value: str) -> str:
    return value.strip()[:MAX_FIELD_CHARS].strip()


def _prospect_texts(turns: list[ConversationTurn]) -> list[str]:
    return [turn.text for turn in counterpart_turns(turns)[:_MAX_TURNS_SCANNED]]


def _prospect_blob(turns: list[ConversationTurn]) -> str:
    return " ".join(_prospect_texts(turns)).lower()


def extract_contact(turns: list[ConversationTurn]) -> str:
    """An email or phone number the visitor typed. Whole match only, never free text."""
    for text in _prospect_texts(turns):
        email = _EMAIL.search(text)
        if email is not None:
            return _clip(email.group(0))
    for text in _prospect_texts(turns):
        phone = _PHONE.search(text)
        if phone is None:
            continue
        candidate = phone.group(1).strip()
        digits = [char for char in candidate if char.isdigit()]
        if 9 <= len(digits) <= 15:
            return _clip(candidate)
    return ""


def extract_need(turns: list[ConversationTurn]) -> str:
    """What they said they want, in their own sanitised words."""
    for text in _prospect_texts(turns):
        folded = text.lower()
        for needle in _WANT_NEEDLES:
            position = folded.find(needle)
            if position < 0:
                continue
            label = sanitize_label(text[position + len(needle) :])
            if label:
                return _clip(label)
    return ""


def describe_pain(sales: SalesState) -> str:
    """The problem as the ladder recorded it. Flags only — no paraphrase of the visitor."""
    parts: list[str] = []
    if int(sales.pain_level) > 0:
        parts.append(f"P{int(sales.pain_level)}")
    if sales.manual_step_known:
        parts.append("manual step identified")
    if sales.impact_confirmed:
        parts.append("time or money cost confirmed")
    if sales.metric_known:
        parts.append("business consequence known")
    return _clip(" · ".join(parts))


def relevant_service(turns: list[ConversationTurn]) -> str:
    """Allowlisted service labels for the topics the visitor actually raised."""
    blob = _prospect_blob(turns)
    found: list[str] = []
    for needle, label in _SERVICE_NEEDLES:
        if needle in blob and label not in found:
            found.append(label)
    if not found:
        return ""
    return _clip(" · ".join(found[:_MAX_SERVICES]))


def extract_timeline(turns: list[ConversationTurn], sales: SalesState) -> str:
    """A timeline phrase they used, else the ladder's own timeline_known flag."""
    blob = _prospect_blob(turns)
    for needle, label in _TIMELINE_NEEDLES:
        if needle in blob:
            return label
    if sales.timeline_known:
        return _TIMELINE_STATED
    return ""


def _amount_from(text: str) -> str:
    folded = text.lower()
    for token, label in _CURRENCY_LABELS:
        position = folded.find(token.lower())
        if position < 0:
            continue
        window = text[max(0, position - 12) : position + len(token) + 12]
        match = _AMOUNT.search(window)
        if match is None:
            continue
        amount = match.group(1)
        thousands = "k" if match.group(2) else ""
        return f"{amount}{thousands} {label}"
    return ""


def extract_budget(turns: list[ConversationTurn], sales: SalesState) -> str:
    """Budget only when money actually came up. A figure when they gave one, else how it came up."""
    discussed = False
    for text in _prospect_texts(turns):
        amount = _amount_from(text)
        if amount:
            return _clip(amount)
        folded = text.lower()
        if any(word in folded for word in _BUDGET_WORDS):
            discussed = True
    if discussed:
        return _BUDGET_DISCUSSED
    if sales.active_objection == ObjectionKind.PRICE:
        return _BUDGET_DISCUSSED
    if sales.active_objection == ObjectionKind.PRICE_QUESTION:
        return _BUDGET_ASKED
    return ""


def describe_meeting(meeting: object | None, sales: SalesState) -> str:
    """Where the meeting stands, from the meetings row first and the sales flags second."""
    status = str(getattr(meeting, "status", "") or "").strip()
    if status:
        scheduled = str(getattr(meeting, "scheduled_at", "") or "").strip()
        if scheduled:
            return _clip(f"{status} · {scheduled}")
        return _clip(status)
    if sales.meeting_exit_offered:
        return "offered, not booked"
    if sales.willingness_to_meet is True:
        return "willing to meet"
    if sales.willingness_to_meet is False:
        return "declined a meeting"
    return ""


def describe_qualification(sales: SalesState) -> str:
    """Fit, unless it is literally unknown — "unknown" is not a fact worth a line."""
    if sales.fit == FitLevel.UNKNOWN:
        return ""
    return sales.fit.value


def describe_name(turns: list[ConversationTurn], sales: SalesState) -> str:
    stated = (sales.display_name or "").strip()
    return _clip(stated or derive_display_name(turns))


def describe_business(turns: list[ConversationTurn], sales: SalesState) -> str:
    headline = (sales.headline or "").strip()
    business = headline or derive_headline(turns)
    if business:
        return _clip(business)
    return _clip(sales.company_domain or "")
