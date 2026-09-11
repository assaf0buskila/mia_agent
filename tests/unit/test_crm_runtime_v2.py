import json
from datetime import UTC, datetime
from uuid import uuid4

from app.core.config import Settings
from app.db.base import Base
from app.db.models import CrmOutboxRow
from app.db.session import make_engine
from app.integrations.sheets import FakeSheetsPort
from app.workers.crm_delivery import CrmDeliveryWorker
from app.workers.crm_runtime import start_crm_runtime, telegram_receipt_handler
from sqlalchemy.exc import DataError
from sqlalchemy.orm import sessionmaker


def _runtime(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'receipts.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        telegram_owner_user_ids="123",
    )
    payload = {"conversation_id": "site-test", "recipient_id": "123", "text": "Test lead"}
    return engine, factory, settings, payload


def test_receipt_survives_handler_restart_and_never_resends(tmp_path):
    engine, factory, settings, payload = _runtime(tmp_path)
    sends = []
    try:
        handle = telegram_receipt_handler(
            factory, settings, transport=lambda *args: sends.append(args)
        )
        assert handle(payload, False) == "confirmed"
        restarted = telegram_receipt_handler(
            factory, settings, transport=lambda *args: sends.append(args)
        )
        assert restarted(payload, True) == "confirmed"
        assert restarted(payload, False) == "confirmed"
        assert len(sends) == 1
    finally:
        engine.dispose()


def test_ambiguous_receipt_blocks_retry_and_reconciliation_cannot_send(tmp_path):
    engine, factory, settings, payload = _runtime(tmp_path)
    attempts = []

    def uncertain(*args):
        attempts.append(args)
        raise TimeoutError("untrusted provider message")

    try:
        handle = telegram_receipt_handler(factory, settings, transport=uncertain)
        assert handle(payload, True) == "failed"
        assert not attempts
        assert handle(payload, False) == "unknown"
        assert handle(payload, True) == "unknown"
        assert handle(payload, False) == "unknown"
        assert len(attempts) == 1
    finally:
        engine.dispose()


def test_receipt_bridge_rechecks_owner_and_kill_switch(tmp_path):
    engine, factory, settings, payload = _runtime(tmp_path)
    attempts = []
    try:
        handle = telegram_receipt_handler(
            factory, settings, transport=lambda *args: attempts.append(args)
        )
        assert handle({**payload, "recipient_id": "999"}, False) == "conflict"
        settings.kill_switch = True
        assert handle(payload, False) == "failed"
        assert not attempts
        assert start_crm_runtime(settings, factory) is None
    finally:
        engine.dispose()


def test_a_db_error_in_one_job_does_not_wedge_every_job_behind_it(tmp_path):
    """Reproduces the production incident directly, not just its trigger.

    Two live-tested website leads never reached Telegram: `website_ping_scope`
    produced a `lead_id` one character over its column's limit, and
    `CrmDeliveryWorker._deliver` did not catch `SQLAlchemyError`. The DataError from
    the first landed job escaped `_deliver` uncaught, past `run_once`, aborting the
    whole cycle before that job could even be marked "failed" - so it, and every job
    behind it, stayed stuck forever, retried every 5 seconds, never delivered.

    This asserts the general contract - a destination handler raising any
    SQLAlchemyError must never escape run_once, and must never block a healthy job
    queued after it - independent of which specific field ever overflows again.
    """
    engine = make_engine(f"sqlite:///{tmp_path / 'wedge.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    now = datetime.now(UTC).isoformat()

    def poison_handler(payload, reconcile_only):
        raise DataError("insert", {}, Exception("value too long for type"))

    calls = []

    def healthy_handler(payload, reconcile_only):
        calls.append(payload)
        return "confirmed"

    handlers = {"poison": poison_handler, "healthy": healthy_handler}

    def dispatch(payload, reconcile_only):
        return handlers[payload["which"]](payload, reconcile_only)

    with factory() as db:
        for which in ("poison", "healthy"):
            db.add(
                CrmOutboxRow(
                    id=uuid4().hex,
                    dedupe_key=f"telegram:{which}",
                    aggregate_type="crm_contact",
                    aggregate_id=uuid4().hex,
                    destination="telegram",
                    payload_json=json.dumps({"which": which, "recipient_id": "999"}),
                    status="pending",
                    created_at=now,
                )
            )
        db.commit()

    worker = CrmDeliveryWorker(
        session_factory=factory,
        sheets=FakeSheetsPort(),
        telegram_handler=dispatch,
        allowed_telegram_recipient_ids=frozenset({"999"}),
    )
    try:
        run = worker.run_once()  # must not raise
        assert run.claimed == 2
        assert run.failed == 1
        assert run.confirmed == 1
        assert calls, "the healthy job behind the poison one must still be delivered"
        with factory() as db:
            statuses = {
                row.dedupe_key: row.status for row in db.query(CrmOutboxRow).all()
            }
        assert statuses == {"telegram:poison": "failed", "telegram:healthy": "confirmed"}
    finally:
        engine.dispose()
