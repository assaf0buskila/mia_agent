"""Owner Telegram/WhatsApp status digest. Read-only counts. No execute. No sales graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.capabilities.types import Principal
from app.domain.handoff.hot import format_hot_leads_ack
from app.domain.owner.briefs import compute_daily_brief

if TYPE_CHECKING:
    from app.db.store import LeadStore

_MENU = (
    "אפשר לבקש: סיכום יומי, סיכום שבועי, לידים חמים, "
    "מה מחכה לאישור, השיחות מהאתר, מועדים פנויים, מה נקבע."
)
# Operator snapshots must not include _MENU. Greetings keep it; grounded reads do not.


def format_owner_status_ack(
    store: LeadStore, *, principal: Principal, timezone: str
) -> str:
    """Hebrew operator digest for greetings and unclassified owner text.

    Always the full dashboard, zeros included -- Assaf's explicit call for this
    surface: a predictable shape he can scan without wondering what got hidden,
    even on a quiet day where the general "omit an empty section" structure
    rule would otherwise apply. No filler opener: line 1 is always the day's
    numbers, not a greeting.
    """
    lines: list[str] = []
    snapshot = compute_daily_brief(store, timezone=timezone)
    if snapshot is not None:
        lines.append(
            "היום: "
            f"לידים {snapshot.leads}, "
            f"הוצעו {snapshot.meetings_offered}, "
            f"נקבעו {snapshot.meetings_booked}, "
            f"העברות {snapshot.handoffs}"
        )
    lines.append(f"אישורים ממתינים: {store.count_pending_approvals()}")
    lines.append(format_hot_leads_ack(store, principal=principal))
    lines.append(_MENU)
    lines.append("לא מבצעת כתיבות בלי בקשה מפורשת.")
    return "\n".join(lines)
