"""Text handling: statement text is data. It is cleaned, bounded and keyed, never interpreted.

Merchant names arrive from a bank statement through an assistant. They are untrusted: they may
carry bidi controls, zero-width characters, markup, instruction-like sentences or an
accidentally pasted card number. Everything here treats them as opaque strings.
"""

from __future__ import annotations

import re
import unicodedata

from .errors import IssueCollector
from .vocabulary import MAX_KEY_CHARS

# Control, format (bidi marks, zero-width joiners), surrogate, private-use and unassigned.
_DROPPED = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_DIGIT_RUN = re.compile(r"[0-9]{8,}")
# Digit groups joined by any run of non-letter, non-digit characters (space, hyphen, dot, slash,
# comma, bullet, zero-width marks, underscore). Letters break a number; punctuation does not.
_GROUPED_DIGITS = re.compile(r"[0-9]+(?:[\W_]+[0-9]+)+")
_GROUP_SEPARATOR = re.compile(r"[\W_]+")
_MAX_PASSES = 4


def clean_text(value: str) -> str:
    """Whitespace collapses to single spaces; control, bidi and zero-width characters vanish."""
    kept: list[str] = []
    for char in value:
        if char.isspace():
            kept.append(" ")
        elif unicodedata.category(char) not in _DROPPED:
            kept.append(char)
    return " ".join("".join(kept).split())


def _fold_once(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = unicodedata.normalize("NFKC", text)
    kept: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if char.isspace():
            kept.append(" ")
        elif category.startswith("M") or category in _DROPPED:
            continue  # combining marks (Hebrew points, accents) and invisible characters
        elif char.isalnum():
            kept.append(char)
        else:
            kept.append(" ")  # punctuation and symbols separate words
    return " ".join("".join(kept).split())


def normalize_merchant(text: str) -> str:
    """A stable, comparable key: case, width, marks and punctuation do not matter. Idempotent."""
    current = text
    for _ in range(_MAX_PASSES):
        folded = _fold_once(current)
        if folded == current:
            break
        current = folded
    return current


def identity_text(text: str) -> str:
    """Cleaned, width- and case-folded text for identity (account labels keep punctuation)."""
    return unicodedata.normalize("NFKC", clean_text(text)).casefold()


def luhn_valid(digits: str) -> bool:
    if not digits or not digits.isascii() or not digits.isdigit():
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = ord(char) - 48
        if index % 2 == 1:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _ascii_digits(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(
        str(unicodedata.decimal(char)) if unicodedata.category(char) == "Nd" else char
        for char in text
    )


def contains_card_like_digits(text: str) -> bool:
    """True for any 8+ digit run, or a Luhn-valid 13-19 digit number written in groups, whatever
    punctuation separates the groups.

    Statements print masked numbers (the last four digits), so no legitimate field here ever
    needs a long digit run. A false positive rejects the whole batch, which the assistant
    resubmits with that field fixed; a false negative would store a card number.
    """
    folded = _ascii_digits(text)
    if _DIGIT_RUN.search(folded):
        return True
    for match in _GROUPED_DIGITS.finditer(folded):
        groups = _GROUP_SEPARATOR.split(match.group())
        for start in range(len(groups)):
            total = 0
            for end in range(start, len(groups)):
                total += len(groups[end])
                if total > 19:
                    break
                if total >= 13 and luhn_valid("".join(groups[start : end + 1])):
                    return True
    return False


def check_text(
    value: object,
    *,
    path: str,
    max_chars: int,
    issues: IssueCollector,
    allow_empty: bool = False,
) -> str | None:
    """Clean and bound one text field. Adds reason codes and returns None when it is unusable."""
    if not isinstance(value, str):
        issues.add(path, "invalid_text")
        return None
    cleaned = clean_text(value)
    if not cleaned:
        if allow_empty:
            return ""
        issues.add(path, "empty")
        return None
    if len(cleaned) > max_chars:
        issues.add(path, "too_long")
        return None
    if contains_card_like_digits(cleaned):
        issues.add(path, "card_like_digits")
        return None
    return cleaned


def key_is_bounded(key: str) -> bool:
    return 0 < len(key) <= MAX_KEY_CHARS
