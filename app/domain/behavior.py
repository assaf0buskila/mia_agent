"""Website funnel behavior sanitization. Client payloads are untrusted data — no PII."""

from __future__ import annotations

import re

from app.domain.attribution import sanitize_url_value

CLIENT_BEHAVIOR_KINDS = frozenset({
    "page_viewed",
    "section_viewed",
    "cta_click",
    "form_started",
    "form_abandoned",
})

SERVER_BEHAVIOR_KINDS = frozenset({
    "mia_opened",
    "conversation_started",
    "whatsapp_handoff",
    "whatsapp_handoff_offered",
})

ALL_BEHAVIOR_KINDS = CLIENT_BEHAVIOR_KINDS | SERVER_BEHAVIOR_KINDS

_FORBIDDEN_SUBSTRINGS = ("token", "secret", "password")
_SLUG_MAX = 80
_SLUG_RE = re.compile(r"^[a-zA-Z0-9_\-\u0590-\u05FF]+$")


def _has_forbidden_substring(value: str) -> bool:
    lower = value.lower()
    return any(part in lower for part in _FORBIDDEN_SUBSTRINGS)


def _sanitize_slug(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if "\n" in cleaned or "\r" in cleaned:
        return None
    if "@" in cleaned:
        return None
    if " " in cleaned:
        return None
    if _has_forbidden_substring(cleaned):
        return None
    if not _SLUG_RE.fullmatch(cleaned):
        return None
    return cleaned[:_SLUG_MAX]


def sanitize_client_behavior(
    *,
    kind: str,
    path: str | None = None,
    section: str | None = None,
    cta: str | None = None,
) -> dict[str, str] | None:
    """Allowlist and sanitize client-posted behavior. Returns None if required fields missing."""
    if kind not in CLIENT_BEHAVIOR_KINDS:
        return None
    if kind == "page_viewed":
        sanitized_path = sanitize_url_value(path) if path is not None else None
        if not sanitized_path:
            return None
        return {"kind": kind, "path": sanitized_path}
    if kind == "section_viewed":
        sanitized_section = _sanitize_slug(section)
        if not sanitized_section:
            return None
        return {"kind": kind, "section": sanitized_section}
    if kind == "cta_click":
        sanitized_cta = _sanitize_slug(cta)
        if not sanitized_cta:
            return None
        return {"kind": kind, "cta": sanitized_cta}
    if kind == "form_started":
        return {"kind": kind}
    if kind == "form_abandoned":
        return {"kind": kind}
    return None


def behavior_provider_event_id(session_id: str, payload: dict[str, str]) -> str:
    kind = payload["kind"]
    if kind == "mia_opened":
        return f"{session_id}:mia_opened"
    if kind == "conversation_started":
        return f"{session_id}:conversation_started"
    if kind == "whatsapp_handoff":
        return f"{session_id}:whatsapp_handoff"
    if kind == "whatsapp_handoff_offered":
        return f"{session_id}:whatsapp_handoff_offered"
    if kind == "page_viewed":
        return f"{session_id}:page:{payload['path']}"
    if kind == "section_viewed":
        return f"{session_id}:section:{payload['section']}"
    if kind == "cta_click":
        return f"{session_id}:cta:{payload['cta']}"
    if kind == "form_started":
        return f"{session_id}:form_started"
    if kind == "form_abandoned":
        return f"{session_id}:form_abandoned"
    raise ValueError(f"unknown behavior kind: {kind}")
