"""Persist reconciliation findings; never repairs, sends, or consumes tokens."""

import json
import logging
import sys

from app.core.config import get_settings
from app.core.demo import demo_mode_active
from app.core.logging import configure_logging
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.reconciliation import inspect_open_findings, run_reconciliation

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    init_db()
    inspect_requested = "--inspect" in sys.argv
    session = get_session_factory()()
    inspect_rows = []
    try:
        store = LeadStore(session)
        summary = run_reconciliation(
            store,
            kill_switch=settings.kill_switch,
            demo_active=demo_mode_active(settings),
            handoff_send_enabled=settings.whatsapp_handoff_send,
        )
        if inspect_requested:
            inspect_rows = inspect_open_findings(store)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    counts = summary.model_dump()
    if inspect_requested:
        open_findings = [
            {
                "kind": row.kind,
                "subject_key": row.subject_key,
                "channel": row.channel,
                "envelope_kind": row.envelope_kind,
            }
            for row in inspect_rows
        ]
        print(
            json.dumps(
                {
                    **counts,
                    "open_count": len(open_findings),
                    "open_findings": open_findings,
                }
            )
        )
    else:
        print(json.dumps(counts))
    logger.info(
        "reconcile complete webhook_received=%s sent_without_out=%s handoff_expired=%s",
        counts["webhook_received"],
        counts["sent_without_out"],
        counts["handoff_expired"],
    )


if __name__ == "__main__":
    main()
