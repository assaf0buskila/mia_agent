from app.core.config import Settings
from app.db.base import Base
from app.db.session import make_engine
from app.workers.crm_runtime import start_crm_runtime, telegram_receipt_handler
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
