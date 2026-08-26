"""Persist prospect send_ready/due_ready; one unprompted owner Telegram due reminder.

Prospect follow-ups stay persist-only (never customer-send). When owner tasks are
due_ready, Mia pings the allowlisted Telegram owner once per local day.
"""

import json
import logging
from datetime import UTC, datetime

from pydantic import BaseModel

from app.agents.client.graph import finalize_inactive_website_conversations
from app.core.config import Settings, get_settings
from app.core.demo import demo_mode_active
from app.core.logging import configure_logging
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.commitments import scan_due_owner_tasks
from app.domain.followups import follow_up_due_on, scan_due_follow_ups
from app.services.notifications import send_owner_telegram

logger = logging.getLogger(__name__)

KIND_DUE_REMINDER = "due_owner_tasks"
_DUE_REMINDER_LEAD = "owner_due"


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
    inserted = store.try_insert_owner_notification(
        kind=KIND_DUE_REMINDER,
        lead_id=_DUE_REMINDER_LEAD,
        conversation_id=day,
        scheduled_at=now.replace(microsecond=0).isoformat(),
    )
    if not inserted:
        return 0
    text = f"יש {due_ready} משימות שמחכות לטיפול."
    return 1 if send_owner_telegram(text=text, settings=settings) else 0


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
        monthly_budget=None,
        spend_mtd=None,
    )
    website_finalized = finalize_inactive_website_conversations(
        store,
        settings=settings,
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


if __name__ == "__main__":
    main()
