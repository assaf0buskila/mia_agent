"""Deterministic reply-language selection.

Mia is bilingual by design. Hebrew is the website default because the audience is
Israeli small businesses, but an English-speaking prospect must be answered in natural
English rather than translated Hebrew. Script counting is enough here and costs nothing;
this is not a job for a model call.
"""

from __future__ import annotations

from app.domain.memory import ROLE_MIA, ConversationTurn

LANG_HE = "he"
LANG_EN = "en"
LANG_UNKNOWN = "und"

_MIN_LATIN_LETTERS = 4


def _script_counts(text: str) -> tuple[int, int]:
    hebrew = 0
    latin = 0
    for char in text:
        if "\u0590" <= char <= "\u05FF":
            hebrew += 1
        elif char.isascii() and char.isalpha():
            latin += 1
    return hebrew, latin


def language_of(text: str) -> str:
    """Language of one message. Mixed Hebrew and English resolves to Hebrew."""
    hebrew, latin = _script_counts(text)
    if hebrew:
        return LANG_HE
    if latin >= _MIN_LATIN_LETTERS:
        return LANG_EN
    return LANG_UNKNOWN


def reply_language(
    *,
    latest_message: str,
    turns: list[ConversationTurn] | None = None,
    default: str = LANG_HE,
) -> str:
    """Answer in the prospect's language; fall back to earlier prospect turns."""
    resolved = language_of(latest_message)
    if resolved != LANG_UNKNOWN:
        return resolved
    for turn in reversed(turns or []):
        if turn.role == ROLE_MIA:
            continue
        resolved = language_of(turn.text)
        if resolved != LANG_UNKNOWN:
            return resolved
    return default
