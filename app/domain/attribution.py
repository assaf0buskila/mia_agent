"""Website and Instagram source attribution sanitization. Untrusted data — no PII or media URLs."""

from __future__ import annotations

import re
from urllib.parse import urlparse

WEBSITE_ATTRIBUTION_KEYS = frozenset({
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "landing_page",
    "referrer",
})

INSTAGRAM_ATTRIBUTION_KEYS = frozenset({
    "ig_content_id",
    "ig_trigger_source",
    "ig_ref",
    "meta_ad_id",
    "meta_post_id",
    "meta_campaign_id",
})

ATTRIBUTION_KEYS = WEBSITE_ATTRIBUTION_KEYS | INSTAGRAM_ATTRIBUTION_KEYS

_UTM_KEYS = frozenset({
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
})

_URL_KEYS = frozenset({"landing_page", "referrer"})

_FORBIDDEN_SUBSTRINGS = ("token", "secret", "password")
_DANGEROUS_SCHEMES = ("javascript:", "data:", "vbscript:")

_MAX_LEN = 200
_IG_CONTENT_ID_MAX = 64
_IG_REF_MAX = 64
_META_ID_MAX = 32

_IG_CONTENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")
_IG_REF_PATTERN = re.compile(r"^[A-Za-z0-9_=-]+$")
_META_ID_PATTERN = re.compile(r"^[0-9]+$")

_IG_TRIGGER_SOURCES = frozenset({"STORY", "SHORTLINKS", "ADS", "STORY_MENTION"})

_FORBIDDEN_IG_COPY_KEYS = frozenset({
    "url",
    "photo_url",
    "video_url",
    "ad_title",
})


def _has_forbidden_substring(value: str) -> bool:
    lower = value.lower()
    return any(part in lower for part in _FORBIDDEN_SUBSTRINGS)


def _sanitize_utm(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if "\n" in cleaned or "\r" in cleaned:
        return None
    if "@" in cleaned:
        return None
    if _has_forbidden_substring(cleaned):
        return None
    return cleaned[:_MAX_LEN]


def sanitize_url_value(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if "\n" in cleaned or "\r" in cleaned:
        return None
    lower = cleaned.lower()
    if any(lower.startswith(scheme) for scheme in _DANGEROUS_SCHEMES):
        return None
    if cleaned.startswith("//"):
        return None
    if cleaned.startswith("/"):
        path = cleaned.split("?", 1)[0].split("#", 1)[0]
        if not path or "@" in path:
            return None
        return path[:_MAX_LEN]
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        return None
    path = parsed.path or ""
    result = f"{parsed.scheme}://{parsed.netloc}{path}"
    if "@" in result:
        return None
    return result[:_MAX_LEN]


def _sanitize_ig_content_id(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _IG_CONTENT_ID_MAX:
        return None
    if "\n" in cleaned or "\r" in cleaned or "@" in cleaned:
        return None
    if _has_forbidden_substring(cleaned):
        return None
    if _IG_CONTENT_ID_PATTERN.fullmatch(cleaned) is None:
        return None
    return cleaned


def _sanitize_ig_trigger_source(value: str) -> str | None:
    cleaned = value.strip()
    if cleaned in _IG_TRIGGER_SOURCES:
        return cleaned
    return None


def _sanitize_ig_ref(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _IG_REF_MAX:
        return None
    if "\n" in cleaned or "\r" in cleaned or "@" in cleaned:
        return None
    if _IG_REF_PATTERN.fullmatch(cleaned) is None:
        return None
    return cleaned


def _sanitize_meta_id(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _META_ID_MAX:
        return None
    if _META_ID_PATTERN.fullmatch(cleaned) is None:
        return None
    return cleaned


def sanitize_attribution(raw: dict[str, str | None]) -> dict[str, str]:
    """Allowlist and sanitize website attribution params. Drops PII and unknown keys."""
    result: dict[str, str] = {}
    for key in WEBSITE_ATTRIBUTION_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if value is None or not value.strip():
            continue
        if key in _UTM_KEYS:
            sanitized = _sanitize_utm(value)
        elif key in _URL_KEYS:
            sanitized = sanitize_url_value(value)
        else:
            continue
        if sanitized:
            result[key] = sanitized
    return result


def sanitize_instagram_attribution(raw: dict[str, str | None]) -> dict[str, str]:
    """Allowlist Instagram organic/referral attribution. Never copies media URLs or ad titles."""
    result: dict[str, str] = {}
    for key in raw:
        if key in _FORBIDDEN_IG_COPY_KEYS or key not in INSTAGRAM_ATTRIBUTION_KEYS:
            continue
        value = raw[key]
        if value is None or not str(value).strip():
            continue
        text = str(value)
        sanitized: str | None
        if key == "ig_content_id":
            sanitized = _sanitize_ig_content_id(text)
        elif key == "ig_trigger_source":
            sanitized = _sanitize_ig_trigger_source(text)
        elif key == "ig_ref":
            sanitized = _sanitize_ig_ref(text)
        elif key in ("meta_ad_id", "meta_post_id", "meta_campaign_id"):
            sanitized = _sanitize_meta_id(text)
        else:
            continue
        if sanitized:
            result[key] = sanitized
    return result
