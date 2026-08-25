"""Read-only operator snapshot for Assaf. Postgres facts. No menu. No writes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.engine_health import compute_engine_health, format_engine_health
from app.domain.funnel import compute_website_funnel, format_website_funnel
from app.domain.hot_handoff import format_hot_leads_ack
from app.domain.owner_briefs import compute_daily_brief
from app.domain.owner_notify import (
    OWNER_NOTIFY_KINDS,
    format_owner_notify_inbox,
)
from app.domain.owner_reads import (
    format_pending_approvals_ack,
    format_website_conversations_ack,
)
from app.domain.owner_weeklies import compute_weekly_brief

if TYPE_CHECKING:
    from app.db.store import LeadStore

_NO_WRITE_LINE = "לא כתבתי כלום."
_SNAPSHOT_PENDING_LIMIT = 4
_DEFAULT_SECTIONS: tuple[str, ...] = (
    "daily_brief",
    "pending_approvals",
    "website_conversations",
    "hot_leads",
)


def _daily_counts_line(store: LeadStore, *, timezone: str) -> str | None:
    snapshot = compute_daily_brief(store, timezone=timezone)
    if snapshot is None:
        return None
    return (
        "היום: "
        f"לידים {snapshot.leads} · "
        f"הוצעו {snapshot.meetings_offered} · "
        f"נקבעו {snapshot.meetings_booked} · "
        f"העברות {snapshot.handoffs}"
    )


def _weekly_counts_line(store: LeadStore, *, timezone: str) -> str | None:
    snapshot = compute_weekly_brief(store, timezone=timezone)
    if snapshot is None:
        return None
    return (
        "השבוע: "
        f"לידים {snapshot.leads} · "
        f"הוצעו {snapshot.meetings_offered} · "
        f"נקבעו {snapshot.meetings_booked} · "
        f"העברות {snapshot.handoffs}"
    )


def _owner_notify_readonly(store: LeadStore, *, timezone: str) -> str:
    total = store.count_unseen_owner_notifications(kinds=OWNER_NOTIFY_KINDS)
    rows = store.list_unseen_owner_notifications(
        kinds=OWNER_NOTIFY_KINDS, limit=3
    )
    return format_owner_notify_inbox(
        rows, timezone=timezone, total_unseen=total
    )


def format_operator_snapshot_ack(
    store: LeadStore,
    *,
    timezone: str,
    matched_types: list[str] | None = None,
) -> str:
    """Grounded Hebrew snapshot from Postgres. Never a command menu. Never a write."""
    selected = [item for item in (matched_types or []) if item != "operator_snapshot"]
    sections = tuple(selected) if selected else _DEFAULT_SECTIONS
    blocks: list[str] = []
    if "daily_brief" in sections or "owner_status" in sections:
        daily = _daily_counts_line(store, timezone=timezone)
        if daily is not None:
            blocks.append(daily)
        funnel = compute_website_funnel(store, timezone=timezone)
        if funnel is not None and funnel.has_signal():
            blocks.append(format_website_funnel(funnel))
        engine = compute_engine_health(store, timezone=timezone)
        if engine is not None and engine.total_runs:
            blocks.append(format_engine_health(engine))
    if "weekly_brief" in sections:
        weekly = _weekly_counts_line(store, timezone=timezone)
        if weekly is not None:
            blocks.append(weekly)
    if "pending_approvals" in sections:
        blocks.append(
            format_pending_approvals_ack(store, limit=_SNAPSHOT_PENDING_LIMIT)
        )
    if "website_conversations" in sections:
        blocks.append(format_website_conversations_ack(store))
    if "hot_leads" in sections:
        blocks.append(format_hot_leads_ack(store))
    if "owner_notify" in sections:
        blocks.append(_owner_notify_readonly(store, timezone=timezone))
    blocks.append(_NO_WRITE_LINE)
    return "\n".join(block for block in blocks if block)
