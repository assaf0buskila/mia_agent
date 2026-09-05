"""Resolve owner follow-up references against the previous Telegram turns.

Assaf does not repeat a lead id every message. He asks "what's most interesting?"
and then says "check that with him". Without memory both land on the generic
fallback, which is half of why unrelated instructions came back identical.

This resolver is deterministic on purpose. It only rewrites *which subject* an
instruction is about by reading Mia's own previous reply. It never chooses a tool,
never grants permission and never sends anything, so a resolved reference still
goes through the same typed owner task path as an explicit one.
"""

from __future__ import annotations

from app.domain.approvals import LEAD_ID_RE
from app.domain.memory import ROLE_MIA, ConversationTurn

_DRILL_DOWN_PHRASES: tuple[str, ...] = (
    "מה הכי מעניין",
    "מה הכי חם",
    "ספרי לי עוד",
    "תספרי לי עוד",
    "תפרטי",
    "מה עוד",
    "tell me more",
    "what's most interesting",
    "whats most interesting",
    "which one matters",
)

_PRONOUN_REFERENCE_PHRASES: tuple[str, ...] = (
    "איתו",
    "אותו",
    "אחריו",
    "שלו",
    "איתה",
    "אותה",
    "אחריה",
    "שלה",
    "with him",
    "with her",
    "ask him",
    "ask her",
    "follow up with them",
)

_MAX_TURNS_BACK = 6

# An approval must name what it approves, and marking a contact personal changes
# how Mia treats a real person. Neither may be inferred from a pronoun.
_NEVER_RESOLVED_PHRASES: tuple[str, ...] = (
    "אשר",
    "דחה",
    "דחי",
    "approve",
    "reject",
    "אל תאוטומטי",
    "סמן אישי",
    "זה אישי",
    "never automate",
    "do not automate",
    "personal",
)


def _matches(text: str, phrases: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(phrase.lower() in lowered for phrase in phrases)


def needs_data_anchor(text: str) -> bool:
    """True when a data anchor could help, so callers can skip the query otherwise.

    Only a drill-down qualifies, and only when the message names no lead itself.
    Lets the inbound path avoid a lookup on every owner message.
    """
    if LEAD_ID_RE.search(text) is not None:
        return False
    if _matches(text, _NEVER_RESOLVED_PHRASES):
        return False
    return _matches(text, _DRILL_DOWN_PHRASES)


def last_lead_id_mentioned(history: list[ConversationTurn]) -> str | None:
    """The lead Mia last named. Owner text is ignored so a typo cannot invent an id."""
    for turn in reversed(history[-_MAX_TURNS_BACK:]):
        if turn.role != ROLE_MIA:
            continue
        match = LEAD_ID_RE.search(turn.text)
        if match is not None:
            return match.group(0)
    return None


def resolve_owner_reference(
    text: str,
    *,
    history: list[ConversationTurn],
    fallback_lead_id: str | None = None,
) -> str | None:
    """Return the lead id an unqualified follow-up is about, or None.

    Returns None when the message already carries an id, when it is not a
    follow-up shape, or when nothing can anchor it. Guessing is worse than asking,
    so an unresolved reference falls through to the normal Understanding Check.

    `fallback_lead_id` covers the drill-down after a counts-only answer, where the
    subject exists in the data but was never named in the transcript. A pronoun
    ("with him") never uses it: the previous reply is the only thing that can say
    who "him" is.
    """
    if LEAD_ID_RE.search(text) is not None:
        return None
    if _matches(text, _NEVER_RESOLVED_PHRASES):
        return None
    is_drill_down = _matches(text, _DRILL_DOWN_PHRASES)
    if not is_drill_down and not _matches(text, _PRONOUN_REFERENCE_PHRASES):
        return None
    mentioned = last_lead_id_mentioned(history)
    if mentioned is not None:
        return mentioned
    return fallback_lead_id if is_drill_down else None


def routed_owner_text(
    text: str,
    *,
    history: list[ConversationTurn],
    fallback_lead_id: str | None = None,
) -> str:
    """Owner text for routing, with a resolved reference made explicit.

    The stored transcript keeps what Assaf actually wrote. Only routing sees the
    expanded form, so the audit trail never claims he said an id he did not say.
    """
    resolved = resolve_owner_reference(
        text, history=history, fallback_lead_id=fallback_lead_id
    )
    if resolved is None:
        return text
    if _matches(text, _DRILL_DOWN_PHRASES):
        # "What's most interesting?" after a list means: open that one.
        return f"תספרי לי על הליד {resolved}"
    return f"{text} {resolved}"
