"""Persist send_ready/due_ready; never sends; never executes."""

import json
import logging
from datetime import UTC, datetime

from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.commitments import scan_due_owner_tasks
from app.domain.followups import scan_due_follow_ups
from app.domain.pacing import parse_monthly_budget
from app.services.finalization import scan_inactive_website_conversations

logger = logging.getLogger(__name__)


class DueScanSummary(BaseModel):
    follow_ups_scanned: int
    follow_ups_send_ready: int
    owner_tasks_scanned: int
    owner_tasks_due_ready: int
    website_conversations_finalized: int


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
    pacing_row = store.get_campaign_pacing()
    if pacing_row is None or pacing_row.spend == "":
        spend_mtd: float | None = None
    else:
        spend_mtd = parse_monthly_budget(pacing_row.spend)
    owner_task_results = scan_due_owner_tasks(
        store,
        timezone=timezone,
        now=effective_now,
        monthly_budget=parse_monthly_budget(settings.campaign_monthly_budget),
        spend_mtd=spend_mtd,
    )
    website_finalized = scan_inactive_website_conversations(
        store,
        settings=settings,
        now=effective_now,
    )
    return DueScanSummary(
        follow_ups_scanned=len(follow_up_results),
        follow_ups_send_ready=sum(1 for item in follow_up_results if item.allowed),
        owner_tasks_scanned=len(owner_task_results),
        owner_tasks_due_ready=sum(1 for item in owner_task_results if item.due_ready),
        website_conversations_finalized=website_finalized,
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
        "website_conversations_finalized=%s",
        counts["follow_ups_scanned"],
        counts["follow_ups_send_ready"],
        counts["owner_tasks_scanned"],
        counts["owner_tasks_due_ready"],
        counts["website_conversations_finalized"],
    )


if __name__ == "__main__":
    main()
