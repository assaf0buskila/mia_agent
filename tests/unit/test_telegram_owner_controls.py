"""Assaf must be able to release a parked lead and approve an action from Telegram.

Both controls existed only on the WhatsApp owner path, which is off. So a
conversation Mia escalated stayed parked forever, and any approval she proposed had
no button and no text command — `pending_approvals` could only grow.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import (
    ACTION_LINKEDIN_COMPOSIO_WRITE,
    DECISION_PENDING,
    approval_expires_at,
)
from app.domain.events import Channel
from app.integrations.base import OutboundMessage
from app.surfaces.owner import run_owner_loop

OWNER = "12345"


class CapturingPort:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)


def _settings() -> Settings:
    return Settings(_env_file=None, telegram_owner_user_ids=OWNER)


def _run(text: str, store: LeadStore, port: CapturingPort):
    item_id = f"tg.{abs(hash(text))}"
    # run_owner_loop marks the webhook at the end, which requires a claimed row.
    store.claim_webhook(provider="telegram", provider_event_id=item_id)
    return asyncio.run(
        run_owner_loop(
            item={"id": item_id, "from": OWNER, "text": text},
            store=store,
            port=port,
            settings=_settings(),
            owner_ids={OWNER},
        )
    )


def test_retired_release_command_does_not_mutate_a_website_or_wa_lead() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _customer_id, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id="972500009001"
        )
        store.set_human_takeover(lead_id, True)
        db.commit()
        assert store.is_human_takeover(lead_id) is True

        port = CapturingPort()
        _run(f"release this lead {lead_id}", store, port)
        db.commit()

        # Deterministic WhatsApp takeover controls were retired with that ingress;
        # ordinary owner text cannot mutate the durable control row.
        assert store.is_human_takeover(lead_id) is True
        assert port.sent, "the owner should get an acknowledgement"
    finally:
        db.close()


def test_retired_takeover_command_does_not_mutate_a_website_or_wa_lead() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _customer_id, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id="972500009002"
        )
        db.commit()
        assert store.is_human_takeover(lead_id) is False

        _run(f"take over this lead {lead_id}", store, CapturingPort())
        db.commit()
        assert store.is_human_takeover(lead_id) is False
    finally:
        db.close()


def test_only_explicit_pending_request_gets_an_existing_approval_keyboard() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        store.upsert_linkedin_approval(
            channel=Channel.TELEGRAM.value,
            action=ACTION_LINKEDIN_COMPOSIO_WRITE,
            risk="R4",
            payload_hash="7" * 64,
            decision=DECISION_PENDING,
            resource_id="li_owner_controls_pending",
            expires_at=approval_expires_at(now=datetime.now(UTC)),
            proposed_parameters=('{"arguments":{"text":"pending"},"slug":"LINKEDIN_POST"}'),
        )
        assert store.list_all_pending_approvals()

        generic_port = CapturingPort()
        _run("מה קורה היום?", store, generic_port)
        assert generic_port.sent
        assert generic_port.sent[0].reply_markup is None

        pending_port = CapturingPort()
        _run("מה מחכה לאישור?", store, pending_port)
        assert pending_port.sent
        assert pending_port.sent[0].reply_markup is not None
    finally:
        db.close()
