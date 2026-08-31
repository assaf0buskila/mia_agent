"""Persist prospect send_ready/due_ready; one unprompted owner Telegram due reminder.

Prospect follow-ups stay persist-only (never customer-send). When owner tasks are
due_ready, Mia pings the allowlisted Telegram owner once per local day.
"""

import json
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from app.agents.client.graph import finalize_inactive_website_conversations
from app.capabilities.types import Principal
from app.core.config import Settings, get_settings
from app.core.demo import demo_mode_active
from app.core.logging import configure_logging
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.commitments import scan_due_owner_tasks
from app.domain.followups import follow_up_due_on, scan_due_follow_ups
from app.integrations.sheets import maintain_crm_workspace
from app.services.notifications import deliver_owner_telegram

logger = logging.getLogger(__name__)

KIND_DUE_REMINDER = "due_owner_tasks"
_DUE_REMINDER_LEAD = "owner_due"


def _legacy_due_claim_matches_day(*, claimed_at: str | None, day: str, timezone: str) -> bool:
    """Conservatively retain only a legacy reminder from this same local day.

    Legacy rows have no recipient and no daily notification key, so they cannot be
    backfilled.  A dated legacy row for today is treated as accepted-or-ambiguous;
    an older (or unparseable) row must not suppress all future daily reminders.
    """
    if not claimed_at or not day:
        return False
    try:
        when = datetime.fromisoformat(claimed_at)
        if when.tzinfo is None:
            return when.date().isoformat() == day
        return when.astimezone(ZoneInfo(timezone)).date().isoformat() == day
    except (TypeError, ValueError, OSError):
        return False


class DueScanSummary(BaseModel):
    follow_ups_scanned: int
    follow_ups_send_ready: int
    owner_tasks_scanned: int
    owner_tasks_due_ready: int
    website_conversations_finalized: int
    owner_reminders_sent: int


def maybe_notify_due_owner_tasks(
    store: LeadStore,
    *,
    due_ready: int,
    settings: Settings,
    kill_switch: bool,
    now: datetime,
) -> int:
    """One Telegram due reminder per local day. Kill switch and demo skip the ping."""
    if kill_switch or due_ready <= 0:
        return 0
    if demo_mode_active(settings):
        return 0
    try:
        day = follow_up_due_on(
            now=now, timezone=settings.calendar_timezone, offset_days=0
        )
    except (ValueError, OSError, KeyError):
        return 0
    if _legacy_due_claim_matches_day(
        claimed_at=store.owner_notification_claimed_at(
            kind=KIND_DUE_REMINDER, lead_id=_DUE_REMINDER_LEAD
        ),
        day=day,
        timezone=settings.calendar_timezone,
    ):
        return 0
    scheduled = now.replace(microsecond=0).isoformat()
    text = f"יש {due_ready} משימות שמחכות לטיפול."
    store.upsert_owner_notification(
        kind=KIND_DUE_REMINDER, lead_id=_DUE_REMINDER_LEAD, scheduled_at=scheduled
    )
    token = settings.telegram_bot_token.strip()
    recipients = tuple(sorted(settings.telegram_owner_user_id_set()))
    if not token or not recipients or not text.strip():
        return 0
    claimed_recipients = tuple(
        recipient_id
        for recipient_id in recipients
        if store.try_claim_owner_notification_recipient(
            kind=KIND_DUE_REMINDER,
            lead_id=_DUE_REMINDER_LEAD,
            notification_key=day,
            recipient_id=recipient_id,
            claimed_at=scheduled,
        )
    )
    if not claimed_recipients:
        return 0
    delivery = deliver_owner_telegram(
        text=text, settings=settings, recipient_ids=claimed_recipients
    )
    for recipient_id in delivery.rejected:
        store.release_owner_notification_recipient_claim(
            kind=KIND_DUE_REMINDER,
            lead_id=_DUE_REMINDER_LEAD,
            notification_key=day,
            recipient_id=recipient_id,
        )
    return 1 if delivery.delivered else 0


def run_due_scan(
    store: LeadStore,
    *,
    timezone: str,
    kill_switch: bool,
    now: datetime | None = None,
) -> DueScanSummary:
    effective_now = now or datetime.now(UTC)
    settings = get_settings()
    follow_up_results = scan_due_follow_ups(
        store,
        timezone=timezone,
        kill_switch=kill_switch,
        now=effective_now,
    )
    owner_task_results = scan_due_owner_tasks(
        store,
        timezone=timezone,
        now=effective_now,
    )
    website_finalized = finalize_inactive_website_conversations(
        store,
        settings=settings,
        principal=Principal.client(source="due_scan"),
        now=effective_now,
    )
    due_ready = sum(1 for item in owner_task_results if item.due_ready)
    owner_reminders_sent = maybe_notify_due_owner_tasks(
        store,
        due_ready=due_ready,
        settings=settings,
        kill_switch=kill_switch,
        now=effective_now,
    )
    return DueScanSummary(
        follow_ups_scanned=len(follow_up_results),
        follow_ups_send_ready=sum(1 for item in follow_up_results if item.allowed),
        owner_tasks_scanned=len(owner_task_results),
        owner_tasks_due_ready=due_ready,
        website_conversations_finalized=website_finalized,
        owner_reminders_sent=owner_reminders_sent,
    )


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    init_db()
    session = get_session_factory()()
    try:
        store = LeadStore(session)
        summary = run_due_scan(
            store,
            timezone=settings.calendar_timezone,
            kill_switch=settings.kill_switch,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    crm_workspace_status = maintain_crm_workspace(settings)
    counts = summary.model_dump()
    print(json.dumps(counts))
    logger.info(
        "due_scan complete follow_ups_scanned=%s follow_ups_send_ready=%s "
        "owner_tasks_scanned=%s owner_tasks_due_ready=%s "
        "website_conversations_finalized=%s owner_reminders_sent=%s",
        counts["follow_ups_scanned"],
        counts["follow_ups_send_ready"],
        counts["owner_tasks_scanned"],
        counts["owner_tasks_due_ready"],
        counts["website_conversations_finalized"],
        counts["owner_reminders_sent"],
    )
    logger.info("sheets CRM maintenance status=%s", crm_workspace_status)


if __name__ == "__main__":
    main()
