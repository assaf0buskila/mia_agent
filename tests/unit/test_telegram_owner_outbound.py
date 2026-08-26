"""Owner Telegram replies are HTML and attach one-tap approval buttons."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.api.inbound import process_inbound_texts
from app.api.inbound_common import (
    outbound_reply as _outbound_reply,
)
from app.api.inbound_common import (
    owner_telegram_reply_markup as _owner_telegram_reply_markup,
)
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import (
    ACTION_PROPOSAL_HANDOFF,
    DECISION_APPROVED,
    DECISION_PENDING,
    RESOURCE_LEAD,
    RISK_R3,
    approval_expires_at,
)
from app.domain.events import Channel
from app.domain.owner_callbacks import approval_token
from app.domain.owner_tasks import OwnerTaskType
from app.integrations.base import RecordingMessagePort
from app.integrations.telegram_format import approval_keyboard

_OWNER_ID = "700100240"


def test_telegram_owner_reply_is_html_escaped() -> None:
    message = _outbound_reply(
        {"id": "evt.1", "from": _OWNER_ID, "message_id": "99"},
        text="a & b < c",
        channel=Channel.TELEGRAM,
    )
    assert message.parse_mode == "HTML"
    assert message.text == "a &amp; b &lt; c"
    assert message.reply_to_id == "99"


def test_whatsapp_prospect_reply_stays_plain_text() -> None:
    message = _outbound_reply(
        {"id": "evt.wa", "from": "972501111111"},
        text="a & b",
        channel=Channel.WHATSAPP,
    )
    assert message.parse_mode is None
    assert message.reply_markup is None
    assert message.text == "a & b"


def test_pending_approvals_markup_uses_first_approval_id() -> None:
    store = SimpleNamespace(
        list_all_pending_approvals=lambda: [
            SimpleNamespace(approval_id="apr_abc123def456"),
            SimpleNamespace(approval_id="apr_fff000111222"),
        ]
    )
    markup = _owner_telegram_reply_markup(
        store,  # type: ignore[arg-type]
        channel=Channel.TELEGRAM,
        task_type=OwnerTaskType.PENDING_APPROVALS,
    )
    assert markup == approval_keyboard(approval_token("apr_abc123def456"))


def test_pending_approvals_markup_skips_empty_and_non_telegram() -> None:
    empty = SimpleNamespace(list_all_pending_approvals=lambda: [])
    assert (
        _owner_telegram_reply_markup(
            empty,  # type: ignore[arg-type]
            channel=Channel.TELEGRAM,
            task_type=OwnerTaskType.PENDING_APPROVALS,
        )
        is None
    )
    pending = SimpleNamespace(
        list_all_pending_approvals=lambda: [SimpleNamespace(approval_id="apr_abc123def456")]
    )
    assert (
        _owner_telegram_reply_markup(
            pending,  # type: ignore[arg-type]
            channel=Channel.TELEGRAM,
            task_type=OwnerTaskType.DAILY_BRIEF,
        )
        is None
    )


@pytest.mark.asyncio
async def test_pending_approvals_owner_turn_attaches_keyboard() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_apr_keyboard_1"
        )
        store.upsert_approval(
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            action=ACTION_PROPOSAL_HANDOFF,
            risk=RISK_R3,
            payload_hash="b" * 64,
            decision=DECISION_PENDING,
            resource_type=RESOURCE_LEAD,
            resource_id=lead_id,
            expires_at=approval_expires_at(now=datetime.now(UTC)),
        )
        db.commit()
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[
                {
                    "id": "evt.owner.apr.keyboard",
                    "from": _OWNER_ID,
                    "text": "מה מחכה לאישור?",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={_OWNER_ID},
        )
        assert len(port.sent) == 1
        sent = port.sent[0]
        assert sent.parse_mode == "HTML"
        assert sent.reply_markup == approval_keyboard(approval_token(row.approval_id))
        assert "מחכים לאישור" in sent.text
    finally:
        db.close()


class _CallbackPort(RecordingMessagePort):
    def __init__(self) -> None:
        super().__init__()
        self.answered: list[str] = []
        self.edited: list[dict[str, str]] = []

    async def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> None:
        self.answered.append(callback_query_id)

    async def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: str,
        text: str,
        parse_mode: str | None = None,
        clear_markup: bool = True,
    ) -> None:
        self.edited.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode or "",
            }
        )


def test_approval_keyboard_callback_applies_the_decision(monkeypatch) -> None:
    """The buttons are not decorative: a tap decides the pending approval."""
    from app.api.deps import get_telegram_port
    from app.main import app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MIA_TELEGRAM_WEBHOOK_SECRET", "tg-secret")
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", _OWNER_ID)
    monkeypatch.setenv("MIA_TELEGRAM_BOT_TOKEN", "bot-token")
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_apr_callback_1"
        )
        store.upsert_approval(
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            action=ACTION_PROPOSAL_HANDOFF,
            risk=RISK_R3,
            payload_hash="c" * 64,
            decision=DECISION_PENDING,
            resource_type=RESOURCE_LEAD,
            resource_id=lead_id,
            expires_at=approval_expires_at(now=datetime.now(UTC)),
        )
        db.commit()
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        token = approval_token(row.approval_id)
        port = _CallbackPort()
        app.dependency_overrides[get_telegram_port] = lambda: port
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/v1/telegram/webhook",
                    json={
                        "update_id": 88,
                        "callback_query": {
                            "id": "q-apr-1",
                            "from": {"id": int(_OWNER_ID)},
                            "data": f"ok:{token}",
                            "message": {
                                "message_id": 44,
                                "chat": {"id": int(_OWNER_ID)},
                            },
                        },
                    },
                    headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
                )
            assert response.status_code == 200
            assert response.json()["processed"] == 1
            assert port.answered == ["q-apr-1"]
            assert port.edited
            assert "אושר" in port.edited[0]["text"]
        finally:
            app.dependency_overrides.pop(get_telegram_port, None)
        db.expire_all()
        refreshed = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert refreshed is not None
        assert refreshed.decision == DECISION_APPROVED
    finally:
        db.close()
