"""Application lifecycle and receipt bridge for durable CRM delivery."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from threading import Event, Thread

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import OwnerNotificationRecipientClaimRow
from app.db.store import LeadStore
from app.domain.handoff.delivery import (
    KIND_WEBSITE_HANDOFF_DELIVERY,
    WEBSITE_HANDOFF_DELIVERY_KINDS,
    website_ping_scope,
)
from app.integrations.sheets import build_sheets_port
from app.services.notifications import deliver_owner_telegram
from app.workers.crm_delivery import POLL_SECONDS, CrmDeliveryWorker, DeliveryOutcome

_LOG = logging.getLogger(__name__)


def telegram_receipt_handler(
    session_factory: Callable[[], Session],
    settings: Settings,
    *,
    transport: Callable[[str, str], None] | None = None,
):
    """Reuse the canonical website receipt; ambiguous sends keep their claim."""

    def handle(payload: Mapping[str, object], reconcile_only: bool) -> DeliveryOutcome:
        recipient = str(payload.get("recipient_id") or "")
        conversation = str(payload.get("conversation_id") or "")
        body = str(payload.get("text") or "")
        if not conversation or recipient not in settings.telegram_owner_user_id_set():
            return "conflict"
        lead_id, key = website_ping_scope(conversation)
        with session_factory() as db:
            store = LeadStore(db)
            if recipient in store.confirmed_owner_notification_recipients(
                kind=KIND_WEBSITE_HANDOFF_DELIVERY,
                lead_id=lead_id,
                notification_key=key,
            ):
                return "confirmed"
            prior = any(
                db.get(OwnerNotificationRecipientClaimRow, (kind, lead_id, key, recipient))
                is not None
                for kind in WEBSITE_HANDOFF_DELIVERY_KINDS
            )
            if prior:
                return "unknown"
            if reconcile_only:
                # A missing durable claim proves this bridge never started the send.
                return "failed"
            if settings.kill_switch or not settings.telegram_bot_token.strip() or not body.strip():
                return "failed"
            claimed = store.try_claim_owner_notification_recipient_compatible(
                kind=KIND_WEBSITE_HANDOFF_DELIVERY,
                compatible_kinds=WEBSITE_HANDOFF_DELIVERY_KINDS,
                lead_id=lead_id,
                notification_key=key,
                recipient_id=recipient,
                claimed_at=datetime.now(UTC).isoformat(),
            )
            db.commit()
            if not claimed:
                return "unknown"
            result = deliver_owner_telegram(
                text=body,
                settings=settings,
                recipient_ids=(recipient,),
                transport=transport,
            )
            rejected = result.rejected or ((recipient,) if result.no_attempt else ())
            persisted = store.record_owner_notification_recipient_delivery_outcomes_durably(
                kind=KIND_WEBSITE_HANDOFF_DELIVERY,
                lead_id=lead_id,
                notification_key=key,
                delivered_recipient_ids=result.delivered,
                rejected_recipient_ids=rejected,
            )
            if not persisted or result.ambiguous:
                return "unknown"
            return "confirmed" if recipient in result.delivered else "failed"

    return handle


def start_crm_runtime(
    settings: Settings,
    session_factory: Callable[[], Session],
) -> tuple[Event, Thread] | None:
    if not settings.crm_delivery_enabled:
        return None
    worker = CrmDeliveryWorker(
        session_factory=session_factory,
        sheets=build_sheets_port(settings),
        telegram_handler=telegram_receipt_handler(session_factory, settings),
        allowed_telegram_recipient_ids=frozenset(settings.telegram_owner_user_id_set()),
    )
    stopped = Event()

    def run() -> None:
        while not stopped.is_set():
            if not settings.kill_switch:
                try:
                    worker.run_once()
                except Exception:  # noqa: BLE001 - keep durable jobs available after transient failures
                    # Provider errors may contain credentials or visitor content.
                    _LOG.warning("CRM delivery cycle failed; durable jobs retained")
            stopped.wait(POLL_SECONDS)

    thread = Thread(target=run, name="mia-crm-delivery", daemon=True)
    thread.start()
    return stopped, thread
