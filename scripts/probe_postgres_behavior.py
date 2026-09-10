"""Verify conflict-safe funnel inserts on PostgreSQL; always roll back probe data."""

import json
import logging
import sys
from uuid import uuid4

from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory
from app.db.store import LeadStore
from app.domain.events import build_behavior_event
from sqlalchemy import func, select


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        with get_session_factory()() as db:
            if db.get_bind().dialect.name != "postgresql":
                raise ValueError("PostgreSQL required")
            probe_id = "probe:" + uuid4().hex
            count = select(func.count()).select_from(CanonicalEventRow).where(
                CanonicalEventRow.conversation_id == probe_id
            )
            event = build_behavior_event(
                session_id=probe_id, lead_id="", payload={"kind": "website_conversion"}
            )
            store = LeadStore(db)
            try:
                store.save_canonical_event(provider="website", event=event)
                # Reproduce a lookup that missed an insert by a competing request.
                store.get_canonical_event = lambda **kwargs: None
                store.save_canonical_event(provider="website", event=event)
                one_event = db.scalar(count) == 1
            finally:
                db.rollback()
            rolled_back = db.scalar(count) == 0
            passed = one_event and rolled_back
            print(json.dumps({
                "result": "PASS" if passed else "FAIL",
                "one_event": one_event,
                "rolled_back": rolled_back,
            }))
            return 0 if passed else 1
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
