"""Durable internal reminder delivery with no network or real Telegram sends."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models import OwnerNotificationRecipientClaimRow
from app.db.reminders import OwnerReminderRow
from app.services.notifications import OwnerTelegramDelivery
from app.services.reminders import ReminderDispatching, ReminderService
from app.workers.reminders import ReminderWorker, reminder_keyboard, start_reminder_runtime
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import sessionmaker

NOW = datetime(2026, 10, 11, 8, 0, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "postgres"])
def factory(tmp_path, request):
    admin = None
    schema = "reminder_worker_" + uuid4().hex
    if request.param == "postgres":
        url = os.environ.get("MIA_TEST_POSTGRES_URL", "")
        if not url:
            pytest.skip("MIA_TEST_POSTGRES_URL required for local PostgreSQL proof")
        admin = create_engine(url)
        with admin.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    else:
        engine = create_engine(
            f"sqlite:///{tmp_path / 'reminders.db'}",
            connect_args={"check_same_thread": False, "timeout": 20},
        )
    # The PostgreSQL schema is fresh. Do not let checkfirst see identically named
    # migrated public tables through search_path and silently reuse their data.
    Base.metadata.create_all(engine, checkfirst=request.param != "postgres")
    if request.param == "postgres":
        with engine.connect() as conn:
            for table in ("owner_reminders", "owner_notification_recipient_claims"):
                actual_schema = conn.scalar(
                    text(
                        "SELECT n.nspname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE c.oid = to_regclass(:table)"
                    ),
                    {"table": table},
                )
                assert actual_schema == schema, "worker fixture reused public data"
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()
    if admin is not None:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def settings(**kwargs):
    return Settings(
        _env_file=None,
        reminders_enabled=True,
        reminder_delivery_enabled=True,
        telegram_owner_user_ids="123",
        telegram_bot_token="test-token",
        **kwargs,
    )


def seed(factory, *, due=NOW, owner="123", delivery_state="pending", updated_at=NOW):
    reminder_id = uuid4().hex
    with factory() as db:
        db.add(
            OwnerReminderRow(
                id=reminder_id,
                owner_id=owner,
                source_event_id=uuid4().hex,
                text="לחזור לדני",
                scheduled_at=due,
                timezone="Asia/Jerusalem",
                status="active",
                revision=1,
                delivery_state=delivery_state,
                created_at=NOW,
                updated_at=updated_at,
            )
        )
        db.commit()
    return reminder_id


def state(factory, reminder_id):
    with factory() as db:
        return db.get(OwnerReminderRow, reminder_id).delivery_state


def test_due_only_and_receipt_committed_before_send(factory):
    due = seed(factory)
    seed(factory, due=NOW + timedelta(minutes=1))
    seed(factory, owner="999")
    calls = []

    def deliver(**kwargs):
        with factory() as db:
            assert db.get(OwnerReminderRow, due).delivery_state == "dispatching"
            receipt = db.get(
                OwnerNotificationRecipientClaimRow, ("internal_reminder", due, "1", "123")
            )
            assert receipt.delivery_status == "pending"
        assert kwargs["recipient_ids"] == ("123",)
        assert len(kwargs["reply_markup"]["inline_keyboard"][0]) == 2
        calls.append(kwargs)
        return OwnerTelegramDelivery(delivered=("123",))

    worker = ReminderWorker(factory, settings(), deliver=deliver)
    assert worker.run_once(now=NOW) == 1
    assert worker.run_once(now=NOW) == 0
    assert state(factory, due) == "accepted"
    assert len(calls) == 1


def test_rejection_cooldown_is_revalidated_inside_dispatch_cas(factory, monkeypatch):
    reminder_id = seed(factory)
    original_get = factory.class_.get
    interleaved = []

    def get_then_other_worker_rejects(db, entity, ident, **kwargs):
        result = original_get(db, entity, ident, **kwargs)
        if entity is OwnerReminderRow and not interleaved:
            interleaved.append(True)
            with factory() as competing:
                competing.execute(
                    update(OwnerReminderRow)
                    .where(OwnerReminderRow.id == reminder_id)
                    .values(delivery_state="rejected", updated_at=NOW)
                )
                competing.commit()
        return result

    monkeypatch.setattr(factory.class_, "get", get_then_other_worker_rejects)
    worker = ReminderWorker(
        factory, settings(), deliver=lambda **_: pytest.fail("fresh rejection retried")
    )
    assert worker.run_once(now=NOW) == 0
    assert interleaved
    with factory() as db:
        assert db.get(OwnerReminderRow, reminder_id).delivery_state == "rejected"
        assert list(db.scalars(select(OwnerNotificationRecipientClaimRow))) == []


def test_concurrent_workers_send_once(factory):
    reminder_id = seed(factory)
    calls = []

    def deliver(**kwargs):
        calls.append(kwargs)
        return OwnerTelegramDelivery(delivered=("123",))

    workers = [ReminderWorker(factory, settings(), deliver=deliver) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda worker: worker.run_once(now=NOW), workers))
    assert sum(results) == 1
    assert len(calls) == 1
    assert state(factory, reminder_id) == "accepted"


@pytest.mark.parametrize("outcome", ["ambiguous", "raises"])
def test_lost_acknowledgement_not_resent_after_restart(factory, outcome):
    reminder_id = seed(factory)
    calls = []

    def deliver(**kwargs):
        calls.append(kwargs)
        if outcome == "raises":
            raise TimeoutError("lost acknowledgement")
        return OwnerTelegramDelivery(ambiguous=("123",))

    assert ReminderWorker(factory, settings(), deliver=deliver).run_once(now=NOW) == 0
    assert state(factory, reminder_id) == "unknown"
    assert (
        ReminderWorker(factory, settings(), deliver=deliver).run_once(now=NOW + timedelta(days=1))
        == 0
    )
    assert len(calls) == 1
    with factory() as db:
        assert (
            db.get(
                OwnerNotificationRecipientClaimRow, ("internal_reminder", reminder_id, "1", "123")
            )
            is not None
        )


def test_crash_after_claim_becomes_unknown_without_resend(factory):
    reminder_id = seed(factory, delivery_state="dispatching", updated_at=NOW - timedelta(minutes=2))
    with factory() as db:
        db.add(
            OwnerNotificationRecipientClaimRow(
                kind="internal_reminder",
                lead_id=reminder_id,
                notification_key="1",
                recipient_id="123",
                delivery_status="pending",
                claimed_at=NOW.isoformat(),
            )
        )
        db.commit()
    worker = ReminderWorker(factory, settings(), deliver=lambda **_: pytest.fail("blind resend"))
    assert worker.run_once(now=NOW) == 0
    assert state(factory, reminder_id) == "unknown"


def test_known_rejection_retries_after_cooldown(factory):
    reminder_id = seed(factory)
    calls = []

    def deliver(**kwargs):
        calls.append(kwargs)
        return (
            OwnerTelegramDelivery(rejected=("123",))
            if len(calls) == 1
            else OwnerTelegramDelivery(delivered=("123",))
        )

    worker = ReminderWorker(factory, settings(), deliver=deliver)
    assert worker.run_once(now=NOW) == 0
    assert state(factory, reminder_id) == "rejected"
    assert worker.run_once(now=NOW + timedelta(seconds=30)) == 0
    assert worker.run_once(now=NOW + timedelta(minutes=1)) == 1
    assert len(calls) == 2


def test_downtime_recovery_labels_late_delivery(factory):
    seed(factory, due=NOW - timedelta(days=2))
    calls = []

    def deliver(**kwargs):
        calls.append(kwargs)
        return OwnerTelegramDelivery(delivered=("123",))

    assert ReminderWorker(factory, settings(), deliver=deliver).run_once(now=NOW) == 1
    assert "2880 דקות" in calls[0]["text"]
    assert "09/10/2026 11:00" in calls[0]["text"]


@pytest.mark.parametrize("option", ["kill_switch", "demo_mode"])
def test_safeguards_prevent_claim_and_send(factory, option):
    seed(factory)
    worker = ReminderWorker(
        factory, settings(**{option: True}), deliver=lambda **_: pytest.fail("send while paused")
    )
    assert worker.run_once(now=NOW) == 0
    with factory() as db:
        assert list(db.scalars(select(OwnerNotificationRecipientClaimRow))) == []


def test_default_runtime_disabled(factory):
    assert start_reminder_runtime(Settings(_env_file=None), factory) is None


def test_enabled_runtime_recovers_cycle_failure_and_stops(monkeypatch):
    recovered = Event()
    calls = []

    def run_once(self):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("test cycle failure")
        recovered.set()
        return 0

    monkeypatch.setattr(ReminderWorker, "run_once", run_once)
    monkeypatch.setattr("app.workers.reminders.POLL_SECONDS", 0.01)
    stopped, thread = start_reminder_runtime(settings(), lambda: None)
    try:
        assert recovered.wait(2), "runtime did not recover from a cycle failure"
        assert thread.is_alive()
    finally:
        stopped.set()
        thread.join(2)
    assert not thread.is_alive()


def test_dispatch_race_rejects_owner_mutation(factory):
    reminder_id = seed(factory)

    def deliver(**kwargs):
        with factory() as db:
            for action in ("complete", "cancel", "snooze"):
                with pytest.raises(ReminderDispatching):
                    ReminderService(db).change(
                        owner_id="123",
                        reminder_id=reminder_id,
                        action=action,
                        expected_revision=1,
                        now=NOW,
                        scheduled_at=NOW + timedelta(minutes=15) if action == "snooze" else None,
                    )
        return OwnerTelegramDelivery(delivered=("123",))

    assert ReminderWorker(factory, settings(), deliver=deliver).run_once(now=NOW) == 1


def test_contact_button_is_readonly_callback_not_customer_link(factory):
    reminder_id = seed(factory)
    with factory() as db:
        row = db.get(OwnerReminderRow, reminder_id)
        row.contact_id = "crm-id"
        keyboard = reminder_keyboard(row)
        button = keyboard["inline_keyboard"][1][0]
        assert button["callback_data"] == f"rm:contact:{reminder_id}:1"
        assert "url" not in button


def test_failed_claim_commit_never_sends(factory, monkeypatch):
    from app.db.store import LeadStore

    reminder_id = seed(factory)

    def fail_commit(store):
        store.session.rollback()
        return False

    monkeypatch.setattr(LeadStore, "commit_owner_notification_delivery_state", fail_commit)
    worker = ReminderWorker(
        factory, settings(), deliver=lambda **_: pytest.fail("uncommitted send")
    )
    assert worker.run_once(now=NOW) == 0
    assert state(factory, reminder_id) == "pending"
    with factory() as db:
        assert list(db.scalars(select(OwnerNotificationRecipientClaimRow))) == []


def test_failed_acceptance_persistence_retains_ambiguous_claim(factory, monkeypatch):
    from app.db.store import LeadStore

    reminder_id = seed(factory)
    calls = []

    def deliver(**kwargs):
        calls.append(kwargs)
        return OwnerTelegramDelivery(delivered=("123",))

    monkeypatch.setattr(
        LeadStore,
        "record_owner_notification_recipient_delivery_outcomes_durably",
        lambda *_, **__: False,
    )
    worker = ReminderWorker(factory, settings(), deliver=deliver)
    assert worker.run_once(now=NOW) == 1
    assert state(factory, reminder_id) == "unknown"
    assert worker.run_once(now=NOW + timedelta(days=1)) == 0
    assert len(calls) == 1


@pytest.mark.parametrize("status", [408, 500, 502, 503])
def test_reminder_gateway_error_is_uncertain_and_legacy_semantics_retained(status):
    import httpx
    from app.services.notifications import _telegram_delivery_status

    response = httpx.Response(status, json={"ok": False})
    assert _telegram_delivery_status(response, conservative_errors=True) is None
    assert _telegram_delivery_status(response) is False


def test_backlog_clock_does_not_expire_live_dispatch(factory):
    first = seed(factory, due=NOW - timedelta(minutes=2))
    second = seed(factory, due=NOW - timedelta(minutes=1))
    moment = [NOW]
    calls = []

    def deliver(**kwargs):
        reminder_id = kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[2]
        calls.append(reminder_id)
        if reminder_id == first:
            moment[0] += timedelta(minutes=2)
        else:
            assert reminder_id == second
            with factory() as db:
                stored = db.get(OwnerReminderRow, second)
                assert stored.updated_at.replace(tzinfo=UTC) == moment[0]
            other = ReminderWorker(
                factory, settings(), deliver=lambda **_: pytest.fail("live retry")
            )
            assert other.run_once(now=moment[0] + timedelta(seconds=1)) == 0
            assert state(factory, second) == "dispatching"
            with factory() as db:
                with pytest.raises(ReminderDispatching):
                    ReminderService(db).change(
                        owner_id="123",
                        reminder_id=second,
                        action="cancel",
                        expected_revision=1,
                        now=moment[0],
                    )
        return OwnerTelegramDelivery(delivered=("123",))

    worker = ReminderWorker(factory, settings(), deliver=deliver, clock=lambda: moment[0])
    assert worker.run_once() == 2
    assert calls == [first, second]


def test_rejected_backlog_does_not_starve_new_pending_reminder(factory):
    for _ in range(100):
        seed(factory, due=NOW - timedelta(days=1), delivery_state="rejected", updated_at=NOW)
    pending = seed(factory)
    calls = []

    def deliver(**kwargs):
        calls.append(kwargs)
        return OwnerTelegramDelivery(delivered=("123",))

    assert ReminderWorker(factory, settings(), deliver=deliver).run_once(now=NOW) == 1
    assert len(calls) == 1 and state(factory, pending) == "accepted"
