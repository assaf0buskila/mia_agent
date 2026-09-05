"""Freshness outcomes for config-backed routing facts (not integrations)."""

from datetime import datetime

from app.domain.policies.freshness import overlay_stale, stamp_freshness
from app.domain.tools import ToolOutcome

VALID_INSTAGRAM_SENDERS = frozenset({"direct", "composio"})


def owner_permissions_outcome(*, present: bool, now: datetime) -> ToolOutcome:
    base_status = "ok" if present else "empty"
    stamp = stamp_freshness(
        "owner_permissions",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="owner_permissions",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=1 if present else 0,
        freshness=stamp.status,
    )
