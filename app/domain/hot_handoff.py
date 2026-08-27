"""Hot / close-ready handoff: stop selling, notify owner. Sync persist; Telegram best-effort."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import NamedTuple

import httpx

from app.capabilities.leads import leads_handlers
from app.capabilities.policy import execute_capability
from app.capabilities.types import Principal
from app.core.config import Settings
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.conversation_scope import TakeoverState
from app.domain.sales import SalesState

KIND_HOT_LEAD = "hot_lead"

_BRIEF_MAX = 500
_TELEGRAM_API = "https://api.telegram.org"


class OwnerNotifyAttempt(NamedTuple):
    """Result of one owner Telegram attempt. `attempted` is False on a duplicate claim."""

    delivered: tuple[str, ...]
    attempted: bool


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


def format_hot_leads_ack(store, *, principal: Principal) -> str:
    result = execute_capability(
        "leads.get_recent",
        principal=principal,
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
    *, brief: str, inbound_id: str, settings: Settings, parse_mode: str | None = "HTML"
) -> tuple[str, ...]:
    """Best-effort Telegram fan-out to every allowlisted owner id, not just the first.

    One recipient's send failing never stops the rest, and a failed send is never
    counted as delivered: the return value is exactly the chat ids that were actually
    sent to, in the same sorted order they were attempted. `inbound_id` is accepted for
    correlation parity with callers; the notify-once idempotency stays in the caller's
    `store.try_insert_owner_notification` claim, which this function does not touch — this
    fans out whatever it is handed, so the caller must not call it without a won claim.

    Telegram Bot API can return HTTP 200 with `ok: false` (or HTTP 400) for a parse_mode
    / chat-id problem. Those are not deliveries. Counting them as success is how a
    website handoff told the visitor the transfer happened while Assaf got nothing.
    """
    token = settings.telegram_bot_token.strip()
    owner_ids = settings.telegram_owner_user_id_set()
    if not token or not owner_ids:
        return ()
    delivered: list[str] = []
    with httpx.Client(timeout=10.0) as client:
        for chat_id in sorted(owner_ids):
            payload: dict[str, object] = {
                "chat_id": chat_id,
                "text": brief,
                "link_preview_options": {"is_disabled": True},
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            try:
                response = client.post(
                    f"{_TELEGRAM_API}/bot{token}/sendMessage",
                    json=payload,
                )
            except httpx.HTTPError:
                continue
            if not _telegram_accepted(response):
                continue
            delivered.append(chat_id)
    _ = inbound_id
    return tuple(delivered)


def _telegram_accepted(response: httpx.Response) -> bool:
    if response.status_code >= 400:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("ok") is True


def apply_hot_handoff(
    store,
    *,
    lead_id: str,
    inbound_id: str,
    want: str,
    kill_switch: bool,
    settings: Settings,
    brief: str | None = None,
    parse_mode: str | None = None,
) -> OwnerNotifyAttempt:
    """Mark HUMAN_TAKEOVER_REQUIRED, claim the notify, then Telegram. Never raise to inbound.

    Returns whether this call attempted a send and which chat ids Telegram accepted.
    Empty delivered means Assaf was not told — callers must not claim a transfer happened.

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
        return OwnerNotifyAttempt((), False)
    claimed = store.try_insert_owner_notification(
        kind=KIND_HOT_LEAD,
        lead_id=lead_id,
        scheduled_at=now_iso,
    )
    if not claimed:
        return OwnerNotifyAttempt((), False)
    sales = store.get_sales(lead_id)
    text = brief if brief is not None else format_hot_brief(
        lead_id=lead_id, sales=sales, want=want
    )
    mode = parse_mode if brief is not None else None
    delivered = notify_owners(
        brief=text, inbound_id=inbound_id, settings=settings, parse_mode=mode
    )
    if not delivered:
        # Claim-then-fail used to lock the lead forever: Assaf never got a retry
        # ping, and the visitor still saw a transfer claim. Release so the next
        # HANDOFF turn can try Telegram again.
        store.release_owner_notification_claim(kind=KIND_HOT_LEAD, lead_id=lead_id)
    return OwnerNotifyAttempt(delivered, True)
