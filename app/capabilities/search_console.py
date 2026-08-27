"""Owner search_console.query — GSC analytics behind policy, never a Composio slug."""

from __future__ import annotations

from typing import Any

from app.core.errors import InvalidArguments
from app.integrations.search_console import SearchConsolePort

_ALLOWED_DIMENSIONS = frozenset({"page", "query"})


def search_console_query(port: SearchConsolePort, args: dict[str, Any]) -> dict[str, Any]:
    start_date = str(args.get("start_date") or "").strip()
    end_date = str(args.get("end_date") or "").strip()
    if not start_date or not end_date:
        raise InvalidArguments("start_date and end_date are required")
    raw_dims = args.get("dimensions") or ["page"]
    if not isinstance(raw_dims, list) or not raw_dims:
        raise InvalidArguments("dimensions must be a non-empty list")
    dimensions = [str(item).strip() for item in raw_dims if str(item).strip()]
    if not dimensions or any(item not in _ALLOWED_DIMENSIONS for item in dimensions):
        raise InvalidArguments("dimensions must be page and/or query")
    rows = port.query_search_analytics(
        start_date=start_date,
        end_date=end_date,
        dimensions=dimensions,
    )
    return {
        "count": len(rows),
        "rows": [
            {
                "page": row.page,
                "query": row.query,
                "clicks": row.clicks,
                "impressions": row.impressions,
                "ctr": row.ctr,
                "position": row.position,
            }
            for row in rows
        ],
    }


def search_console_handlers(port: SearchConsolePort) -> dict[str, Any]:
    return {"search_console.query": lambda args: search_console_query(port, args)}
