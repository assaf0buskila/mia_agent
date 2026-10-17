"""Owner-only reminder entry points; no models or external adapters are invoked."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.base import Base
from app.db.reminders import OwnerReminderRow
from app.db.store import LeadStore
from app.tools.owner.reminders import (
    _reminder_create,
    _reminder_list,
    _reminder_snapshot,
    handle_reminder_callback,
)
from app.tools.owner.reminders import (
    handle_reminder_message as _handle_reminder_message,
)
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

NOW = datetime(2026, 10, 11, 8, 0, tzinfo=UTC)


def handle_reminder_message(*, item, **kwargs):
    return _handle_reminder_message(
        item=item, principal=Principal.owner(source="telegram", actor_id=item["from"]), **kwargs
    )


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as session:
        yield session
    engine.dispose()


def settings():
    return Settings(_env_file=None, telegram_owner_user_ids="123", reminders_enabled=True)


def item(text, event="event-1", owner="123"):
    return {"id": event, "from": owner, "chat_id": owner, "text": text}


def test_exact_hebrew_creation_and_duplicate_event(db):
    text = "מיה, תזכירי לי מחר ב-11:30 לחזור לדני לגבי הצעת המחיר, ללא שיוך"
    first = handle_reminder_message(
        item=item(text), store=LeadStore(db), settings=settings(), now=NOW
    )
    assert "נשמרה" in first and "12/10/2026 11:30" in first
    assert "ללא שיוך" in first
    assert "נשמרה" in handle_reminder_message(
        item=item(text), store=LeadStore(db), settings=settings(), now=NOW
    )
    assert db.scalar(select(func.count()).select_from(OwnerReminderRow)) == 1


@pytest.mark.parametrize("text", ["Remind me tomorrow to call", "תזכירי לי מחר להתקשר"])
def test_missing_time_clarifies_without_persistence(db, text):
    reply = handle_reminder_message(
        item=item(text), store=LeadStore(db), settings=settings(), now=NOW
    )
    assert "שעה" in reply and "נשמרה" not in reply
    assert db.scalar(select(func.count()).select_from(OwnerReminderRow)) == 0


@pytest.mark.parametrize(
    "text",
    [
        "Remind me the day after tomorrow at 11:30",
        "Remind me two days before tomorrow at 11:30",
        "Remind me three days from tomorrow at 11:30",
        "תזכירי לי יום אחרי מחר ב-11:30",
        "תזכירי לי יום לפני מחר ב-11:30",
    ],
)
def test_compound_date_clarifies_without_persistence(db, text):
    reply = handle_reminder_message(
        item=item(text), store=LeadStore(db), settings=settings(), now=NOW
    )
    assert "נשמרה" not in reply
    assert db.scalar(select(func.count()).select_from(OwnerReminderRow)) == 0


def test_unauthorized_and_group_chat_cannot_create(db):
    text = "Remind me tomorrow at 11:30 to prepare invoice"
    result = handle_reminder_message(
        item=item(text, owner="999"), store=LeadStore(db), settings=settings(), now=NOW
    )
    assert "נשמרה" not in result
    group = item(text)
    group["chat_id"] = "-456"
    assert "פרטי" in handle_reminder_message(
        item=group, store=LeadStore(db), settings=settings(), now=NOW
    )
    assert db.scalar(select(func.count()).select_from(OwnerReminderRow)) == 0


def test_website_principal_denied_even_with_owner_numeric_id(db):
    ctx = SimpleNamespace(
        principal=Principal.client(source="website", actor_id="123"), settings=settings()
    )
    assert _reminder_create(ctx, {}).error == "unauthorized"


def test_reminder_read_tools_require_private_owner_chat(db):
    ctx = SimpleNamespace(
        principal=Principal.owner(source="telegram", actor_id="123"),
        settings=settings(),
        private_owner_chat=False,
    )
    assert _reminder_list(ctx, {}).error == "unauthorized"
    assert _reminder_snapshot(ctx, {}).error == "unauthorized"


def test_other_owner_and_group_callback_cannot_change_reminder(db):
    handle_reminder_message(
        item=item("Remind me tomorrow at 11:30 to prepare invoice"),
        store=LeadStore(db),
        settings=settings(),
        now=NOW,
    )
    row = db.scalars(select(OwnerReminderRow)).one()
    for callback in (
        {"from": "999", "chat_id": "999", "data": f"rm:complete:{row.id}:1"},
        {"from": "123", "chat_id": "-999", "data": f"rm:complete:{row.id}:1"},
    ):
        assert "מורשית" in handle_reminder_callback(
            callback=callback, store=LeadStore(db), settings=settings(), now=NOW
        )
    db.refresh(row)
    assert row.status == "active" and row.revision == 1


def test_negated_change_does_not_cancel(db):
    handle_reminder_message(
        item=item("Remind me tomorrow at 11:30 to prepare invoice"),
        store=LeadStore(db),
        settings=settings(),
        now=NOW,
    )
    row = db.scalars(select(OwnerReminderRow)).one()
    reply = handle_reminder_message(
        item=item(f"Do not cancel reminder {row.id} revision: 1", "e2"),
        store=LeadStore(db),
        settings=settings(),
        now=NOW,
    )
    assert "אינה הוראה" in reply
    db.refresh(row)
    assert row.status == "active"


def test_paging_exposes_active_reminders_after_first_twenty(db):
    from datetime import timedelta

    from app.services.reminders import ReminderService

    for index in range(25):
        ReminderService(db).create(
            owner_id="123",
            source_event_id=f"page-{index}",
            text=f"task {index}",
            scheduled_at=NOW + timedelta(days=1, minutes=index),
            now=NOW,
        )
    reply = handle_reminder_message(
        item=item("my reminders page 3"), store=LeadStore(db), settings=settings(), now=NOW
    )
    assert "task 24" in reply and "task 0" not in reply


def test_email_contact_verb_links_canonical_contact(db):
    import json

    from app.db.models import CrmContactRow

    db.add(CrmContactRow(id="contact-danny", fields_json=json.dumps({"name": "Danny"})))
    db.commit()
    reply = handle_reminder_message(
        item=item("Remind me tomorrow at 11:30 to email Danny"),
        store=LeadStore(db),
        settings=settings(),
        now=NOW,
    )
    assert "contact-danny" in reply and "ללא שיוך" not in reply


@pytest.mark.parametrize(
    "prefix", ["What happens if I cancel", "איך מבטלים", "Explain how to cancel", "בטלפון"]
)
def test_question_about_mutation_does_not_execute_it(db, prefix):
    handle_reminder_message(
        item=item("Remind me tomorrow at 11:30 to prepare invoice"),
        store=LeadStore(db),
        settings=settings(),
        now=NOW,
    )
    row = db.scalars(select(OwnerReminderRow)).one()
    handle_reminder_message(
        item=item(f"{prefix} {row.id} revision 1?", "query"),
        store=LeadStore(db),
        settings=settings(),
        now=NOW,
    )
    db.refresh(row)
    assert row.status == "active" and row.revision == 1


def test_affirmative_text_cancel_executes(db):
    handle_reminder_message(
        item=item("Remind me tomorrow at 11:30 to prepare invoice"),
        store=LeadStore(db),
        settings=settings(),
        now=NOW,
    )
    row = db.scalars(select(OwnerReminderRow)).one()
    reply = handle_reminder_message(
        item=item(f"Cancel reminder {row.id} revision 1", "cancel"),
        store=LeadStore(db),
        settings=settings(),
        now=NOW,
    )
    assert "עודכנה" in reply
    db.refresh(row)
    assert row.status == "cancelled" and row.revision == 2


def test_supplied_unsupported_snooze_time_never_defaults_to_fifteen_minutes(db):
    handle_reminder_message(
        item=item("Remind me tomorrow at 11:30 to prepare invoice"),
        store=LeadStore(db),
        settings=settings(),
        now=NOW,
    )
    row = db.scalars(select(OwnerReminderRow)).one()
    reply = handle_reminder_message(
        item=item(f"Snooze {row.id} revision 1 for a while", "snooze"),
        store=LeadStore(db),
        settings=settings(),
        now=NOW,
    )
    assert "עודכנה" not in reply
    db.refresh(row)
    assert row.revision == 1


def test_duplicate_creation_after_due_time_reports_persisted_reminder(db):
    from datetime import timedelta

    incoming = item("Remind me today at 11:01 to prepare invoice")
    first = handle_reminder_message(
        item=incoming, store=LeadStore(db), settings=settings(), now=NOW
    )
    assert "נשמרה" in first
    again = handle_reminder_message(
        item=incoming, store=LeadStore(db), settings=settings(), now=NOW + timedelta(minutes=2)
    )
    assert "נשמרה" in again and "המועד כבר עבר" not in again
    assert db.scalar(select(func.count()).select_from(OwnerReminderRow)) == 1


def test_calendar_meeting_and_customer_outreach_are_not_reminder_intents(db):
    for text in (
        "Book a meeting tomorrow at 11:30",
        "שלחי לדני הצעת מחיר מחר",
        "list calendar",
        "Don't remind me tomorrow at 11:30",
        "אל תזכירי לי מחר ב-11:30",
        "Explain how 'remind me tomorrow at 11:30' works",
    ):
        assert (
            handle_reminder_message(
                item=item(text), store=LeadStore(db), settings=settings(), now=NOW
            )
            is None
        )


def test_callback_duplicate_and_stale_snooze_do_not_revive_completed(db):
    handle_reminder_message(
        item=item("Remind me tomorrow at 11:30 to prepare invoice"),
        store=LeadStore(db),
        settings=settings(),
        now=NOW,
    )
    row = db.scalars(select(OwnerReminderRow)).one()
    callback = {"from": "123", "chat_id": "123", "data": f"rm:complete:{row.id}:1"}
    assert "עודכנה" in handle_reminder_callback(
        callback=callback, store=LeadStore(db), settings=settings(), now=NOW
    )
    callback["data"] = f"rm:snooze:{row.id}:1"
    assert "השתנתה" in handle_reminder_callback(
        callback=callback, store=LeadStore(db), settings=settings(), now=NOW
    )
    db.refresh(row)
    assert row.status == "completed"


async def test_actual_owner_loop_intercepts_before_any_calendar_tool(db, monkeypatch):
    from app.surfaces import owner

    monkeypatch.setattr(
        owner,
        "_talk_with_optional_agent",
        lambda **_: pytest.fail("reminder routed into general/Calendar tools"),
    )
    calls = []

    class FakePort:
        async def send(self, message):
            calls.append(message)

    incoming = item("Remind me tomorrow at 11:30 to prepare invoice")
    # Real clock is permitted here, so use a future explicit date.
    incoming["text"] = "Remind me 2099-10-12 at 11:30 to prepare invoice"
    result = await owner.run_owner_loop(
        item=incoming, store=LeadStore(db), port=FakePort(), settings=settings(), owner_ids={"123"}
    )
    assert result.sent and "נשמרה" in result.last_reply
    assert len(calls) == 1
    assert db.scalar(select(func.count()).select_from(OwnerReminderRow)) == 1
