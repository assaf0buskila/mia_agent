"""Domain idempotency store contract. Webhook claims stay on claim_webhook."""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

ALLOWLISTED_OPERATION_SCOPES = frozenset(
    {
        "calendar_create",
        "calendar_reschedule",
        "canonical",
        "approval",
        "owner_task",
        "sheets_mirror",
        "follow_up",
        "calendar_cancellation",
    }
)

ALLOWLISTED_OPERATION_STATUSES = frozenset({"in_flight", "completed", "failed"})

OPERATION_TTL_SECONDS = 300

_MAX_RESULT_JSON_LEN = 512
_MAX_EVENT_ID_LEN = 128
_ALLOWLISTED_RESULT_KEYS = frozenset({"ok", "event_id"})


def sanitize_operation_result(result_json: str) -> str:
    """Return a tiny sanitized result object JSON string; never lead text or PII."""
    try:
        parsed = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        return "{}"
    if not isinstance(parsed, dict):
        return "{}"
    if set(parsed.keys()) - _ALLOWLISTED_RESULT_KEYS:
        return "{}"
    if "ok" in parsed and not isinstance(parsed["ok"], bool):
        return "{}"
    event_id = parsed.get("event_id")
    if event_id is not None and (
        not isinstance(event_id, str) or len(event_id) > _MAX_EVENT_ID_LEN
    ):
        return "{}"
    sanitized = json.dumps(parsed, separators=(",", ":"), sort_keys=True)
    if len(sanitized) > _MAX_RESULT_JSON_LEN:
        return "{}"
    return sanitized


@runtime_checkable
class IdempotencyStore(Protocol):
    def claim_webhook(
        self,
        *,
        provider: str,
        provider_event_id: str,
        channel: str = "",
        envelope_kind: str = "",
    ) -> bool: ...

    def claim_operation(
        self, *, scope: str, key: str, ttl_seconds: int = OPERATION_TTL_SECONDS
    ) -> bool: ...

    def complete_operation(
        self, *, scope: str, key: str, result_json: str = "{}"
    ) -> None: ...

    def fail_operation(self, *, scope: str, key: str) -> None: ...

    def get_operation_result(self, *, scope: str, key: str) -> str: ...
