"""Explicit company website/domain identity (§12.2). No inference, no HTTP."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

_MAX_HOSTNAME_LEN = 253
_TERMINAL_PUNCT_RE = re.compile(r"[.!?,;:]+$")
_CONTROL_OR_SPACE_RE = re.compile(r"[\s\u0000-\u001f\u007f]")
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_FORBIDDEN_HOST_SUBSTRINGS = (
    "secret",
    "token",
    "password",
    "apikey",
    "api_key",
)
_DANGEROUS_SCHEMES = frozenset({
    "javascript",
    "data",
    "file",
    "vbscript",
    "about",
    "blob",
})

_ENGLISH_MARKERS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"(?<![a-z])our website is\s+", re.IGNORECASE), 0),
    (re.compile(r"(?<![a-z])website is\s+", re.IGNORECASE), 0),
    (re.compile(r"(?<![a-z])our site is\s+", re.IGNORECASE), 0),
    (re.compile(r"(?<![a-z])company website\s+", re.IGNORECASE), 0),
    (re.compile(r"(?<![a-z])business website\s+", re.IGNORECASE), 0),
)

_HEBREW_MARKERS: tuple[str, ...] = (
    "האתר שלנו",
    "אתר העסק",
    "הדומיין שלנו",
    "האתר הוא",
)


def _strip_terminal_punctuation(text: str) -> str:
    return _TERMINAL_PUNCT_RE.sub("", text.strip())


def _normalize_hostname(hostname: str) -> str | None:
    host = hostname.strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None
    if len(host) > _MAX_HOSTNAME_LEN:
        return None
    if _CONTROL_OR_SPACE_RE.search(host):
        return None
    if "@" in host:
        return None
    for substring in _FORBIDDEN_HOST_SUBSTRINGS:
        if substring in host:
            return None
    labels = host.split(".")
    if any(not label for label in labels):
        return None
    ascii_labels: list[str] = []
    for label in labels:
        if len(label) > 63:
            return None
        try:
            if label.isascii():
                ascii_label = label
            else:
                ascii_label = label.encode("idna").decode("ascii")
        except (UnicodeError, UnicodeDecodeError):
            return None
        if not _LABEL_RE.fullmatch(ascii_label):
            return None
        ascii_labels.append(ascii_label)
    normalized = ".".join(ascii_labels)
    if len(normalized) > _MAX_HOSTNAME_LEN:
        return None
    return normalized


def _hostname_from_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip("[]"))
        return True
    except ValueError:
        return False


def _extract_host_candidate(value: str) -> str | None:
    trimmed = value.strip()
    if not trimmed or _CONTROL_OR_SPACE_RE.search(trimmed):
        return None
    if "@" in trimmed and not trimmed.lower().startswith(("http://", "https://")):
        return None

    if "://" in trimmed:
        parsed = urlparse(trimmed)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            return None
        if scheme in _DANGEROUS_SCHEMES:
            return None
        if parsed.username or parsed.password:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        if port is not None:
            return None
        hostname = parsed.hostname
        if not hostname:
            return None
        if _hostname_from_ip(hostname):
            return None
        return _normalize_hostname(hostname)

    if trimmed.lower().startswith("//"):
        return None

    host_part = trimmed.split("/")[0].split("?")[0].split("#")[0]
    if ":" in host_part:
        return None
    if _hostname_from_ip(host_part.strip("[]")):
        return None
    return _normalize_hostname(host_part)


def sanitize_company_domain(value: str) -> str | None:
    """Return normalized registrable hostname or None. Never performs HTTP."""
    if not isinstance(value, str):
        return None
    candidate = _strip_terminal_punctuation(value)
    if not candidate:
        return None
    host = _extract_host_candidate(candidate)
    if host is None:
        return None
    if host in ("localhost", "local"):
        return None
    if host.count(".") < 1:
        return None
    return host


def _candidate_after_marker(text: str, start: int) -> str:
    remainder = text[start:].lstrip(" :-\u2013\u2014")
    if not remainder:
        return ""
    token = remainder.split()[0]
    return _strip_terminal_punctuation(token)


def _extract_marked_domain(message: str) -> str | None:
    for pattern, _ in _ENGLISH_MARKERS:
        match = pattern.search(message)
        if match is None:
            continue
        candidate = _candidate_after_marker(message, match.end())
        domain = sanitize_company_domain(candidate)
        if domain is not None:
            return domain
    lowered = message
    for marker in _HEBREW_MARKERS:
        index = lowered.find(marker)
        if index < 0:
            continue
        candidate = _candidate_after_marker(message, index + len(marker))
        domain = sanitize_company_domain(candidate)
        if domain is not None:
            return domain
    return None


def extract_explicit_company_domain(message: str) -> str | None:
    """Conservative explicit domain extract. No LLM, no embedded URL without marker."""
    if not isinstance(message, str):
        return None
    trimmed = message.strip()
    if not trimmed:
        return None

    whole = _strip_terminal_punctuation(trimmed)
    whole_domain = sanitize_company_domain(whole)
    if whole_domain is not None:
        return whole_domain

    return _extract_marked_domain(trimmed)
