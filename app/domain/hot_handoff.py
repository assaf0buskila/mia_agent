"""Hot / close-ready handoff: stop selling, notify owner. Sync persist; Telegram best-effort."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from app.core.config import Settings
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.conversation_scope import TakeoverState
from app.domain.sales import SalesState

KIND_HOT_LEAD = "hot_lead"

_BRIEF_MAX = 500
_TELEGRAM_API = "https://api.telegram.org"


def format_hot_brief(*, lead_id: str, sales: SalesState, want: str) -> str:
    lines = [
        "ליד חם — מיה עוצרת.",
        lead_id,
        f"fit={sales.fit.value}",
        f"pain={int(sales.pain_level)}",
        f"workflow={'yes' if sales.workflow_known else 'no'}",
        f"want={want[:120]}" if want else "want=handoff",
        "הבא: תפיסה אנושית.",
    ]
    return "\n".join(lines)[:_BRIEF_MAX]


def format_hot_leads_ack(store) -> str:
    ids = store.list_hot_lead_ids()
    if not ids:
        return "אין לידים חמים שמחכים לתפיסה."
    listed = ", ".join(ids[:12])
    extra = "" if len(ids) <= 12 else f" (+{len(ids) - 12})"
    return f"לידים חמים: {listed}{extra}"


def _notify_telegram(*, brief: str, inbound_id: str, settings: Settings) -> None:
    token = settings.telegram_bot_token.strip()
    owner_ids = settings.telegram_owner_user_id_set()
    if not token or not owner_ids:
        return
    chat_id = sorted(owner_ids)[0]
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(
                f"{_TELEGRAM_API}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": brief},
            )
    except httpx.HTTPError:
        return
    _ = inbound_id


def apply_hot_handoff(
    store,
    *,
    lead_id: str,
    inbound_id: str,
    want: str,
    kill_switch: bool,
    settings: Settings,
) -> None:
    """Mark HUMAN_TAKEOVER_REQUIRED, persist notify, optional Telegram. Never raise to inbound."""
    store.set_takeover_state(lead_id, TakeoverState.HUMAN_TAKEOVER_REQUIRED.value)
    store.cancel_pending_follow_up(lead_id)
    now_iso = datetime.now(UTC).replace(microsecond=0).isoformat()
    try:
        assert_allowed(
            RiskAction(name="hot_handoff_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    store.upsert_owner_notification(
        kind=KIND_HOT_LEAD,
        lead_id=lead_id,
        scheduled_at=now_iso,
    )
    sales = store.get_sales(lead_id)
    brief = format_hot_brief(lead_id=lead_id, sales=sales, want=want)
    _notify_telegram(brief=brief, inbound_id=inbound_id, settings=settings)
