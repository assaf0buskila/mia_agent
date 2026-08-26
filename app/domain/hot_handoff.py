"""Hot / close-ready handoff: stop selling, notify owner. Sync persist; Telegram best-effort."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from app.capabilities.leads import leads_handlers
from app.capabilities.policy import execute_capability
from app.capabilities.types import GraphName
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
    result = execute_capability(
        "leads.get_recent",
        graph=GraphName.OWNER,
        args={"limit": 12},
        handlers=leads_handlers(store),
    )
    ids = [str(item) for item in (result.get("hot_ids") or []) if item]
    if not ids:
        return "אין לידים חמים שמחכים לתפיסה."
    listed = ", ".join(ids[:12])
    extra = "" if len(ids) <= 12 else f" (+{len(ids) - 12})"
    return f"לידים חמים: {listed}{extra}"


def notify_owners(
    *, brief: str, inbound_id: str, settings: Settings, parse_mode: str = "HTML"
) -> tuple[str, ...]:
    """Best-effort Telegram fan-out to every allowlisted owner id, not just the first.

    One recipient's send failing never stops the rest, and a failed send is never
    counted as delivered: the return value is exactly the chat ids that were actually
    sent to, in the same sorted order they were attempted. `inbound_id` is accepted for
    correlation parity with callers; the notify-once idempotency stays in the caller's
    `store.try_insert_owner_notification` claim, which this function does not touch — this
    fans out whatever it is handed, so the caller must not call it without a won claim.
    """
    token = settings.telegram_bot_token.strip()
    owner_ids = settings.telegram_owner_user_id_set()
    if not token or not owner_ids:
        return ()
    delivered: list[str] = []
    with httpx.Client(timeout=10.0) as client:
        for chat_id in sorted(owner_ids):
            try:
                client.post(
                    f"{_TELEGRAM_API}/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": brief,
                        "parse_mode": parse_mode,
                        "link_preview_options": {"is_disabled": True},
                    },
                )
            except httpx.HTTPError:
                continue
            delivered.append(chat_id)
    _ = inbound_id
    return tuple(delivered)


def apply_hot_handoff(
    store,
    *,
    lead_id: str,
    inbound_id: str,
    want: str,
    kill_switch: bool,
    settings: Settings,
) -> None:
    """Mark HUMAN_TAKEOVER_REQUIRED, claim the notify, then Telegram. Never raise to inbound.

    The send is gated on a claiming insert that reports whether it actually won. The
    previous version persisted through an upsert that returns None and silently no-ops on a
    duplicate, then sent unconditionally — so every retry of the same inbound re-sent the
    brief and Assaf got the same hot lead over and over. One handoff, one message.
    """
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
    claimed = store.try_insert_owner_notification(
        kind=KIND_HOT_LEAD,
        lead_id=lead_id,
        scheduled_at=now_iso,
    )
    if not claimed:
        return
    sales = store.get_sales(lead_id)
    brief = format_hot_brief(lead_id=lead_id, sales=sales, want=want)
    notify_owners(brief=brief, inbound_id=inbound_id, settings=settings)
