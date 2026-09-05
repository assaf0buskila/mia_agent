"""Website→WhatsApp handoff tokens. Pure helpers — no DB."""

from __future__ import annotations

import hashlib
import re
import secrets
from urllib.parse import quote

TOKEN_PREFIX = "mia1_"
_TOKEN_MIN_BODY_LEN = 16
_WRAP_FRAGMENT_RE = re.compile(
    r"^(?P<fragment>[A-Za-z0-9_-]{1,12})(?:\s+(?P<more>.*))?$",
    re.DOTALL,
)
HANDOFF_TOKEN_RE = re.compile(
    rf"^(?P<token>{TOKEN_PREFIX}[A-Za-z0-9_-]{{{_TOKEN_MIN_BODY_LEN},}})(?:(?P<ws>\s+)(?P<rest>.*))?$",
    re.DOTALL,
)


HANDOFF_PLACEHOLDER = "[website handoff]"
HANDOFF_COMPOSE_HINT_HE = "היי אסף, הגעתי מהאתר אחרי שיחה עם מיה."


def generate_handoff_token() -> str:
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(12)}"


def compose_handoff_text(raw_token: str | None = None) -> str:
    """Customer-facing wa.me prefill.

    Until Cloud API inbound works, Mia does not consume this message. The token
    is issued in the store for a later official API; it is not shown to the
    customer. Assaf gets the briefing on Telegram instead.
    """
    _ = raw_token
    return HANDOFF_COMPOSE_HINT_HE


def hash_handoff_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def extract_handoff_token(text: str) -> tuple[str, str] | None:
    stripped = text.strip()
    match = HANDOFF_TOKEN_RE.match(stripped)
    if match is None:
        return None
    token = match.group("token")
    rest = (match.group("rest") or "").strip()
    ws = match.group("ws") or ""
    if rest and any(char in "\n\r" for char in ws):
        wrap = _WRAP_FRAGMENT_RE.match(rest)
        if wrap is not None:
            token = f"{token}{wrap.group('fragment')}"
            rest = (wrap.group("more") or "").strip()
    return token, rest


def inbound_text_without_token(text: str) -> str:
    """Strip a leading handoff token so it is never persisted or sent to the graph."""
    extracted = extract_handoff_token(text)
    if extracted is None:
        return text
    _token, remaining = extracted
    return remaining if remaining else HANDOFF_PLACEHOLDER


def click_to_chat_digits(value: str) -> str:
    """Return digits-only click-to-chat number, or empty if the value is not a phone."""
    stripped = value.strip().lstrip("+").replace(" ", "")
    if not stripped or not stripped.isdigit():
        return ""
    return stripped


def click_to_chat_url(value: str, raw_token: str | None = None) -> str:
    """Return an https wa.me URL from the click-to-chat setting, or empty."""
    digits = click_to_chat_digits(value)
    if not digits:
        return ""
    return f"https://wa.me/{digits}?text={quote(compose_handoff_text(raw_token))}"
