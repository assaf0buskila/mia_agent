"""Owner leads.get_recent — Postgres sales facts, never visitor transcripts or Composio."""

from __future__ import annotations

from typing import Any

from app.core.errors import InvalidArguments
from app.db.store import LeadStore


def _limit(args: dict[str, Any]) -> int:
    raw = args.get("limit", 8)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidArguments("limit must be an integer") from exc
    return max(1, min(value, 20))


def leads_get_recent(store: LeadStore, args: dict[str, Any]) -> dict[str, Any]:
    limit = _limit(args)
    snapshots = store.list_sales_snapshots(limit=limit)
    return {
        "leads": [
            {
                "lead_id": sales.lead_id,
                "fit": sales.fit.value,
                "pain": int(sales.pain_level),
                "workflow_known": sales.workflow_known,
                "headline": (sales.headline or "")[:80],
            }
            for sales in snapshots
        ],
        "hot_ids": store.list_hot_lead_ids()[:limit],
    }


def leads_handlers(store: LeadStore) -> dict[str, Any]:
    return {"leads.get_recent": lambda args: leads_get_recent(store, args)}
