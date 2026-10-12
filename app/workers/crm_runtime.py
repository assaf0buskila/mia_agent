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
    form_ping_scope,
    website_ping_scope,
)
from app.domain.owner.brain import build_agent_client
from app.domain.owner.callbacks import approval_token
from app.domain.owner.proposal_cards import render_owner_proposal_card
from app.integrations.sheets import build_sheets_port
from app.integrations.telegram_format import (
    MAX_MESSAGE_CHARS,
    esc,
    lead_path_keyboard,
    owner_text,
    plain_text_length,
    render_lead_card,
)
from app.services.assaf_os_leads import prepare_lead_choice
from app.services.notifications import deliver_owner_telegram
from app.workers.crm_delivery import POLL_SECONDS, CrmDeliveryWorker, DeliveryOutcome

_LOG = logging.getLogger(__name__)


def telegram_receipt_handler(
    session_factory: Callable[[], Session],
    settings: Settings,
    *,
    transport: Callable[[str, str], None] | None = None,
    lead_template_client: object | None = None,
):
    """Reuse the canonical website receipt; ambiguous sends keep their claim."""

    def handle(payload: Mapping[str, object], reconcile_only: bool) -> DeliveryOutcome:
        recipient = str(payload.get("recipient_id") or "")
        conversation = str(payload.get("conversation_id") or "")
        body = str(payload.get("text") or "")
        if not conversation or recipient not in settings.telegram_owner_user_id_set():
            return "conflict"
        try:
            lead_id, key = (
                form_ping_scope(conversation)
                if conversation.startswith("form:")
                else website_ping_scope(conversation)
            )
        except (AssertionError, ValueError):
            return "conflict"
        # A contact update after the first handoff carries its own scope, so it gets its
        # own at-most-once claim and can never replay or suppress the first ping.
        scope = str(payload.get("ping_scope") or "")
        if scope:
            key = f"{key}:{scope}"
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
            # Non-card legacy summaries still belong to the claimed notification.
            # Escape them before composing enrichment so a template can never replace
            # or reinterpret the original website ping.
            card = render_lead_card(body) or esc(body)
            reply_markup = None
            if settings.assaf_os_enabled and not scope and payload.get("lead_source"):
                try:
                    prepared = prepare_lead_choice(
                        store,
                        settings=settings,
                        recipient_id=recipient,
                        payload=payload,
                        client=(lead_template_client or build_agent_client(settings)),
                    )
                    db.commit()
                    prepared_card = render_owner_proposal_card(prepared.envelope)
                    combined = "\n\n".join(part for part in (card, prepared_card) if part)
                    if plain_text_length(owner_text(combined, html=True)) <= MAX_MESSAGE_CHARS:
                        card = combined
                    else:
                        template = prepared.envelope.get("target", {}).get("template", {})
                        recommendation = str(template.get("recommended_path") or "unknown")
                        compact = (
                            "🧭 הכנת ליד ל-Assaf OS\n"
                            f"המלצת מסלול: {recommendation}\n"
                            + (
                                "חסרים פרטים או נדרש תיקון מפורש; לא הוכנה כתיבה."
                                if prepared.blocked
                                else (
                                    "בחירת מסלול רק מכינה כרטיס אישור נפרד; "
                                    "עדיין לא נכתבה רשומה."
                                )
                            )
                        )
                        compact_combined = "\n\n".join(
                            part for part in (card, compact) if part
                        )
                        if (
                            plain_text_length(owner_text(compact_combined, html=True))
                            <= MAX_MESSAGE_CHARS
                        ):
                            card = compact_combined
                    if not prepared.blocked:
                        reply_markup = lead_path_keyboard(
                            approval_token(prepared.proposal.approval_id)
                        )
                except Exception as exc:  # noqa: BLE001 - ping survives enrichment failure
                    db.rollback()
                    _LOG.warning(
                        "lead proposal preparation unavailable reason=preparation_failed error=%s",
                        type(exc).__name__,
                    )
                    fallback = (
                        "\n\nלא הוכנה הצעת הכנסה לגיליון. הפנייה נשמרה ב-CRM; "
                        "אפשר לבקש ממיה להכין אותה שוב לפי מזהה המקור."
                    )
                    fallback_card = f"{card}{fallback}" if card else f"{body}{fallback}"
                    if (
                        plain_text_length(owner_text(fallback_card, html=True))
                        <= MAX_MESSAGE_CHARS
                    ):
                        card = fallback_card
            result = deliver_owner_telegram(
                text=owner_text(card, html=True) if card else owner_text(body),
                settings=settings,
                recipient_ids=(recipient,),
                transport=transport,
                parse_mode="HTML" if card else None,
                reply_markup=reply_markup,
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
        legacy_sheet_projection_enabled=not settings.assaf_os_enabled,
    )
    stopped = Event()

    def run() -> None:
        while not stopped.is_set():
            if not settings.kill_switch:
                try:
                    worker.run_once()
                except Exception as exc:  # noqa: BLE001 - keep durable jobs available
                    # Provider errors may contain credentials or visitor content, so log
                    # only the exception class.
                    _LOG.warning(
                        "CRM delivery cycle failed error=%s; durable jobs retained",
                        type(exc).__name__,
                    )
            stopped.wait(POLL_SECONDS)

    thread = Thread(target=run, name="mia-crm-delivery", daemon=True)
    thread.start()
    return stopped, thread
