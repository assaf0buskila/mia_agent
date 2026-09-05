"""Approved provider writes must not replay after an outcome-commit crash."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import (
    ACTION_GMAIL_SEND,
    DECISION_APPROVED,
    RESOURCE_GMAIL,
    payload_hash,
)
from app.domain.events import Channel
from app.domain.gmail.drafts import execute_approved_gmail_send
from app.domain.owner.calendar_writes import (
    ACTION_CALENDAR_CREATE,
    apply_owner_calendar_change_request,
    decide_calendar_change,
    execute_approved_calendar_change,
)
from app.domain.owner.linkedin_writes import (
    _digest,
    _parameters,
    execute_approved_linkedin_write,
)
from app.integrations.calendar import FakeCalendarPort, TimeSlot
from app.integrations.calendar_booking import FakeCalendarBookingPort
from app.integrations.composio_catalog import CatalogTool, ComposioCatalog
from app.integrations.gmail import FakeGmailPort
from sqlalchemy.orm import Session


def _store() -> tuple[Session, LeadStore]:
    init_db()
    session = get_session_factory()()
    return session, LeadStore(session)


def test_gmail_claim_is_committed_before_provider_send_and_replay_never_resends() -> None:
    session, store = _store()
    try:
        draft_id = "draft_crash_safe"
        store.upsert_gmail_approval(
            channel=Channel.TELEGRAM.value,
            action=ACTION_GMAIL_SEND,
            risk="R3",
            payload_hash="a" * 64,
            decision="pending",
            resource_type=RESOURCE_GMAIL,
            resource_id=draft_id,
            expires_at="2099-01-01T00:00:00+00:00",
        )
        row = store.get_approval_by_resource(RESOURCE_GMAIL, draft_id, ACTION_GMAIL_SEND)
        assert row is not None
        row.decision = DECISION_APPROVED
        row.payload_hash = payload_hash(
            action=ACTION_GMAIL_SEND,
            risk="R3",
            channel=Channel.TELEGRAM.value,
            resource_type=RESOURCE_GMAIL,
            resource_id=draft_id,
        )
        session.commit()

        class ClaimInspectingPort(FakeGmailPort):
            def send_draft(self, value: str) -> bool:
                other = get_session_factory()()
                try:
                    assert LeadStore(other).get_provider_write_status(
                        scope="approval", key=f"{draft_id}:send:{ACTION_GMAIL_SEND}"
                    ) == "provider_claimed"
                finally:
                    other.close()
                self.sent_drafts.append(value)
                return True

        port = ClaimInspectingPort()
        # This represents provider success followed by a failed local outcome commit.
        store.complete_provider_write = lambda **_kwargs: False  # type: ignore[method-assign]
        assert "ממתינה לבדיקה" in execute_approved_gmail_send(
            store=store,
            settings=Settings(gmail_send=True),
            port=port,
            draft_id=draft_id,
            kill_switch=False,
            demo_active=False,
        )
        assert port.sent_drafts == [draft_id]
        assert "לא שלחתי שוב" in execute_approved_gmail_send(
            store=store,
            settings=Settings(gmail_send=True),
            port=port,
            draft_id=draft_id,
            kill_switch=False,
            demo_active=False,
        )
        assert port.sent_drafts == [draft_id]
    finally:
        session.close()


def test_calendar_create_reconciles_durable_claim_without_reissuing_provider_write() -> None:
    session, store = _store()
    try:
        text = "צור אירוע: פגישת בטיחות בתל אביב | 2026-09-03T10:00 | 60 | Asia/Jerusalem"
        apply_owner_calendar_change_request(
            store, text=text, channel=Channel.TELEGRAM, kill_switch=False, demo_active=False,
            default_timezone="Asia/Jerusalem",
        )
        row = next(
            item
            for item in store.list_all_pending_approvals()
            if item.action == ACTION_CALENDAR_CREATE
        )
        _, resource_id = decide_calendar_change(
            store, text=f"אשר אירוע {row.approval_id}", kill_switch=False
        )
        assert resource_id
        operation_key = f"{resource_id}:execute:{ACTION_CALENDAR_CREATE}"
        assert store.claim_provider_write(scope="approval", key=operation_key)
        start = datetime(2026, 9, 3, 7, 0, tzinfo=UTC)
        calendar = FakeCalendarPort([TimeSlot(start=start, end=start + timedelta(hours=2))])
        booking = FakeCalendarBookingPort()
        booking.create_event(
            booking_key="mia_" + hashlib.sha256(resource_id.encode()).hexdigest(),
            start=start,
            end=start + timedelta(hours=1),
            timezone="Asia/Jerusalem",
            summary="פגישת בטיחות בתל אביב",
            create_meeting_room=False,
            allow_nonstandard_duration=True,
        )
        booking.create_calls.clear()
        assert execute_approved_calendar_change(
            store=store, settings=Settings(calendar_write=True), calendar=calendar, booking=booking,
            resource_id=resource_id, kill_switch=False, demo_active=False,
        ) == "יצרתי את האירוע ביומן."
        assert booking.create_calls == []
        assert store.get_provider_write_status(scope="approval", key=operation_key) == "completed"
    finally:
        session.close()


def test_linkedin_ambiguous_provider_outcome_is_pending_review_and_never_replayed(
    monkeypatch,
) -> None:
    parameters = _parameters("LINKEDIN_POST_UPDATE", {"text": "hello"})
    row = SimpleNamespace(
        channel="telegram",
        resource_id="li_crash_safe",
        risk="R4",
        proposed_parameters=parameters,
        payload_hash=_digest(
            channel="telegram",
            resource_id="li_crash_safe",
            risk="R4",
            parameters=parameters,
        ),
        decision=DECISION_APPROVED,
        expires_at="2099-01-01T00:00:00+00:00",
    )

    class Store:
        def __init__(self) -> None:
            self.claimed = False
            self.pending = False

        def get_approval_by_resource(self, *_args):
            return row

        def claim_provider_write(self, **_kwargs):
            if self.claimed:
                return False
            self.claimed = True
            return True

        def mark_provider_write_pending_review(self, **_kwargs):
            self.pending = True

        def complete_provider_write(self, **_kwargs):
            return True

    class Catalog:
        calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def detail(self, _slug):
            return CatalogTool(
                "LINKEDIN_POST_UPDATE",
                "LINKEDIN",
                "post",
                {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            )

        def execute(self, *_args):
            self.calls += 1
            return None

    catalog = Catalog()
    monkeypatch.setattr(
        ComposioCatalog,
        "from_settings",
        classmethod(lambda _cls, _settings: catalog),
    )
    store = Store()
    first = execute_approved_linkedin_write(
        store=store, settings=Settings(), resource_id="li_crash_safe", kill_switch=False
    )
    replay = execute_approved_linkedin_write(
        store=store, settings=Settings(), resource_id="li_crash_safe", kill_switch=False
    )
    assert "pending review" in first
    assert "pending review" in replay
    assert store.pending is True
    assert catalog.calls == 1
