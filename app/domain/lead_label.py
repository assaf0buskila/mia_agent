"""A short human label for a lead, so Assaf can recognise it at a glance.

The owner console listed leads as `lead_82f527e3be5e · workflow · שלב ידני · עלות מאומתת`.
Every field is true and none of it answers "who is this?". Assaf asked
"מי זה הבחור של השעונים?" about a lead whose own conversation says he sells watches, and
the console could not connect the two.

This derives a label from what the prospect actually said — their words, not an inferred
summary — and is shown **to Assaf only**. That is the important distinction from
`website_handoff_brief._recommended_first_line`, which is copied into a message sent to the
*customer* and therefore stays on a strict topic allowlist. Assaf already sees the full
transcript in the briefing, so echoing a fragment back to him exposes nothing new.

Sanitising is still real: no digits runs, no URLs, no emails, no currency, no newlines, hard
length cap. A label is a glance, not a quote.
"""

from __future__ import annotations

import re

from app.domain.memory import ConversationTurn, counterpart_turns

MAX_LABEL_CHARS = 42
MIN_LABEL_CHARS = 3
_MAX_TURNS_SCANNED = 4

# Anything that could carry an identifier, a price, or a link is dropped whole.
# Every digit run goes, not just long ones: "הנחה של 20%" losing only the "%" leaves a
# bare "20" that reads like a price in a list. A label says what the business is; numbers
# never carry that and always risk being misread.
_UNSAFE = re.compile(
    r"(https?://\S+|www\.\S+|[\w.+-]+@[\w-]+\.\w+|\d+|[₪$€%])"
)
_PUNCT = re.compile(r"[^\w\s֐-׿'\"-]")
_WS = re.compile(r"\s+")

# Openers that carry no information about the business.
_FILLER_PREFIXES = (
    "היי", "שלום", "הי", "אהלן", "בוקר טוב", "ערב טוב",
    "hi", "hey", "hello", "good morning",
)
# A label made only of these is not a label.
_STOPWORDS = frozenset(
    {
        "אני", "יש", "לי", "את", "של", "עם", "על", "זה", "הוא", "היא", "אנחנו",
        "רוצה", "צריך", "צריכה", "שלי", "מה", "איך", "כן", "לא", "טוב",
        "i", "have", "a", "an", "the", "we", "my", "is", "are", "want", "need",
        "ok", "yes", "no", "and", "to", "of",
    }
)


def _strip_filler(text: str) -> str:
    cleaned = text.strip()
    lowered = cleaned.lower()
    for prefix in _FILLER_PREFIXES:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix) :].lstrip(" ,.!־-")
            break
    return cleaned


def sanitize_label(text: str) -> str:
    """Reduce a prospect line to a safe, glanceable fragment. Empty if nothing survives."""
    if not text:
        return ""
    cleaned = _strip_filler(text)
    if _UNSAFE.search(cleaned):
        # Drop only the offending runs, keep the rest of the sentence.
        cleaned = _UNSAFE.sub(" ", cleaned)
    cleaned = _PUNCT.sub(" ", cleaned)
    cleaned = _WS.sub(" ", cleaned).strip()
    if not cleaned:
        return ""
    words = cleaned.split()
    if not any(word.lower() not in _STOPWORDS for word in words):
        return ""
    label = ""
    for word in words:
        candidate = f"{label} {word}".strip()
        if len(candidate) > MAX_LABEL_CHARS:
            break
        label = candidate
    label = label.strip(" -־,")
    return label if len(label) >= MIN_LABEL_CHARS else ""


def derive_headline(turns: list[ConversationTurn]) -> str:
    """Best short label from the prospect's own words.

    Prefers the earliest substantive line, because that is where people say what they do
    ("אני מוכר שעונים") before the conversation narrows to a symptom.
    """
    for turn in counterpart_turns(turns)[:_MAX_TURNS_SCANNED]:
        label = sanitize_label(turn.text)
        if label:
            return label
    return ""


def short_lead_id(lead_id: str, *, keep: int = 8) -> str:
    """`lead_82f527e3be5e` -> `lead_82f5`. Enough to say aloud, still unique in a list."""
    if not lead_id.startswith("lead_"):
        return lead_id[: keep + 5]
    return lead_id[: keep + 1]


def lead_display(lead_id: str, headline: str) -> str:
    """`lead_82f527e3be5e · מוכר שעונים`, or just the id when nothing was learned yet.

    The id stays FULL. Assaf references it back to Mia ("תספר לי על lead_..."), so a
    truncated id would be prettier and useless.
    """
    cleaned = headline.strip()
    return f"{lead_id} · {cleaned}" if cleaned else lead_id
