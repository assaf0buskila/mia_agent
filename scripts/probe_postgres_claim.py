"""Verify production claim semantics with a synthetic row that is always rolled back.

Run inside the release container using python -c and this file's contents. No
notification is sent. Only outcome booleans are printed; credentials stay injected.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from uuid import uuid4

from app.db.models import OwnerNotificationRecipientClaimRow
from app.db.session import get_session_factory
from app.db.store import LeadStore
from sqlalchemy import func, select


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        with get_session_factory()() as db:
            if db.get_bind().dialect.name != "postgresql":
                raise ValueError("PostgreSQL required")
            probe_id = "probe:" + uuid4().hex
            table = OwnerNotificationRecipientClaimRow.__table__
            count = select(func.count()).select_from(table).where(table.c.lead_id == probe_id)
            args = dict(
                kind="release_claim_probe",
                lead_id=probe_id,
                notification_key="isolated-rollback-probe",
                recipient_id="0",
                claimed_at=datetime.now(UTC).isoformat(),
            )
            store = LeadStore(db)
            try:
                first = store.try_claim_owner_notification_recipient(**args)
                duplicate = store.try_claim_owner_notification_recipient(**args)
                visible = db.scalar(count) == 1
            finally:
                db.rollback()
            rolled_back = db.scalar(count) == 0
            passed = first and not duplicate and visible and rolled_back
            print(
                json.dumps(
                    {
                        "result": "PASS" if passed else "FAIL",
                        "first_claim": first,
                        "duplicate_claim": duplicate,
                        "insert_visible": visible,
                        "rolled_back": rolled_back,
                    }
                )
            )
            return 0 if passed else 1
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
