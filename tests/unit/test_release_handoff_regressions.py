"""Focused regressions for the final v2 release blockers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.base import Base
from app.db.models import CrmActivityRow, CrmContactRow, CrmIssueRow, CrmOutboxRow
from app.db.session import make_engine
from app.db.store import LeadStore
from app.integrations.llm_client import LlmResponse, ToolCall
from app.services.crm_v2 import CrmService
from app.services.owner_actions import propose_owner_action
from app.surfaces.site_v2 import SiteV2State, _actual_contact
from app.workers.crm_delivery import CrmDeliveryWorker
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker


class _CallbackPort:
    def __init__(self) -> None:
        self.edited: list[dict] = []

    async def answer_callback_query(self, _callback_query_id: str) -> None:
        return None

    async def edit_message_text(self, **kwargs) -> None:  # noqa: ANN003
        self.edited.append(kwargs)


def _crm_settings(*, kill_switch: bool) -> Settings:
    return Settings(
        _env_file=None,
        telegram_owner_user_ids="123",
        crm_v2_enabled=True,
        kill_switch=kill_switch,
    )


@pytest.mark.asyncio
async def test_kill_switch_crm_callback_keeps_pending_without_constructing_sheets(
    monkeypatch, tmp_path
) -> None:
    from app.api import telegram as telegram_api

    engine = make_engine(f"sqlite:///{tmp_path / 'crm-kill-switch.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    constructed = []
    try:
        store = LeadStore(db)
        fields = {"name": "Stopped Contact", "email": "stopped@example.com"}
        snapshot = CrmService(db).snapshot_identity(fields)
        proposal = propose_owner_action(
            store,
            principal=Principal.owner(source="telegram", actor_id="123"),
            source_ref="telegram:kill-switch-crm",
            kind="crm.upsert",
            parameters={
                "fields": fields,
                "contact_id": snapshot.contact_id,
                "expected_revision": snapshot.revision,
            },
            target={
                "contact_id": snapshot.contact_id,
                "revision": snapshot.revision,
                "snapshot_hash": snapshot.snapshot_hash,
                "fields": snapshot.fields,
            },
        )
        db.commit()

        def fail_if_constructed(_settings):
            constructed.append(True)
            raise AssertionError("Sheets must not be constructed while stopped")

        monkeypatch.setattr(telegram_api, "build_sheets_port", fail_if_constructed)
        result = await telegram_api._handle_callback(
            callback={
                "callback_query_id": "kill-switch-callback",
                "from": "123",
                "data": f"ok:{proposal.approval_id}",
                "chat_id": "123",
                "message_id": "1",
            },
            port=_CallbackPort(),
            owner_ids={"123"},
            db=db,
            settings=_crm_settings(kill_switch=True),
        )
        db.commit()

        row = store.get_approval_by_approval_id(proposal.approval_id)
        assert result["processed"] == 1
        assert row is not None and row.decision == "pending"
        assert constructed == []
        assert db.scalar(select(CrmContactRow)) is None
        assert db.scalar(select(CrmActivityRow)) is None
    finally:
        db.close()
        engine.dispose()


def _outbox_job(
    *, job_id: str, aggregate_id: str, created_at: str, status: str, destination: str = "telegram"
) -> CrmOutboxRow:
    return CrmOutboxRow(
        id=job_id,
        dedupe_key=f"dedupe:{job_id}",
        aggregate_type="contact",
        aggregate_id=aggregate_id,
        destination=destination,
        payload_json=json.dumps({"conversation_id": aggregate_id, "recipient_id": "123"}),
        status=status,
        attempts=0,
        next_attempt_at="2026-09-11T00:00:00+00:00",
        lease_owner="",
        lease_expires_at="",
        last_attempt_at="",
        confirmed_at="",
        last_error="",
        created_at=created_at,
    )


def test_blocked_contact_predecessors_do_not_fill_claim_limit(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'crm-fairness.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        base = datetime(2026, 9, 11, tzinfo=UTC)
        db.add(
            _outbox_job(
                job_id="unknown-a",
                aggregate_id="contact-a",
                created_at=base.isoformat(),
                status="unknown",
            )
        )
        for index in range(25):
            db.add(
                _outbox_job(
                    job_id=f"successor-a-{index}",
                    aggregate_id="contact-a",
                    created_at=(base + timedelta(seconds=index + 1)).isoformat(),
                    status="pending",
                )
            )
        db.add(
            _outbox_job(
                job_id="independent-b",
                aggregate_id="contact-b",
                created_at=(base + timedelta(seconds=100)).isoformat(),
                status="pending",
            )
        )
        db.commit()
        worker = CrmDeliveryWorker(
            session_factory=factory,
            sheets=SimpleNamespace(),
            now=lambda: base + timedelta(minutes=1),
        )
        assert worker._claim_one() == "independent-b"
    finally:
        db.close()
        engine.dispose()


def test_blocked_distinct_contacts_do_not_starve_an_independent_job(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'crm-distinct-fairness.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        base = datetime(2026, 9, 11, tzinfo=UTC)
        for index in range(25):
            contact_id = f"blocked-contact-{index}"
            db.add(CrmContactRow(id=contact_id, created_at=base.isoformat()))
            db.add(
                _outbox_job(
                    job_id=f"blocked-contact-job-{index}",
                    aggregate_id=contact_id,
                    created_at=(base + timedelta(seconds=index)).isoformat(),
                    status="pending",
                    destination="contacts",
                )
            )
            db.add(
                CrmIssueRow(
                    id=f"open-issue-{index}",
                    contact_id=contact_id,
                    issue_type="field_conflict",
                    status="open",
                    created_at=base.isoformat(),
                )
            )
        db.add(
            _outbox_job(
                job_id="independent-telegram",
                aggregate_id="independent-contact",
                created_at=(base + timedelta(seconds=100)).isoformat(),
                status="pending",
            )
        )
        db.commit()
        worker = CrmDeliveryWorker(
            session_factory=factory,
            sheets=SimpleNamespace(),
            now=lambda: base + timedelta(minutes=1),
        )
        assert worker._claim_one() == "independent-telegram"
    finally:
        db.close()
        engine.dispose()


class _ConsentClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def enabled(self) -> bool:
        return True

    def complete(self, **kwargs):  # noqa: ANN003
        prompt = kwargs["messages"][0]["content"]
        self.prompts.append(prompt)
        text = "Please have Assaf get in touch with me at alex@example.com."
        return LlmResponse(
            "",
            (
                ToolCall(
                    "consent-1",
                    "classify_contact_consent",
                    {
                        "decision": "affirmative",
                        "evidence": text,
                        "contact_span": "alex@example.com",
                    },
                    "{}",
                ),
            ),
            "stop",
            "",
            0,
            0,
            {"role": "assistant", "content": None},
        )


def test_contact_bearing_wording_reaches_whole_input_consent_classifier() -> None:
    client = _ConsentClient()
    result = _actual_contact(
        SiteV2State(),
        client=client,
        text="Please have Assaf get in touch with me at alex@example.com.",
        name="",
        phone="",
        email="",
        date="",
    )
    assert result == {"email": "alex@example.com"}
    assert len(client.prompts) == 1
    assert (
        'CURRENT_INPUT="Please have Assaf get in touch with me at alex@example.com."'
        in client.prompts[0]
    )


def test_widget_contact_status_uses_backend_delivery_state() -> None:
    source = Path("app/web/ask_mia.js").read_text(encoding="utf-8")
    assert "הפרטים נשמרו. אסף יחזור אליכם." not in source
    assert "data.delivery_status" in source
    assert "הפרטים נשמרו והמסירה לאסף עדיין ממתינה." in source
    assert "הפרטים נשמרו והמסירה לאסף אושרה." in source
