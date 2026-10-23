"""OS-RUNTIME F2: the Telegram lead-path callback runs off the loop and commits before it sends.

``choose_lead_path`` makes up to three blocking Apps Script calls (a ledger call under postgres) and
then flushes an UPDATE of the preparation ApprovalRow. Before this chunk the handler ran it inline
inside ``async def _handle_callback`` and held that row lock across ``port.send`` and
``edit_message_text`` until ``get_db`` committed at request end.

A file-backed SQLite store or a disposable PostgreSQL schema gives every session its own
connection, so a probe session sees only committed rows (an in-memory StaticPool shares one
connection and would prove nothing).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest
from app.api import deps
from app.api import telegram as telegram_api
from app.db.base import Base
from app.db.models import (
    ApprovalRow,
    CrmContactConversationRow,
    CrmContactRow,
    CrmOutboxRow,
)
from app.db.session import make_engine
from app.domain.owner.callbacks import approval_token
from app.integrations.base import RecordingMessagePort
from app.services import assaf_os_leads
from app.services.assaf_os_leads import FINAL_KIND, prepare_lead_choice
from app.services.owner_actions import read_owner_action
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from tests.services.os_v2_28_support import NOW, OWNER, LedgerWorld, pg_settings
from tests.unit import test_website_lead_approval as sheets_leads

_WRITERS = (
    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
    "AND pid <> pg_backend_pid() AND backend_xid IS NOT NULL"
)


@pytest.fixture(params=["sqlite", "postgres"])
def factory(request, tmp_path, monkeypatch):
    admin = None
    schema = "f2_" + uuid4().hex
    if request.param == "postgres":
        url = os.environ.get("MIA_TEST_POSTGRES_URL", "")
        if not url:
            pytest.skip("MIA_TEST_POSTGRES_URL required")
        admin = create_engine(url)
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = make_engine(f"sqlite:///{tmp_path / 'f2.db'}")
    Base.metadata.create_all(engine, checkfirst=request.param != "postgres")
    # The production session settings: autoflush off, expire_on_commit at its default.
    built = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    built.dialect_name = request.param
    # The request session (get_db) and the worker thread both open sessions from the factory.
    monkeypatch.setattr("app.db.session.get_session_factory", lambda: built)
    monkeypatch.setattr(deps, "get_session_factory", lambda: built)
    yield built
    engine.dispose()
    if admin is not None:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture(params=["sheets", "ledger"])
def prepared(request, factory, monkeypatch):
    """The settings and the committed preparation card for one captured website lead."""
    if request.param == "ledger":
        world = LedgerWorld().install(monkeypatch)
        monkeypatch.setattr(
            "app.services.assaf_os.AssafOsPort",
            lambda *_a, **_k: pytest.fail("the ledger path must never build the Apps Script port"),
        )
        settings = pg_settings()
        del world
    else:
        # The fake keeps its state on the class: patch it so nothing leaks into other modules.
        for name, value in (
            ("proposals", []), ("snapshot_rows", []), ("decisions", []), ("current_proposal", {}),
            ("snapshot_calls", 0),
        ):
            monkeypatch.setattr(sheets_leads.FakeAssafOsPort, name, value)
        monkeypatch.setattr("app.services.assaf_os.AssafOsPort", sheets_leads.FakeAssafOsPort)
        settings = sheets_leads._settings()
    with factory() as session:
        from app.db.store import LeadStore

        store = LeadStore(session)
        payload = _seed_lead_source(store)
        card = prepare_lead_choice(
            store, settings=settings, recipient_id=OWNER, payload=payload
        )
        session.commit()
        approval_id = card.proposal.approval_id
    return settings, approval_id


def _seed_lead_source(store) -> dict:
    """One captured website conversation with its durable Telegram delivery payload.

    The same rows as ``os_v2_28_support.seed_lead_source``, flushed parent first because
    PostgreSQL enforces the contact foreign key where SQLite does not.
    """
    captured = {
        "name": "דנה",
        "phone": "+972502222222",
        "email": "visitor@example.com",
        "source": "AssafWeb",
        "status": "new",
    }
    raw = {**captured, "phone": "050-222-2222"}
    store.session.add(
        CrmContactRow(
            id="crm-contact-1",
            revision=1,
            fields_json=json.dumps(captured, ensure_ascii=False),
            source_ref="old-source",
            conversation_id="older-conversation",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    store.session.flush()
    store.session.add(
        CrmContactConversationRow(
            id="conversation-link-1",
            contact_id="crm-contact-1",
            conversation_id="site-current",
            captured_fields_json=json.dumps(captured, ensure_ascii=False),
            source_ref="website:site-current",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    payload = {
        "conversation_id": "site-current",
        "recipient_id": OWNER,
        "text": "new lead",
        "lead_source": {"source_ref": "website:site-current", "raw_fields": raw},
    }
    store.session.add(
        CrmOutboxRow(
            id="outbox-1",
            dedupe_key="telegram:site-current",
            aggregate_type="contact",
            aggregate_id="crm-contact-1",
            destination="telegram",
            payload_json=json.dumps(payload, ensure_ascii=False),
            status="pending",
            created_at=NOW,
        )
    )
    store.session.flush()
    return payload


def _probe(factory, preparation_id: str) -> dict:
    """What a SEPARATE connection sees right now: committed state only."""
    with factory() as fresh:
        prep = fresh.scalars(
            select(ApprovalRow).where(ApprovalRow.approval_id == preparation_id)
        ).one()
        finals = [
            row
            for row in fresh.scalars(select(ApprovalRow)).all()
            if (read_owner_action(row) or {}).get("kind") == FINAL_KIND
        ]
        snapshot = {
            "executed_at": bool(prep.executed_at),
            "result": prep.result or "",
            "finals": len(finals),
            "writers": None,
        }
        if factory.dialect_name == "postgres":
            snapshot["writers"] = fresh.execute(text(_WRITERS)).scalar()
        return snapshot


class _Port(RecordingMessagePort):
    """Records what a separate connection sees at every Telegram call."""

    def __init__(self, probe) -> None:
        super().__init__()
        self._probe = probe
        self.seen: list[tuple[str, dict]] = []
        self.edited: list[str] = []

    async def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> None:
        return None

    async def send(self, message) -> None:
        self.seen.append(("send", self._probe()))
        await super().send(message)

    async def edit_message_text(
        self, *, chat_id, message_id, text, parse_mode=None, clear_markup=True
    ) -> None:
        self.seen.append(("edit", self._probe()))
        self.edited.append(text)


async def _press(settings, port, preparation_id: str, *, path: str = "l") -> dict:
    callback = {
        "callback_query_id": "q-lead-path",
        "from": OWNER,
        "data": f"lp:{path}:{approval_token(preparation_id)}",
        "chat_id": OWNER,
        "chat_type": "private",
        "message_id": "71",
    }
    # The same session lifecycle as POST /v1/telegram/webhook: get_db commits at request end.
    with contextmanager(deps.get_db)() as db:
        return await telegram_api._handle_callback(
            callback=callback, port=port, owner_ids={OWNER}, db=db, settings=settings
        )


async def test_the_blocking_lead_path_call_does_not_stall_the_event_loop(
    factory, prepared, monkeypatch
):
    settings, preparation_id = prepared
    real = assaf_os_leads.choose_lead_path

    def blocking(*args, **kwargs):
        time.sleep(0.3)  # stands in for the 25 s Apps Script budget
        return real(*args, **kwargs)

    monkeypatch.setattr(assaf_os_leads, "choose_lead_path", blocking)
    ticks = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)  # the ticker is running before the handler starts
    port = _Port(lambda: _probe(factory, preparation_id))
    before = ticks
    result = await _press(settings, port, preparation_id)
    during = ticks - before
    stop.set()
    await task
    print(f"OS-RUNTIME F2a ticker_ticks_during_handler={during}")
    assert result["status"] == "prepared" and result["sent"] is True
    assert during >= 10, f"the loop advanced {during} ticks during a 0.3 s blocking call"


async def test_the_preparation_and_final_rows_are_committed_before_any_telegram_call(
    factory, prepared
):
    settings, preparation_id = prepared
    port = _Port(lambda: _probe(factory, preparation_id))
    result = await _press(settings, port, preparation_id)
    assert result["status"] == "prepared"
    assert [event for event, _ in port.seen] == ["send", "edit"]
    for event, snapshot in port.seen:
        print(
            f"OS-RUNTIME F2b at_{event} prep_committed={snapshot['executed_at']} "
            f"result={snapshot['result'][:9]!r} final_rows={snapshot['finals']} "
            f"foreign_write_xids={snapshot['writers']}"
        )
        assert snapshot["executed_at"], f"the preparation row is not committed at {event}"
        assert snapshot["result"].startswith("prepared:"), event
        assert snapshot["finals"] == 1, f"the final card row is not visible at {event}"
        if snapshot["writers"] is not None:
            assert snapshot["writers"] == 0, f"another backend holds a write tx at {event}"
    # The row the owner is about to tap exists, committed, with the keyboard that targets it.
    final_id = port.sent[0].idempotency_key.removeprefix("lead-final:")
    with factory() as fresh:
        final = fresh.scalars(select(ApprovalRow).where(ApprovalRow.approval_id == final_id)).one()
        assert read_owner_action(final)["kind"] == FINAL_KIND


async def test_the_handler_never_touches_the_request_session_for_the_lead_path(
    factory, prepared
):
    settings, preparation_id = prepared
    port = _Port(lambda: _probe(factory, preparation_id))
    touched: list[str] = []

    class Untouchable:
        def __getattr__(self, name: str):
            touched.append(name)
            raise AssertionError(f"the lead-path branch used db.{name}")

    callback = {
        "callback_query_id": "q-lead-path",
        "from": OWNER,
        "data": f"lp:a:{approval_token(preparation_id)}",
        "chat_id": OWNER,
        "chat_type": "private",
        "message_id": "71",
    }
    result = await telegram_api._handle_callback(
        callback=callback, port=port, owner_ids={OWNER}, db=Untouchable(), settings=settings
    )
    assert result["status"] == "prepared" and touched == []


async def test_a_failure_in_the_worker_rolls_back_and_propagates(factory, prepared, monkeypatch):
    settings, preparation_id = prepared

    def explode(*_args, **_kwargs):
        raise RuntimeError("synthetic lead path failure")

    monkeypatch.setattr(assaf_os_leads, "choose_lead_path", explode)
    port = _Port(lambda: _probe(factory, preparation_id))
    with pytest.raises(RuntimeError, match="synthetic lead path failure"):
        await _press(settings, port, preparation_id)
    assert port.seen == [] and port.sent == []
    assert _probe(factory, preparation_id)["executed_at"] is False


async def test_the_replies_stay_byte_identical(factory, prepared):
    settings, preparation_id = prepared
    port = _Port(lambda: _probe(factory, preparation_id))
    await _press(settings, port, preparation_id)
    (message,) = port.sent
    final_id = message.idempotency_key.removeprefix("lead-final:")
    assert message.idempotency_key == f"lead-final:{final_id}"
    assert message.conversation_id == OWNER and message.parse_mode == "HTML"
    assert message.reply_markup["inline_keyboard"][0][0]["callback_data"] == (
        "ok:" + approval_token(final_id)
    )
    assert port.edited == [
        "המסלול נבחר והוכנה הצעה מדויקת. עדיין לא נכתבה רשומה; יש לאשר את הכרטיס החדש."
    ]
    # A replay is answered from the committed state, not re-run.
    again = _Port(lambda: _probe(factory, preparation_id))
    replay = await _press(settings, again, preparation_id)
    assert replay["status"] == "already_handled" and again.sent == []
    assert again.edited == ["בקשת ההכנה כבר טופלה ולא ביצעתי אותה שוב."]
