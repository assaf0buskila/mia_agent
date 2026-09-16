import inspect
import json
import os
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from app.core.capabilities import CapabilityId, require_alive
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.commitments import (
    ACTION_FOLLOW_UP,
    CONDITION_NONE,
    TRIGGER_DUE_DATE,
)
from app.domain.followups import follow_up_due_on
from app.workers import due_scan as due_scan_module
from app.workers.due_scan import main, run_due_scan
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

OWNER_EVENT_ID = "evt.owner.scan.worker.due"
OWNER_EXTERNAL_ID = "972509994404"
_FIXED_NOW = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))


def _seed_owner_task(
    store: LeadStore,
    *,
    provider_event_id: str,
    due_at: str,
    status: str = "logged",
    trigger: str = TRIGGER_DUE_DATE,
    condition: str = CONDITION_NONE,
) -> None:
    store.save_owner_task(
        provider="telegram",
        provider_event_id=provider_event_id,
        channel="telegram",
        external_id=OWNER_EXTERNAL_ID,
        task_type="sales",
        status=status,
        due_at=due_at,
        trigger=trigger,
        condition=condition,
        action=ACTION_FOLLOW_UP,
    )


def test_run_due_scan_marks_only_explicit_owner_task_due() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        due_at = follow_up_due_on(
            now=_FIXED_NOW, timezone=settings.calendar_timezone, offset_days=0
        )
        _seed_owner_task(store, provider_event_id=OWNER_EVENT_ID, due_at=due_at)
        db.commit()
        summary = run_due_scan(
            store,
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=_FIXED_NOW,
        )
        assert summary.owner_tasks_scanned >= 1
        assert summary.owner_tasks_due_ready >= 1
        owner_row = store.get_owner_task(provider="telegram", provider_event_id=OWNER_EVENT_ID)
        assert owner_row is not None
        assert owner_row.due_ready is True
        assert owner_row.block_reason == "due_pending"
    finally:
        db.close()


def test_run_due_scan_calls_owner_task_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    db = get_session_factory()()
    calls: dict[str, object] = {}

    def fake_owner_tasks(store, *, timezone, now=None):
        calls["owner_tasks"] = {
            "timezone": timezone,
            "now": now,
        }
        return []

    monkeypatch.setattr(due_scan_module, "scan_due_owner_tasks", fake_owner_tasks)
    try:
        store = LeadStore(db)
        summary = run_due_scan(
            store,
            timezone="Asia/Jerusalem",
            kill_switch=True,
            now=_FIXED_NOW,
        )
        assert summary.model_dump() == {
            "owner_tasks_scanned": 0,
            "owner_tasks_due_ready": 0,
            "owner_reminders_sent": 0,
        }
        assert calls["owner_tasks"] == {
            "timezone": "Asia/Jerusalem",
            "now": _FIXED_NOW,
        }
    finally:
        db.close()


def test_run_due_scan_never_imports_message_port() -> None:
    source = inspect.getsource(due_scan_module)
    assert "MessagePort" not in source
    assert "app.integrations.base" not in source


