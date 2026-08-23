"""Gmail read port — optional message hydration only.

Production adapter: Composio ``GMAIL`` toolkit version ``20260817_00``,
pin ``GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID`` only when ``MIA_COMPOSIO_API_KEY`` and
``MIA_COMPOSIO_USER_ID`` are set. Managed OAuth **Yes**.
Never send, delete, forward, or MIME-decode this slice.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from app.core.config import Settings
from app.domain.policies.freshness import overlay_stale, stamp_freshness
from app.domain.tools import AdapterHttpError, ToolOutcome

COMPOSIO_GMAIL_VERSION = "20260817_00"
COMPOSIO_FETCH_MESSAGE_TOOL = "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"
GMAIL_NEW_MESSAGE_TRIGGER = "GMAIL_NEW_GMAIL_MESSAGE"
_COMPOSIO_EXECUTE_URL = (
    f"https://backend.composio.dev/api/v3.1/tools/execute/{COMPOSIO_FETCH_MESSAGE_TOOL}"
)


class InboundEmail(BaseModel):
    message_id: str
    sender: str
    subject: str = ""
    text: str = ""
    thread_id: str = ""


class GmailPort(Protocol):
    def fetch_message(self, message_id: str) -> InboundEmail | None: ...


class DisabledGmailPort:
    def fetch_message(self, message_id: str) -> InboundEmail | None:
        return None


class ComposioGmailPort:
    """Live Composio GMAIL fetch adapter. Raises AdapterHttpError on HTTP."""

    def __init__(
        self,
        *,
        api_key: str,
        user_id: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._client = client

    def fetch_message(self, message_id: str) -> InboundEmail | None:
        payload = {
            "user_id": self._user_id,
            "version": COMPOSIO_GMAIL_VERSION,
            "arguments": {
                "message_id": message_id,
                "format": "full",
            },
        }
        headers = {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = self._client.post(
                    _COMPOSIO_EXECUTE_URL,
                    json=payload,
                    headers=headers,
                )
            else:
                with httpx.Client(timeout=20.0) as client:
                    response = client.post(
                        _COMPOSIO_EXECUTE_URL,
                        json=payload,
                        headers=headers,
                    )
        except httpx.HTTPError as exc:
            raise AdapterHttpError(None) from exc
        if response.status_code >= 400:
            raise AdapterHttpError(response.status_code)
        try:
            body = response.json()
            if not isinstance(body, dict) or body.get("successful") is not True:
                return None
            data = body.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    return None
            if not isinstance(data, dict):
                return None
            return _map_fetch_data(data, message_id=message_id)
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ):
            return None


class FakeGmailPort:
    """Test double for optional message hydration."""

    def __init__(self, messages: dict[str, InboundEmail] | None = None) -> None:
        self._messages = messages or {}

    def fetch_message(self, message_id: str) -> InboundEmail | None:
        return self._messages.get(message_id)


def build_gmail_port(settings: Settings) -> GmailPort:
    api_key = settings.composio_api_key.strip()
    user_id = settings.composio_user_id.strip()
    if api_key and user_id:
        return ComposioGmailPort(api_key=api_key, user_id=user_id)
    return DisabledGmailPort()


def parse_sender_email(sender: str) -> str:
    """Extract the address from `Name <a@b.com>` or return the raw value."""
    cleaned = sender.strip()
    if "<" in cleaned and ">" in cleaned:
        start = cleaned.rindex("<") + 1
        end = cleaned.rindex(">")
        return cleaned[start:end].strip()
    return cleaned


def build_inbound_text(*, subject: str, message_text: str) -> str:
    parts = [part.strip() for part in (subject, message_text) if part and part.strip()]
    return "\n".join(parts)


def gmail_results_outcome(
    *,
    present: bool,
    now: datetime,
    latency_ms: int = 0,
) -> ToolOutcome:
    base_status = "ok" if present else "empty"
    stamp = stamp_freshness(
        "gmail_results",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="gmail_fetch",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=1 if present else 0,
        latency_ms=latency_ms,
        freshness=stamp.status,
    )


def hydrate_gmail_item(item: dict[str, str], port: GmailPort) -> dict[str, str]:
    if item.get("text", "").strip():
        return item
    fetched = port.fetch_message(item["id"])
    if fetched is None:
        return item
    updated = dict(item)
    built = build_inbound_text(subject=fetched.subject, message_text=fetched.text)
    if built:
        updated["text"] = built
    if fetched.thread_id and not updated.get("thread_id"):
        updated["thread_id"] = fetched.thread_id
    return updated


def _non_empty_str(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _extract_message_text(data: dict[str, Any]) -> str:
    for key in ("message_text", "messageText", "snippet", "text"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    body = data.get("body")
    if isinstance(body, str) and body.strip():
        return body.strip()
    return ""


def _map_fetch_data(data: dict[str, Any], *, message_id: str) -> InboundEmail:
    sender_raw = _non_empty_str(data.get("sender") or data.get("from")) or ""
    sender = parse_sender_email(sender_raw) if sender_raw else ""
    subject = _non_empty_str(data.get("subject")) or ""
    text = _extract_message_text(data)
    thread_id = _non_empty_str(data.get("thread_id") or data.get("threadId")) or ""
    return InboundEmail(
        message_id=message_id,
        sender=sender,
        subject=subject,
        text=text,
        thread_id=thread_id,
    )
