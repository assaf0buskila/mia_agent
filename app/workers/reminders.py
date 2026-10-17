"""Minute-level internal reminders using the existing owner receipt ledger."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.demo import demo_mode_active
from app.db.reminders import OwnerReminderRow
from app.db.store import LeadStore
from app.services.notifications import deliver_owner_telegram

KIND = "internal_reminder"
POLL_SECONDS = 10
DISPATCH_STALE_SECONDS = 60
_LOG = logging.getLogger(__name__)


def utc(value: datetime) -> datetime:
    # SQLite strips timezone metadata in local tests; production uses TIMESTAMPTZ.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def reminder_keyboard(row) -> dict:
    token = f"{row.id}:{row.revision}"
    buttons = [
        {"text": "בוצע", "callback_data": f"rm:complete:{token}"},
        {"text": "דחה ב־15 דקות", "callback_data": f"rm:snooze:{token}"},
    ]
    keyboard = [buttons]
    if row.contact_id:
        # Opens a read-only contact card in this private bot chat, never a customer URL.
        keyboard.append([{"text": "פתח איש קשר", "callback_data": f"rm:contact:{token}"}])
    return {"inline_keyboard": keyboard}


class ReminderWorker:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        settings: Settings,
        *,
        deliver=deliver_owner_telegram,
        clock: Callable[[], datetime] | None = None,
    ):
        self.session_factory = session_factory
        self.settings = settings
        self.deliver = deliver
        self.clock = clock or (lambda: datetime.now(UTC))

    def run_once(self, *, now: datetime | None = None) -> int:
        clock = self.clock if now is None else lambda: now
        now = utc(clock())
        settings = self.settings
        if (
            not settings.reminders_enabled
            or not settings.reminder_delivery_enabled
            or settings.kill_switch
            or demo_mode_active(settings)
            or not settings.telegram_bot_token.strip()
        ):
            return 0
        owners = settings.telegram_owner_user_id_set()
        with self.session_factory() as db:
            # A crashed dispatch is uncertain even if the crash preceded the HTTP call.
            # Retain its receipt forever; restart must never blindly resend it.
            db.execute(
                update(OwnerReminderRow)
                .where(
                    OwnerReminderRow.delivery_state == "dispatching",
                    OwnerReminderRow.updated_at <= now - timedelta(seconds=DISPATCH_STALE_SECONDS),
                )
                .values(delivery_state="unknown")
            )
            db.commit()
            candidates = list(
                db.execute(
                    select(OwnerReminderRow.id, OwnerReminderRow.revision)
                    .where(
                        OwnerReminderRow.status == "active",
                        OwnerReminderRow.owner_id.in_(owners),
                        OwnerReminderRow.scheduled_at <= now,
                        or_(
                            OwnerReminderRow.delivery_state == "pending",
                            and_(
                                OwnerReminderRow.delivery_state == "rejected",
                                OwnerReminderRow.updated_at <= now - timedelta(minutes=1),
                            ),
                        ),
                    )
                    .order_by(
                        case((OwnerReminderRow.delivery_state == "pending", 0), else_=1),
                        OwnerReminderRow.scheduled_at,
                        OwnerReminderRow.id,
                    )
                    .limit(100)
                )
            )
        sent = 0
        for reminder_id, revision in candidates:
            sent += self._dispatch(reminder_id, revision, utc(clock()), clock=clock)
        return sent

    def _dispatch(self, reminder_id: str, revision: int, now: datetime, *, clock=None) -> int:
        with self.session_factory() as db:
            row = db.get(OwnerReminderRow, reminder_id)
            if row is None:
                return 0
            if row.delivery_state == "rejected" and utc(row.updated_at) > now - timedelta(
                minutes=1
            ):
                return 0
            # CAS serializes workers with owner cancel/complete/reschedule operations.
            won = db.execute(
                update(OwnerReminderRow)
                .where(
                    OwnerReminderRow.id == reminder_id,
                    OwnerReminderRow.revision == revision,
                    OwnerReminderRow.status == "active",
                    OwnerReminderRow.owner_id.in_(self.settings.telegram_owner_user_id_set()),
                    or_(
                        OwnerReminderRow.delivery_state == "pending",
                        and_(
                            OwnerReminderRow.delivery_state == "rejected",
                            OwnerReminderRow.updated_at <= now - timedelta(minutes=1),
                        ),
                    ),
                    OwnerReminderRow.scheduled_at <= now,
                )
                .values(delivery_state="dispatching", updated_at=now)
                .returning(OwnerReminderRow.id)
                .execution_options(synchronize_session="fetch")
            ).first()
            if won is None:
                db.rollback()
                return 0
            db.refresh(row)
            owner_id = row.owner_id
            store = LeadStore(db)
            claimed = store.try_claim_owner_notification_recipient(
                kind=KIND,
                lead_id=reminder_id,
                notification_key=str(revision),
                recipient_id=owner_id,
                claimed_at=now.isoformat(),
            )
            if not claimed:
                row.delivery_state = "unknown"
                db.commit()
                return 0
            scheduled = utc(row.scheduled_at)
            local = scheduled.astimezone(ZoneInfo(row.timezone)).strftime("%d/%m/%Y %H:%M")
            late = max(0, int((now - scheduled).total_seconds() // 60))
            text = f"תזכורת פנימית · {local}\n{row.text}"
            if late:
                text += f"\nנמסרת באיחור של {late} דקות."
            markup = reminder_keyboard(row)
            # Both the occurrence and recipient claim become durable BEFORE the network.
            if not store.commit_owner_notification_delivery_state():
                return 0
            try:
                delivery = self.deliver(
                    text=text,
                    settings=self.settings,
                    recipient_ids=(owner_id,),
                    reply_markup=markup,
                    conservative_errors=True,
                )
            except Exception:  # noqa: BLE001 - never retry an uncertain provider outcome
                delivery = None
            accepted = delivery is not None and owner_id in delivery.delivered
            rejected = delivery is not None and (
                owner_id in delivery.rejected or delivery.no_attempt
            )
            if delivery is not None:
                saved = store.record_owner_notification_recipient_delivery_outcomes_durably(
                    kind=KIND,
                    lead_id=reminder_id,
                    notification_key=str(revision),
                    delivered_recipient_ids=delivery.delivered,
                    rejected_recipient_ids=(owner_id,) if rejected else (),
                )
            else:
                saved = True
            state = (
                "accepted"
                if accepted and saved
                else "rejected"
                if rejected and saved
                else "unknown"
            )
            db.execute(
                update(OwnerReminderRow)
                .where(
                    OwnerReminderRow.id == reminder_id,
                    OwnerReminderRow.revision == revision,
                    OwnerReminderRow.delivery_state == "dispatching",
                )
                .values(delivery_state=state, updated_at=utc(clock()) if clock else now)
            )
            db.commit()
            return int(accepted)


def start_reminder_runtime(settings: Settings, session_factory) -> tuple[Event, Thread] | None:
    if not settings.reminders_enabled or not settings.reminder_delivery_enabled:
        return None
    stopped = Event()
    worker = ReminderWorker(session_factory, settings)

    def run():
        while not stopped.is_set():
            try:
                worker.run_once()
            except Exception as exc:  # noqa: BLE001 - durable rows survive cycle failure
                _LOG.warning("reminder_cycle_failed error=%s", type(exc).__name__)
            stopped.wait(POLL_SECONDS)

    thread = Thread(target=run, name="mia-reminders", daemon=True)
    thread.start()
    return stopped, thread
