"""Owner analytics.get_traffic — GA4 read behind policy, never a Composio slug."""

from __future__ import annotations

from typing import Any

from app.core.errors import InvalidArguments
from app.integrations.ga4 import Ga4Port


def analytics_get_traffic(port: Ga4Port, args: dict[str, Any]) -> dict[str, Any]:
    start_date = str(args.get("start_date") or "").strip()
    end_date = str(args.get("end_date") or "").strip()
    if not start_date or not end_date:
        raise InvalidArguments("start_date and end_date are required")
    rows = port.run_pivot_report(start_date=start_date, end_date=end_date)
    conversions = port.list_conversion_events()
    return {
        "count": len(rows),
        "rows": [
            {
                "landing_page": row.landing_page,
                "session_source": row.session_source,
                "sessions": row.sessions,
                "engaged_sessions": row.engaged_sessions,
                "users": row.users,
                "conversions": row.conversions,
            }
            for row in rows
        ],
        "conversions": [str(item) for item in conversions if str(item).strip()],
    }


def analytics_handlers(port: Ga4Port) -> dict[str, Any]:
    return {"analytics.get_traffic": lambda args: analytics_get_traffic(port, args)}
