from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.db.models import CrmActivityRow, CrmContactRow
from app.db.reminders import OwnerReminderRow
from app.services import owner_os_reads
from app.services.owner_os_reads import (
    CursorCodec,
    OwnerOsCursorInvalid,
    OwnerOsInvalidQuery,
    OwnerOsReader,
    OwnerOsSourceDataInvalid,
    OwnerOsUnsafeDatabase,
    read_snapshot,
    section,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

OWNER = "12345"
WORKSPACE = "assafweb"
SECRET = "cursor-test-secret-that-is-at-least-thirty-two-bytes"
NOW = datetime(2026, 10, 11, 8, tzinfo=UTC)


@pytest.fixture
def database() -> sessionmaker[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    CrmContactRow.__table__.create(engine)
    CrmActivityRow.__table__.create(engine)
    OwnerReminderRow.__table__.create(engine)
    return sessionmaker(engine, autoflush=False, expire_on_commit=False)


def contact(
    contact_id: str,
    *,
    updated_at: str,
    revision: int = 1,
    name: str = "דנה",
    next_step: str = "",
    date: str = "",
    summary: str = "private detail",
) -> CrmContactRow:
    return CrmContactRow(
        id=contact_id,
        revision=revision,
        fields_json=json.dumps(
            {
                "name": name,
                "phone": "+972501234567",
                "email": "dana@example.test",
                "business": "Example",
                "want": "אתר",
                "summary": summary,
                "source": "referral",
                "status": "lead",
                "next_step": next_step,
                "date": date,
            },
            ensure_ascii=False,
        ),
        source_ref="must-not-leak",
        conversation_id="must-not-leak",
        created_at=updated_at,
        updated_at=updated_at,
    )


def reader(session: Session, owner_id: str = OWNER) -> OwnerOsReader:
    value = object.__new__(OwnerOsReader)
    value.session = session
    value.owner_id = owner_id
    value.workspace_id = WORKSPACE
    value.cursor = CursorCodec(SECRET)
    value.observed_at = NOW.isoformat().replace("+00:00", "Z")
    return value


def test_cursor_is_short_signed_scoped_and_expires() -> None:
    codec = CursorCodec(SECRET)
    token = codec.encode(
        owner_id=OWNER,
        workspace_id=WORKSPACE,
        context="contacts|updated_at_desc_id_desc|limit=20",
        row_id="a" * 64,
        checksum=42,
        now=100,
    )
    assert len(token) <= 128
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
    assert codec.decode(
        owner_id=OWNER,
        workspace_id=WORKSPACE,
        context="contacts|updated_at_desc_id_desc|limit=20",
        token=token,
        now=101,
    ).row_id == "a" * 64
    with pytest.raises(OwnerOsInvalidQuery):
        codec.decode(
            owner_id="999",
            workspace_id=WORKSPACE,
            context="contacts|updated_at_desc_id_desc|limit=20",
            token=token,
            now=101,
        )
    with pytest.raises(OwnerOsInvalidQuery):
        codec.decode(
            owner_id=OWNER,
            workspace_id=WORKSPACE,
            context="contacts|updated_at_desc_id_desc|limit=50",
            token=token,
            now=101,
        )
    with pytest.raises(OwnerOsInvalidQuery):
        codec.decode(
            owner_id=OWNER,
            workspace_id="different_workspace",
            context="contacts|updated_at_desc_id_desc|limit=20",
            token=token,
            now=101,
        )
    with pytest.raises(OwnerOsInvalidQuery):
        codec.decode(
            owner_id=OWNER,
            workspace_id=WORKSPACE,
            context="contacts|updated_at_desc_id_desc|limit=20",
            token=token[:-1] + ("A" if token[-1] != "A" else "B"),
        )
    with pytest.raises(OwnerOsCursorInvalid):
        codec.decode(
            owner_id=OWNER,
            workspace_id=WORKSPACE,
            context="contacts|updated_at_desc_id_desc|limit=20",
            token=token,
            now=401,
        )


def test_contacts_are_keyset_paginated_and_list_does_not_leak_detail(database) -> None:
    with database() as db:
        db.add_all(
            [
                contact("contact_03", updated_at="2026-10-11T08:03:00Z", revision=3),
                contact("contact_02", updated_at="2026-10-11T08:02:00Z", revision=2),
                contact("contact_01", updated_at="2026-10-11T08:01:00Z"),
            ]
        )
        db.commit()
        service = reader(db)
        first = service.list_contacts(limit=2)
        assert [item["contact_id"] for item in first.items] == ["contact_03", "contact_02"]
        assert first.has_more is True
        assert first.next_cursor and len(first.next_cursor) <= 128
        assert "summary" not in first.items[0]
        assert "need" not in first.items[0]
        assert "source_ref" not in json.dumps(first.items[0])
        second = service.list_contacts(limit=2, cursor=first.next_cursor)
        assert [item["contact_id"] for item in second.items] == ["contact_01"]
        assert second.has_more is False


def test_cursor_fails_if_boundary_contact_changed(database) -> None:
    with database() as db:
        db.add_all(
            [
                contact("contact_02", updated_at="2026-10-11T08:02:00Z", revision=2),
                contact("contact_01", updated_at="2026-10-11T08:01:00Z"),
            ]
        )
        db.commit()
        service = reader(db)
        first = service.list_contacts(limit=1)
        boundary = db.get(CrmContactRow, "contact_02")
        assert boundary is not None
        boundary.revision += 1
        db.commit()
        with pytest.raises(OwnerOsCursorInvalid):
            service.list_contacts(limit=1, cursor=first.next_cursor)


def test_next_actions_only_project_current_nonblank_steps_and_never_invent_dates(database) -> None:
    with database() as db:
        db.add_all(
            [
                contact(
                    "contact_action",
                    updated_at="2026-10-11T08:02:00Z",
                    revision=4,
                    next_step="להתקשר ביום שני",
                    date="2026-10-13",
                ),
                contact("contact_blank", updated_at="2026-10-11T08:01:00Z", next_step="  "),
            ]
        )
        db.commit()
        page = reader(db).list_next_actions()
        assert len(page.items) == 1
        item = page.items[0]
        assert item["item_id"] == "contact_next_action:contact_action"
        assert item["due"] == {
            "date": None,
            "at": None,
            "precision": "none",
            "timezone": "Asia/Jerusalem",
            "semantic_state": "unverified",
            "reason_codes": ["legacy_date_semantics_unverified"],
        }
        assert item["due_state"] == "unknown"
        assert item["record_quality"]["stored_read"] == "verified"


def test_activity_output_is_allowlisted_and_scoped_to_contact(database) -> None:
    with database() as db:
        db.add_all(
            [
                contact("contact_01", updated_at="2026-10-11T08:01:00Z"),
                contact("contact_02", updated_at="2026-10-11T08:02:00Z"),
            ]
        )
        db.add_all(
            [
                CrmActivityRow(
                    id="activity_01",
                    contact_id="contact_01",
                    occurred_at="2026-10-11T08:05:00Z",
                    who="private actor",
                    channel="website",
                    action="captured",
                    result="stored",
                    source_ref="private-provider-payload",
                    created_at="2026-10-11T08:05:00Z",
                ),
                CrmActivityRow(
                    id="activity_02",
                    contact_id="contact_02",
                    occurred_at="2026-10-11T08:06:00Z",
                    who="private actor",
                    channel="owner",
                    action="note",
                    result="stored",
                    source_ref="private-ref",
                    created_at="2026-10-11T08:06:00Z",
                ),
            ]
        )
        db.commit()
        page = reader(db).list_activities(contact_id="contact_01")
        assert [item["activity_id"] for item in page.items] == ["activity_01"]
        serialized = json.dumps(page.items)
        assert "private actor" not in serialized
        assert "private-provider-payload" not in serialized
        assert page.items[0]["revision"] is None
        assert page.items[0]["immutable_record"] == "unknown"


def test_reminders_are_owner_scoped_and_reject_naive_range(database) -> None:
    with database() as db:
        db.add(contact("contact_01", updated_at="2026-10-11T08:01:00Z"))
        db.add_all(
            [
                OwnerReminderRow(
                    id="reminder_self",
                    owner_id=OWNER,
                    source_event_id="source-self",
                    text="לחזור לדנה",
                    scheduled_at=NOW + timedelta(hours=1),
                    timezone="Asia/Jerusalem",
                    contact_id="contact_01",
                    status="active",
                    revision=2,
                    delivery_state="pending",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                OwnerReminderRow(
                    id="reminder_other",
                    owner_id="999",
                    source_event_id="source-other",
                    text="private other owner",
                    scheduled_at=NOW + timedelta(hours=2),
                    timezone="Asia/Jerusalem",
                    contact_id=None,
                    status="active",
                    revision=1,
                    delivery_state="pending",
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        db.commit()
        # SQLite drops timezone metadata for DateTime; keep the identity-map values
        # aware so this unit test exercises the PostgreSQL DTO contract.
        own = db.get(OwnerReminderRow, "reminder_self")
        other = db.get(OwnerReminderRow, "reminder_other")
        assert own is not None and other is not None
        own.scheduled_at = NOW + timedelta(hours=1)
        other.scheduled_at = NOW + timedelta(hours=2)
        page = reader(db).list_reminders()
        assert [item["reminder_id"] for item in page.items] == ["reminder_self"]
        assert "private other owner" not in json.dumps(page.items, ensure_ascii=False)
        with pytest.raises(OwnerOsInvalidQuery):
            reader(db).list_reminders(from_at=datetime(2026, 10, 11, 9))


def test_verified_empty_is_only_complete_on_fresh_first_page() -> None:
    empty = SimpleNamespace(items=[], next_cursor=None, has_more=False, page_complete=True)
    assert section(empty, coverage_scope="stored_pg_contacts", first_page=True) == {
        "section_state": "available",
        "items": [],
        "coverage": "complete",
        "coverage_scope": "stored_pg_contacts",
        "freshness": "fresh",
        "reason_codes": [],
        "pagination": {"next_cursor": None, "has_more": False, "page_complete": True},
    }
    continued = section(empty, coverage_scope="stored_pg_contacts", first_page=False)
    assert continued["coverage"] == "partial"


def test_canonical_long_reminder_is_explicit_excerpt_without_mutating_source(database) -> None:
    original = "א" * 2_800
    with database() as db:
        row = OwnerReminderRow(
            id="reminder_long",
            owner_id=OWNER,
            source_event_id="source-long",
            text=original,
            scheduled_at=NOW + timedelta(hours=1),
            timezone="Asia/Jerusalem",
            contact_id=None,
            status="active",
            revision=1,
            delivery_state="pending",
            created_at=NOW,
            updated_at=NOW,
        )
        db.add(row)
        db.commit()
        # SQLite drops timezone metadata; PostgreSQL preserves it.
        row.scheduled_at = NOW + timedelta(hours=1)
        page = reader(db).list_reminders()
        assert len(page.items) == 1
        item = page.items[0]
        assert len(item["text"]) == 500
        assert item["text"] == original[:499] + "…"
        assert item["text_truncated"] is True
        assert item["text_presentation"] == "excerpt"
        assert item["full_text_available"] is False
        assert item["reason_codes"] == ["reminder_text_excerpt"]
        assert row.text == original


def test_reminder_excerpt_obeys_javascript_utf16_limit_without_splitting_emoji(database) -> None:
    # Canonical reminder policy is 2,800 Python code points, independent of the
    # frozen frontend's smaller 500 UTF-16-unit presentation boundary.
    original = "A" + "😀" * 2_799
    with database() as db:
        row = OwnerReminderRow(
            id="reminder_emoji",
            owner_id=OWNER,
            source_event_id="source-emoji",
            text=original,
            scheduled_at=NOW + timedelta(hours=1),
            timezone="Asia/Jerusalem",
            contact_id=None,
            status="active",
            revision=1,
            delivery_state="pending",
            created_at=NOW,
            updated_at=NOW,
        )
        db.add(row)
        db.commit()
        row.scheduled_at = NOW + timedelta(hours=1)
        item = reader(db).list_reminders().items[0]
        presented = item["text"]
        assert isinstance(presented, str)
        assert len(presented.encode("utf-16-le")) // 2 == 500
        assert presented.endswith("…")
        assert all(not 0xD800 <= ord(character) <= 0xDFFF for character in presented)
        assert item["text_truncated"] is True
        assert row.text == original


def test_contact_text_bounds_use_utf16_units_and_reject_malformed_surrogates(database) -> None:
    exactly_160_units = "A" + "😀" * 79 + "B"
    beyond_160_units = "A" + "😀" * 80
    with database() as db:
        accepted = contact(
            "contact_unicode_ok",
            updated_at="2026-10-11T08:02:00Z",
            name=exactly_160_units,
        )
        oversized = contact(
            "contact_unicode_large",
            updated_at="2026-10-11T08:01:00Z",
            name=beyond_160_units,
        )
        malformed = contact(
            "contact_unicode_malformed",
            updated_at="2026-10-11T08:00:00Z",
        )
        malformed.fields_json = json.dumps({"name": "\ud800"}, ensure_ascii=True)
        db.add_all([accepted, oversized, malformed])
        db.commit()

        service = reader(db)
        record = service.get_contact("contact_unicode_ok")
        assert record["name"] == exactly_160_units
        assert len(record["name"].encode("utf-16-le")) // 2 == 160
        with pytest.raises(OwnerOsSourceDataInvalid):
            service.get_contact("contact_unicode_large")
        with pytest.raises(OwnerOsSourceDataInvalid):
            service.get_contact("contact_unicode_malformed")


def test_read_snapshot_refuses_non_postgresql_before_opening_a_session() -> None:
    settings = SimpleNamespace(
        owner_os_enabled=True,
        owner_os_read_database_url="sqlite+pysqlite:///:memory:",
    )
    with pytest.raises(OwnerOsUnsafeDatabase):
        with read_snapshot(settings):
            pytest.fail("non-PostgreSQL reader must never yield")


def test_reader_engine_bounds_connection_and_pool_wait(monkeypatch) -> None:
    captured = {}
    sentinel = object()

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(owner_os_reads, "create_engine", fake_create_engine)
    result = owner_os_reads.make_owner_os_engine(
        "postgresql://synthetic:synthetic@127.0.0.1:1/owner_os_test"
    )
    assert result is sentinel
    assert captured == {
        "url": "postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/owner_os_test",
        "kwargs": {
            "connect_args": {"connect_timeout": 5},
            "pool_pre_ping": False,
            "pool_timeout": 5,
        },
    }