def test_due_scan_sends_one_unprompted_owner_reminder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    class Delivery:
        delivered = ("111",)
        rejected = ()
        confirmed_failure = False

    def fake_send(*, text: str, settings, transport=None, recipient_ids=None) -> Delivery:
        del settings, transport, recipient_ids
        sent.append(text)
        return Delivery()

    monkeypatch.setattr(due_scan_module, "deliver_owner_telegram", fake_send)
    monkeypatch.setattr(
        due_scan_module,
        "get_settings",
        lambda: Settings(telegram_bot_token="tok", telegram_owner_user_ids="111"),
    )
    reminder_now = datetime(2026, 8, 22, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        due_at = follow_up_due_on(now=reminder_now, timezone="Asia/Jerusalem", offset_days=0)
        _seed_owner_task(store, provider_event_id="evt.owner.scan.worker.remind", due_at=due_at)
        db.commit()
        first = run_due_scan(
            store,
            timezone="Asia/Jerusalem",
            kill_switch=False,
            now=reminder_now,
        )
        assert first.owner_tasks_due_ready >= 1
        assert first.owner_reminders_sent == 1
        # C9: due-reminder text now passes through owner_text() at egress, which
        # isolates the LTR digit run so it does not reorder inside the Hebrew
        # sentence -- the visible digits are unchanged. The expected isolation
        # is spelled out with literal FSI/PDI escapes rather than a second call
        # to owner_text, so a revert of the due_scan.py adoption diff (back to
        # a bare `text=text`) actually fails this instead of trivially matching.
        assert sent == [
            f"יש ⁨{first.owner_tasks_due_ready}⁩ משימות שמחכות לטיפול."
        ]
        second = run_due_scan(
            store,
            timezone="Asia/Jerusalem",
            kill_switch=False,
            now=reminder_now,
        )
        assert second.owner_reminders_sent == 0
        assert len(sent) == 1
    finally:
        db.close()


def test_due_reminder_retries_only_the_known_rejected_owner(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    class Delivery:
        def __init__(self, delivered: tuple[str, ...], rejected: tuple[str, ...]) -> None:
            self.delivered = delivered
            self.rejected = rejected

    def fake_send(*, recipient_ids=None, **kwargs) -> Delivery:
        del kwargs
        ids = tuple(recipient_ids or ())
        calls.append(ids)
        if ids == ("111", "222"):
            return Delivery(("111",), ("222",))
        return Delivery(ids, ())

    monkeypatch.setattr(due_scan_module, "deliver_owner_telegram", fake_send)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111,222")
        now = datetime(2026, 8, 23, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        assert (
            due_scan_module.maybe_notify_due_owner_tasks(
                store, due_ready=2, settings=settings, kill_switch=False, now=now
            )
            == 1
        )
        assert (
            due_scan_module.maybe_notify_due_owner_tasks(
                store, due_ready=2, settings=settings, kill_switch=False, now=now
            )
            == 1
        )
        assert calls == [("111", "222"), ("222",)]
    finally:
        db.close()


@pytest.fixture
def postgres_sessions():
    url = os.environ.get("MIA_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("MIA_TEST_POSTGRES_URL required for durable due reminder claim proof")
    schema = "mia_due_scan_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url,
        connect_args={"options": f"-csearch_path={schema} -clock_timeout=2000"},
    )
    try:
        Base.metadata.create_all(engine)
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_due_reminder_claim_survives_outer_rollback_after_accepted_send(
    postgres_sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    class Delivery:
        delivered = ("123",)
        rejected = ()
        ambiguous = ()

    def fake_send(*, recipient_ids=None, **kwargs) -> Delivery:
        del kwargs
        calls.append(tuple(recipient_ids or ()))
        return Delivery()

    monkeypatch.setattr(due_scan_module, "deliver_owner_telegram", fake_send)
    settings = Settings(telegram_bot_token="fake", telegram_owner_user_ids="123")
    now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

    with postgres_sessions() as first_db:
        first = due_scan_module.maybe_notify_due_owner_tasks(
            LeadStore(first_db),
            due_ready=1,
            settings=settings,
            kill_switch=False,
            now=now,
        )
        assert first == 1
        # Simulate a caller rolling back unrelated work after Telegram accepted.
        first_db.rollback()

    with postgres_sessions() as second_db:
        second = due_scan_module.maybe_notify_due_owner_tasks(
            LeadStore(second_db),
            due_ready=1,
            settings=settings,
            kill_switch=False,
            now=now,
        )
        assert second == 0
        second_db.rollback()

    assert calls == [("123",)]


def test_legacy_same_day_due_claim_does_not_resend_but_old_day_does(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    class Delivery:
        delivered = ("111",)
        rejected = ()

    def fake_send(*, recipient_ids=None, **kwargs) -> Delivery:
        del kwargs
        calls.append(tuple(recipient_ids or ()))
        return Delivery()

    monkeypatch.setattr(due_scan_module, "deliver_owner_telegram", fake_send)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111")
        today = datetime(2026, 8, 24, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        assert store.try_claim_owner_notification(
            kind=due_scan_module.KIND_DUE_REMINDER,
            lead_id="owner_due",
            claimed_at="2026-08-24T08:00:00+00:00",
        )
        db.commit()

        assert (
            due_scan_module.maybe_notify_due_owner_tasks(
                store, due_ready=2, settings=settings, kill_switch=False, now=today
            )
            == 0
        )
        assert calls == []

        store.release_owner_notification_claim(
            kind=due_scan_module.KIND_DUE_REMINDER, lead_id="owner_due"
        )
        db.commit()
        assert store.try_claim_owner_notification(
            kind=due_scan_module.KIND_DUE_REMINDER,
            lead_id="owner_due",
            claimed_at="2026-08-23T08:00:00+00:00",
        )
        db.commit()

        assert (
            due_scan_module.maybe_notify_due_owner_tasks(
                store, due_ready=2, settings=settings, kill_switch=False, now=today
            )
            == 1
        )
        assert calls == [("111",)]
    finally:
        db.close()


def test_due_scan_kill_switch_skips_owner_reminder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*, text: str, settings, transport=None):
        del text, settings, transport
        raise AssertionError("kill switch must not ping the owner")

    monkeypatch.setattr(due_scan_module, "deliver_owner_telegram", boom)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        due_at = follow_up_due_on(now=_FIXED_NOW, timezone="Asia/Jerusalem", offset_days=0)
        _seed_owner_task(store, provider_event_id="evt.owner.scan.worker.killed", due_at=due_at)
        db.commit()
        summary = run_due_scan(
            store,
            timezone="Asia/Jerusalem",
            kill_switch=True,
            now=_FIXED_NOW,
        )
        assert summary.owner_reminders_sent == 0
    finally:
        db.close()


def test_require_alive_due_scan() -> None:
    require_alive(CapabilityId.DUE_SCAN)


def test_main_stdout_counts_only(capsys: pytest.CaptureFixture[str]) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        due_at = follow_up_due_on(
            now=datetime.now(UTC),
            timezone=settings.calendar_timezone,
            offset_days=0,
        )
        _seed_owner_task(
            store,
            provider_event_id="evt.owner.scan.worker.main",
            due_at=due_at,
        )
        db.commit()
    finally:
        db.close()
    main()
    captured = capsys.readouterr()
    body = json.loads(captured.out.strip())
    assert set(body.keys()) == {
        "owner_tasks_scanned",
        "owner_tasks_due_ready",
        "owner_reminders_sent",
    }
    for value in body.values():
        assert isinstance(value, int)
    assert "lead_" not in captured.out
    assert "draft" not in captured.out
