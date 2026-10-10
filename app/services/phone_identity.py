"""Opt-in comparison keys for new phone input; never rewrites stored contacts."""

from __future__ import annotations

import re

_FORMATTED_PHONE = re.compile(r"\+?[0-9][0-9 ().-]*\Z", re.ASCII)
_ISRAELI_MOBILE_LOCAL = re.compile(r"05[0-9]{8}\Z", re.ASCII)
_ISRAELI_MOBILE_INTERNATIONAL = re.compile(r"9725[0-9]{8}\Z", re.ASCII)
_INTERNATIONAL = re.compile(r"[1-9][0-9]{6,14}\Z", re.ASCII)


def normalize_new_input_phone(value: str) -> str:
    """Validate formatting and equate explicit Israeli mobile representations.

    This checks syntax, not number assignment, ownership, or consent. A bare country
    code is not guessed to be international. Non-mobile local numbers retain their
    legacy comparison key; explicit non-Israeli E.164 numbers retain their key.
    """
    raw = value.strip()
    if not raw or not _FORMATTED_PHONE.fullmatch(raw):
        return ""
    if raw.count("(") != raw.count(")"):
        return ""
    digits = re.sub(r"[^0-9]", "", raw)
    if raw.startswith("+"):
        if not _INTERNATIONAL.fullmatch(digits) or digits.startswith("9720"):
            return ""
        if digits.startswith("9725"):
            return f"+{digits}" if _ISRAELI_MOBILE_INTERNATIONAL.fullmatch(digits) else ""
        return f"+{digits}"
    if digits.startswith("05"):
        return f"+972{digits[1:]}" if _ISRAELI_MOBILE_LOCAL.fullmatch(digits) else ""
    return digits if 7 <= len(digits) <= 15 else ""
