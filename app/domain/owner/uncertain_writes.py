"""Owner-visible list of provider writes whose outcome was never confirmed.

`claim_provider_write` + `mark_provider_write_pending_review` (`app/db/store.py`)
deliberately leave a handful of write paths -- calendar create/reschedule
(`app/domain/owner/calendar_writes.py`), Gmail send (`app/domain/gmail/drafts.py`),
LinkedIn Composio actions (`app/domain/owner/linkedin_writes.py`), any other
approved Composio tool write (`app/domain/owner/composio_writes.py`), and the
generic `propose_owner_action` execute step (`app/services/owner_actions.py`) --
parked in ``provider_claimed`` or ``pending_review`` forever once the outcome is
uncertain: an unconfirmed write must never be auto-retried. Until now nothing
surfaced those rows to Assaf; `claim_provider_write` just refused silently on
any retry.

This module only *describes* a stuck row for display: what kind of write, roughly
when, and a short plain-words target. It never dumps a raw payload, a connection
id or an account hash, and it never mutates a row -- reconciling or retrying a
stuck write is out of scope here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.approvals import (
    ACTION_CALENDAR_CREATE,
    ACTION_CALENDAR_RESCHEDULE,
    ACTION_COMPOSIO_WRITE,
    ACTION_GMAIL_SEND,
    ACTION_LINKEDIN_COMPOSIO_WRITE,
    RESOURCE_CALENDAR,
    RESOURCE_COMPOSIO_TOOL,
    RESOURCE_LINKEDIN_TOOL,
)
from app.services.owner_actions import (
    ACTION_OWNER_EXTERNAL_WRITE,
    RESOURCE_OWNER_PROPOSAL,
    read_owner_action,
)

# A write claimed moments ago is not "stuck" yet -- the provider call may simply
# still be in flight. Only surface a row once it has sat unconfirmed past this.
UNCERTAIN_WRITE_GRACE_SECONDS = 120
MAX_UNCERTAIN_WRITES = 10
_STUCK_STATUSES = ("pending_review", "provider_claimed")

# Module-level so a later chunk can add this to the tool loop's exact "no data"
# markers without having to know this module's internals.
OWNER_UNCERTAIN_WRITES_EMPTY = "No uncertain provider writes are waiting on review."

_TARGET_FIELD_PRIORITY: tuple[str, ...] = (
    "recipient",
    "to",
    "title",
    "event_title",
    "contact_id",
    "contact",
    "name",
)
_MAX_TARGET_CHARS = 120


@dataclass(frozen=True)
class UncertainWrite:
    scope: str
    status: str
    kind: str
    target: str
    when_local: str


def _resolve_zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _parse_created_at(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _local_time(moment: datetime, *, timezone: str) -> str:
    return moment.astimezone(_resolve_zone(timezone)).strftime("%Y-%m-%d %H:%M")


def _plain_target(target: dict) -> str:
    for field in _TARGET_FIELD_PRIORITY:
        value = target.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_TARGET_CHARS]
    return ""


def _owner_proposal_target(store, resource_id: str) -> tuple[str, str] | None:
    """(kind, plain-words target) from a `propose_owner_action` envelope, when
    that approval row is still readable and intact. None when unresolvable."""
    row = store.get_approval_by_resource(
        RESOURCE_OWNER_PROPOSAL, resource_id, ACTION_OWNER_EXTERNAL_WRITE
    )
    if row is None:
        return None
    envelope = read_owner_action(row)
    if envelope is None:
        return None
    kind = str(envelope.get("kind") or "owner action")
    target = envelope.get("target")
    return kind, _plain_target(target if isinstance(target, dict) else {})


def _calendar_target(store, resource_id: str, action: str) -> str:
    row = store.get_approval_by_resource(RESOURCE_CALENDAR, resource_id, action)
    if row is None:
        return ""
    try:
        value = json.loads(row.proposed_parameters)
    except (TypeError, ValueError):
        return ""
    if not isinstance(value, dict):
        return ""
    title = str(value.get("title") or "").strip()
    start = str(value.get("start") or "").strip()
    if not title:
        return ""
    return f"{title} · {start}" if start else title


def _slug_target(store, *, resource_type: str, action: str, resource_id: str) -> str:
    """Plain tool-slug target shared by LinkedIn and generic Composio writes --
    both persist `{"arguments": ..., "slug": ...}` (see `_parameters` in
    `linkedin_writes.py` / `composio_writes.py`). Only the slug is surfaced;
    `arguments` may hold arbitrary provider payload and is never read here.
    """
    row = store.get_approval_by_resource(resource_type, resource_id, action)
    if row is None:
        return ""
    try:
        value = json.loads(row.proposed_parameters)
    except (TypeError, ValueError):
        return ""
    if not isinstance(value, dict):
        return ""
    return str(value.get("slug") or "").strip()


def describe_stuck_write(store, *, scope: str, key: str) -> tuple[str, str]:
    """(kind label, plain-words target) for one stuck idempotency key.

    Never raises: an unresolvable key still returns a usable, honest label rather
    than crashing the whole list. `key` shapes are those actually written by the
    provider-write call sites (see module docstring); anything else falls through
    to a generic label with no target.
    """
    parts = key.split(":")
    resource_id = parts[0] if parts else ""
    if scope == "approval" and len(parts) == 2 and parts[1] == "execute":
        resolved = _owner_proposal_target(store, resource_id)
        if resolved is not None:
            return resolved
        return "Owner action", ""
    if scope == "approval" and len(parts) == 3 and parts[1] == "execute":
        action = parts[2]
        if action == ACTION_CALENDAR_CREATE:
            return "Calendar create", _calendar_target(store, resource_id, action)
        if action == ACTION_CALENDAR_RESCHEDULE:
            return "Calendar reschedule", _calendar_target(store, resource_id, action)
        return "Owner action", ""
    if (
        scope == "approval"
        and len(parts) == 3
        and parts[1] == "send"
        and parts[2] == ACTION_GMAIL_SEND
    ):
        return "Gmail send", f"draft {resource_id[:16]}" if resource_id else ""
    if scope == "linkedin_approval":
        target = _slug_target(
            store,
            resource_type=RESOURCE_LINKEDIN_TOOL,
            action=ACTION_LINKEDIN_COMPOSIO_WRITE,
            resource_id=resource_id,
        )
        return "LinkedIn action", target
    if scope == "composio_approval" and len(parts) == 2 and parts[1] == "execute":
        target = _slug_target(
            store,
            resource_type=RESOURCE_COMPOSIO_TOOL,
            action=ACTION_COMPOSIO_WRITE,
            resource_id=resource_id,
        )
        return "Composio action", target
    return f"{scope} write", ""


def list_uncertain_writes(
    store,
    *,
    now: datetime,
    timezone: str,
    grace_seconds: int = UNCERTAIN_WRITE_GRACE_SECONDS,
    limit: int = MAX_UNCERTAIN_WRITES,
) -> list[UncertainWrite]:
    cutoff = now - timedelta(seconds=grace_seconds)
    rows = store.list_stuck_provider_writes(older_than=cutoff, limit=limit)
    items: list[UncertainWrite] = []
    for row in rows:
        created = _parse_created_at(row.created_at) or now
        kind, target = describe_stuck_write(store, scope=row.scope, key=row.key)
        items.append(
            UncertainWrite(
                scope=row.scope,
                status=row.status,
                kind=kind,
                target=target,
                when_local=_local_time(created, timezone=timezone),
            )
        )
    return items


def format_uncertain_writes(items: list[UncertainWrite]) -> str:
    if not items:
        return OWNER_UNCERTAIN_WRITES_EMPTY
    lines = [
        "These may or may not have happened. Mia will not retry them automatically "
        "-- check the provider (Gmail/Calendar/LinkedIn/Composio) before redoing any "
        "of them.",
    ]
    for index, item in enumerate(items, start=1):
        target_part = f" -- {item.target}" if item.target else ""
        lines.append(f"{index}. {item.kind}{target_part} · {item.when_local} · {item.status}")
    return "\n".join(lines)
