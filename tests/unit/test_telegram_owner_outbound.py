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
    ACTION_GMAIL_SEND,
    ACTION_PROPOSAL_HANDOFF,
    DECISION_APPROVED,
    DECISION_PENDING,
    RESOURCE_GMAIL,
    RESOURCE_LEAD,
    RISK_R3,
    approval_expires_at,
    payload_hash,
)
from app.domain.events import Channel
from app.domain.owner_callbacks import approval_token
from app.domain.owner_tasks import OwnerTaskType
from app.integrations.base import RecordingMessagePort
from app.integrations.gmail import FakeGmailPort
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


def _callback_update(*, update_id: int, query_id: str, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": query_id,
            "from": {"id": int(_OWNER_ID)},
            "data": data,
            "message": {"message_id": 44, "chat": {"id": int(_OWNER_ID)}},
        },
    }


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
            payload_hash=payload_hash(
                action=ACTION_PROPOSAL_HANDOFF,
                risk=RISK_R3,
                channel=Channel.WEBSITE.value,
                resource_type=RESOURCE_LEAD,
                resource_id=lead_id,
            ),
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


def test_gmail_callback_recovers_deferred_and_failed_send_once(monkeypatch) -> None:
    from uuid import uuid4

    from app.api import telegram as telegram_api
    from app.api.deps import get_telegram_port
    from app.main import app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MIA_TELEGRAM_WEBHOOK_SECRET", "tg-secret")
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", _OWNER_ID)
    monkeypatch.setenv("MIA_TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("MIA_GMAIL_SEND", "false")
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        gmail_port = FakeGmailPort()
        draft = gmail_port.create_draft(
            to="owner@example.com",
            subject="Callback approval",
            body="test",
        )
        assert draft is not None
        draft = draft.model_copy(update={"draft_id": f"draft-callback-{uuid4().hex[:12]}"})
        gmail_port.created_drafts[-1] = draft
        draft_id = draft.draft_id
        store.upsert_gmail_approval(
            channel=Channel.TELEGRAM.value,
            action=ACTION_GMAIL_SEND,
            risk=RISK_R3,
            payload_hash=payload_hash(
                action=ACTION_GMAIL_SEND,
                risk=RISK_R3,
                channel=Channel.TELEGRAM.value,
                resource_type=RESOURCE_GMAIL,
                resource_id=draft_id,
            ),
            decision=DECISION_PENDING,
            resource_type=RESOURCE_GMAIL,
            resource_id=draft_id,
            expires_at=approval_expires_at(now=datetime.now(UTC)),
        )
        db.commit()
        row = store.get_approval_by_resource(
            RESOURCE_GMAIL, draft_id, ACTION_GMAIL_SEND
        )
        assert row is not None
        telegram_port = _CallbackPort()
        original_send = gmail_port.send_draft
        send_attempts = 0

        def fail_once(draft_id: str) -> bool:
            nonlocal send_attempts
            send_attempts += 1
            return send_attempts > 1 and original_send(draft_id)

        monkeypatch.setattr(gmail_port, "send_draft", fail_once)
        app.dependency_overrides[get_telegram_port] = lambda: telegram_port
        monkeypatch.setattr(telegram_api, "build_gmail_port", lambda _settings: gmail_port)
        try:
            with TestClient(app) as client:
                first = client.post(
                    "/v1/telegram/webhook",
                    json=_callback_update(
                        update_id=89,
                        query_id="q-gmail-1",
                        data=f"ok:{approval_token(row.approval_id)}",
                    ),
                    headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
                )
                monkeypatch.setenv("MIA_GMAIL_SEND", "true")
                failed_replay = client.post(
                    "/v1/telegram/webhook",
                    json=_callback_update(
                        update_id=90,
                        query_id="q-gmail-2",
                        data=f"ok:{approval_token(row.approval_id)}",
                    ),
                    headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
                )
                sent_replay = client.post(
                    "/v1/telegram/webhook",
                    json=_callback_update(
                        update_id=91,
                        query_id="q-gmail-3",
                        data=f"ok:{approval_token(row.approval_id)}",
                    ),
                    headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
                )
                completed_replay = client.post(
                    "/v1/telegram/webhook",
                    json=_callback_update(
                        update_id=92,
                        query_id="q-gmail-4",
                        data=f"ok:{approval_token(row.approval_id)}",
                    ),
                    headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
                )
            assert first.status_code == 200
            assert failed_replay.status_code == 200
            assert sent_replay.status_code == 200
            assert completed_replay.status_code == 200
            assert gmail_port.sent_drafts == [draft_id]
            assert send_attempts == 2
            assert "השליחה כבויה" in telegram_port.edited[0]["text"]
            assert "השליחה נכשלה" in telegram_port.edited[1]["text"]
            assert "שלחתי" in telegram_port.edited[2]["text"]
            assert "כבר טופלה" in telegram_port.edited[3]["text"]
        finally:
            app.dependency_overrides.pop(get_telegram_port, None)
    finally:
        db.close()


@pytest.mark.parametrize("bad_field", ["payload_hash", "expires_at"])
def test_gmail_invalid_callback_never_sends(monkeypatch, bad_field: str) -> None:
    from app.api import telegram as telegram_api
    from app.api.deps import get_telegram_port
    from app.main import app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MIA_TELEGRAM_WEBHOOK_SECRET", "tg-secret")
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", _OWNER_ID)
    monkeypatch.setenv("MIA_TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("MIA_GMAIL_SEND", "true")
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        draft_id = "draft_callback_tampered_1"
        store.upsert_gmail_approval(
            channel=Channel.TELEGRAM.value,
            action=ACTION_GMAIL_SEND,
            risk=RISK_R3,
            payload_hash=payload_hash(
                action=ACTION_GMAIL_SEND,
                risk=RISK_R3,
                channel=Channel.TELEGRAM.value,
                resource_type=RESOURCE_GMAIL,
                resource_id=draft_id,
            ),
            decision=DECISION_PENDING,
            resource_type=RESOURCE_GMAIL,
            resource_id=draft_id,
            expires_at=approval_expires_at(now=datetime.now(UTC)),
        )
        db.commit()
        row = store.get_approval_by_resource(
            RESOURCE_GMAIL, draft_id, ACTION_GMAIL_SEND
        )
        assert row is not None
        if bad_field == "payload_hash":
            row.payload_hash = "x" * 64
        else:
            row.expires_at = "2020-01-01T00:00:00+00:00"
        db.commit()
        telegram_port = _CallbackPort()
        gmail_port = FakeGmailPort()
        app.dependency_overrides[get_telegram_port] = lambda: telegram_port
        monkeypatch.setattr(telegram_api, "build_gmail_port", lambda _settings: gmail_port)
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/v1/telegram/webhook",
                    json=_callback_update(
                        update_id=91,
                        query_id="q-gmail-tampered",
                        data=f"ok:{approval_token(row.approval_id)}",
                    ),
                    headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
                )
            assert response.status_code == 200
            assert gmail_port.sent_drafts == []
            assert "אינו תקף" in telegram_port.edited[0]["text"]
        finally:
            app.dependency_overrides.pop(get_telegram_port, None)
    finally:
        db.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("risk", "R4"),
        ("resource_type", "gmail"),
        ("resource_id", "lead_other"),
        ("payload_hash", "x" * 64),
    ],
)
def test_approval_keyboard_callback_rejects_misbinding(field: str, value: str) -> None:
    from app.domain.owner_callbacks import resolve_owner_callback

    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE,
            external_id=f"web_callback_{field}",
        )
        store.upsert_approval(
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            action=ACTION_PROPOSAL_HANDOFF,
            risk=RISK_R3,
            payload_hash=payload_hash(
                action=ACTION_PROPOSAL_HANDOFF,
                risk=RISK_R3,
                channel=Channel.WEBSITE.value,
                resource_type=RESOURCE_LEAD,
                resource_id=lead_id,
            ),
            decision=DECISION_PENDING,
            resource_type=RESOURCE_LEAD,
            resource_id=lead_id,
            expires_at=approval_expires_at(now=datetime.now(UTC)),
        )
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        setattr(row, field, value)
        result = resolve_owner_callback(store, decision="approve", token=row.approval_id)
        assert "אינו תקף" in result
        assert row.decision == DECISION_PENDING
    finally:
        db.close()
