"""Deterministic prospect tone cues for sales replies. Not an LLM."""

from __future__ import annotations

import re

from app.domain.extract import is_substantive_answer
from app.domain.memory import ConversationTurn

# Labels are passed to the reply port as data — short English tokens the model maps to tone.
_CUE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "frustrated",
        (
            "frustrated",
            "frustrating",
            "annoying",
            "annoyed",
            "sick of",
            "fed up",
            "ridiculous",
            "unacceptable",
            "מתסכל",
            "מתסכלת",
            "מעצבן",
            "מעצבנת",
            "נמאס",
            "נמאס לי",
            "לא נורמלי",
            "זה לא מקובל",
        ),
    ),
    (
        "overwhelmed",
        (
            "overwhelm",
            "overwhelmed",
            "too much",
            "drowning",
            "can't keep up",
            "cant keep up",
            "falling behind",
            "מוצף",
            "יותר מדי",
            "לא מספיק ידיים",
            "לא עומדים",
            "בלגן",
            "כאוס",
            "מבולגן",
        ),
    ),
    (
        "stressed",
        (
            "stressed",
            "stress",
            "pressure",
            "under pressure",
            "panic",
            "לחוץ",
            "לחוצה",
            "בלחץ",
            "מלחיץ",
            "דחוף מאוד",
        ),
    ),
    (
        "skeptical",
        (
            "not sure",
            "don't trust",
            "dont trust",
            "skeptical",
            "doubt",
            "hard to believe",
            "sounds too good",
            "לא בטוח",
            "לא בטוחה",
            "קשה להאמין",
            "נשמע מוגזם",
            "באמת?",
        ),
    ),
    (
        "excited",
        (
            "excited",
            "can't wait",
            "cant wait",
            "love this",
            "sounds great",
            "amazing",
            "מתרגש",
            "מתרגשת",
            "נשמע מעולה",
            "מדהים",
            "וואו",
        ),
    ),
    (
        "tired",
        (
            "exhausted",
            "burned out",
            "burnt out",
            "tired of",
            "so tired",
            "עייף",
            "עייפה",
            "נשרף",
            "נשרפת",
            "שחיקה",
        ),
    ),
    (
        "worried",
        (
            "worried",
            "afraid",
            "concerned",
            "anxious",
            "nervous",
            "דואג",
            "דואגת",
            "חושש",
            "חוששת",
            "מפחד",
            "מפחדת",
            "חרד",
        ),
    ),
    (
        "uncertain",
        (
            "confused",
            "not clear",
            "unclear",
            "maybe",
            "לא יודע",
            "לא יודעת",
            "מבולבל",
            "מבולבלת",
            "לא ברור",
        ),
    ),
)

_HEBREW_LETTER = "\u0590-\u05FF"


def _token_matches(text: str, token: str) -> bool:
    if any("\u0590" <= char <= "\u05FF" for char in token):
        pattern = rf"(?<![{_HEBREW_LETTER}]){re.escape(token)}(?![{_HEBREW_LETTER}])"
    else:
        pattern = rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])"
    return re.search(pattern, text) is not None


def detect_emotional_cues(message: str) -> tuple[str, ...]:
    """Return stable tone labels detected in one message. Conservative: no guesswork."""
    text = message.lower().strip()
    if not text:
        return ()
    found: list[str] = []
    for label, tokens in _CUE_PATTERNS:
        if any(_token_matches(text, token) for token in tokens):
            found.append(label)
    return tuple(found)


def _last_prospect_text(turns: tuple[ConversationTurn, ...]) -> str:
    for turn in reversed(turns):
        if turn.role == "prospect" and turn.text.strip():
            return turn.text
    return ""


def infer_emotional_cues(
    message: str,
    *,
    recent_turns: tuple[ConversationTurn, ...] = (),
) -> tuple[str, ...]:
    """Tone for this reply turn. Short answers inherit the prior prospect message."""
    cues = detect_emotional_cues(message)
    if cues:
        return cues
    if is_substantive_answer(message):
        return ()
    prior = _last_prospect_text(recent_turns)
    if prior and prior.strip().lower() != message.strip().lower():
        return detect_emotional_cues(prior)
    return ()
