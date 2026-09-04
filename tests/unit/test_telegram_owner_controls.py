"""Assaf must be able to release a parked lead and approve an action from Telegram.

Both controls existed only on the WhatsApp owner path, which is off. So a
conversation Mia escalated stayed parked forever, and any approval she proposed had
no button and no text command — `pending_approvals` could only grow.
"""

from __future__ import annotations

import asyncio

from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.owner_tasks import OwnerTaskType, classify_owner_task
from app.integrations.base import OutboundMessage
from app.surfaces.crm import DisabledContactsCrm
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
            crm=DisabledContactsCrm(spreadsheet_id=""),
            owner_ids={OWNER},
            talk=lambda *, text, crm: ("ok", False),
        )
    )


def test_release_phrases_are_recognised_as_a_resume() -> None:
    for text in ("release this lead lead_abc123def456", "שחרר את הליד lead_abc123def456"):
        assert (
            classify_owner_task(text).task_type is OwnerTaskType.HUMAN_TAKEOVER_RESUME
        ), text


def test_releasing_a_parked_lead_from_telegram_hands_it_back_to_mia() -> None:
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

        # The whole point: Mia can speak to this person again.
        assert store.is_human_takeover(lead_id) is False
        assert port.sent, "the owner should get an acknowledgement"
    finally:
        db.close()


def test_taking_over_from_telegram_stops_mia_replying() -> None:
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
        assert store.is_human_takeover(lead_id) is True
    finally:
        db.close()


def test_the_keyboard_tracks_whether_anything_is_actually_pending() -> None:
    """Order-independent: other tests share this DB and may leave approvals behind."""
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = CapturingPort()
        _run("מה קורה היום?", store, port)
        assert port.sent
        has_pending = bool(store.list_all_pending_approvals())
        has_buttons = bool(port.sent[0].reply_markup)
        assert has_buttons == has_pending, (
            "buttons must appear exactly when something is waiting on him"
        )
    finally:
        db.close()
