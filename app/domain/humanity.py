"""Deterministic Human Voice linter for customer-facing sales replies."""

from __future__ import annotations

import re

from pydantic import BaseModel

_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})

_AI_PHRASE_MULTI: tuple[str, ...] = (
    "absolutely!",
    "let's dive in",
    "it's important to note",
    "game-changing",
    "בהחלט!",
    "בואו נצלול",
    "חשוב לציין",
)
_AI_PHRASE_WORD: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bseamless\b", re.IGNORECASE),
    re.compile(r"\bleverage\b", re.IGNORECASE),
)
_UNSUPPORTED_MULTI: tuple[str, ...] = (
    "limited time",
    "only today",
    "last chance",
    "act now",
    "רק היום",
    "הזדמנות אחרונה",
    "הגדלת מכירות ב",
)
_UNSUPPORTED_WORD: tuple[re.Pattern[str], ...] = (
    re.compile(r"\broi\b", re.IGNORECASE),
)
_LETTER = r"[A-Za-z\u0590-\u05FF]"
# Backslash or chupchik (') between single letters: ש'ל'ו'ם / a\b\c
_SPACED_OUT_LETTERS = re.compile(
    rf"(?:{_LETTER}(?:\\|--|['\u05f3\u2019]|\u05be|-)){{2,}}{_LETTER}"
)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_HYPHEN_MARKS = ("-", "\u2013", "\u2014", "\u05be", "--")


class HumanityVerdict(BaseModel):
    ok: bool
    reasons: tuple[str, ...] = ()


def _fold(text: str) -> str:
    return text.translate(_APOSTROPHES).lower()


def _prose(text: str) -> str:
    """Customer prose only. URLs may legally contain hyphens; the rest may not."""
    return _URL.sub(" ", text)


def _has_bad_typography(text: str) -> bool:
    prose = _prose(text)
    if "\\" in prose or " / " in prose:
        return True
    if any(mark in prose for mark in _HYPHEN_MARKS):
        return True
    return _SPACED_OUT_LETTERS.search(prose) is not None


def lint_customer_reply(text: str) -> HumanityVerdict:
    if not text.strip():
        return HumanityVerdict(ok=False, reasons=())
    reasons: list[str] = []
    folded = _fold(text)
    if any(phrase in folded for phrase in _AI_PHRASE_MULTI):
        reasons.append("ai_phrase")
    if any(pattern.search(text) for pattern in _AI_PHRASE_WORD):
        reasons.append("ai_phrase")
    if _has_bad_typography(text):
        reasons.append("typography")
    if text.count("?") + text.count("\u061f") > 1:
        reasons.append("question_count")
    if any(phrase in folded for phrase in _UNSUPPORTED_MULTI):
        reasons.append("unsupported_claim")
    if any(pattern.search(text) for pattern in _UNSUPPORTED_WORD):
        reasons.append("unsupported_claim")
    if reasons:
        return HumanityVerdict(ok=False, reasons=tuple(dict.fromkeys(reasons)))
    return HumanityVerdict(ok=True)


def customer_reply_or_canned(*, candidate: str, canned: str) -> str:
    if lint_customer_reply(candidate).ok:
        return candidate
    return canned
