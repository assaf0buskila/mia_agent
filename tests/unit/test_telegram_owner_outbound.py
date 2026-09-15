"""Owner Telegram replies are HTML and attach one-tap approval buttons."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.api.inbound_common import (
    outbound_reply as _outbound_reply,
)
from app.api.inbound_common import (
    owner_telegram_reply_markup as _owner_telegram_reply_markup,
)
from app.api.owner import process_owner_texts as process_inbound_texts
from app.capabilities.types import Principal
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
from app.domain.owner.callbacks import approval_token
from app.domain.owner.tasks import OwnerTaskType
from app.integrations.base import RecordingMessagePort
from app.integrations.gmail import FakeGmailPort
from app.integrations.telegram_format import approval_keyboard
from app.services.owner_actions import propose_owner_action

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


def test_linkedin_proposal_markup_binds_the_exact_new_approval_id() -> None:
    markup = _owner_telegram_reply_markup(
        SimpleNamespace(),  # type: ignore[arg-type]
        channel=Channel.TELEGRAM,
        task_type=OwnerTaskType.NOTE,
        turn_approval_id="apr_linkedin_exact",
    )
    assert markup == approval_keyboard(approval_token("apr_linkedin_exact"))


def test_store_keeps_long_linkedin_payload_exact() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        parameters = '{"arguments":{"text":"' + "x" * 1_000 + '"},"slug":"LINKEDIN_POST_UPDATE"}'
        store.upsert_linkedin_approval(
            channel=Channel.TELEGRAM.value,
            action="linkedin_composio_write",
            risk="R4",
            payload_hash="a" * 64,
            decision=DECISION_PENDING,
            resource_id="li_long_parameters",
            expires_at=approval_expires_at(now=datetime.now(UTC)),
            proposed_parameters=parameters,
        )
        row = store.get_approval_by_resource(
            "linkedin_tool", "li_long_parameters", "linkedin_composio_write"
        )
        assert row is not None
        assert row.proposed_parameters == parameters
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_turn_approval", [True, False])
@pytest.mark.parametrize(
    "tool_name",
    [
        "composio_propose_linkedin_action",
        "calendar_create_meeting",
        "gmail_create_draft",
    ],
)
async def test_proposal_turn_keyboard_ignores_an_unrelated_newer_approval(
    monkeypatch,
    tool_name,
    has_turn_approval,
) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        common = {
            "channel": Channel.TELEGRAM.value,
            "action": "linkedin_composio_write",
            "risk": "R4",
            "decision": DECISION_PENDING,
            "expires_at": approval_expires_at(now=datetime.now(UTC)),
        }
        store.upsert_linkedin_approval(
            **common,
            payload_hash="1" * 64,
            resource_id="li_exact_turn",
            proposed_parameters='{"arguments":{"text":"exact"},"slug":"LINKEDIN_POST"}',
        )
        exact = store.get_approval_by_resource(
            "linkedin_tool", "li_exact_turn", "linkedin_composio_write"
        )
        assert exact is not None
        store.upsert_linkedin_approval(
            **common,
            payload_hash="2" * 64,
            resource_id="li_unrelated_newer",
            proposed_parameters='{"arguments":{"text":"other"},"slug":"LINKEDIN_POST"}',
        )
        unrelated = store.get_approval_by_resource(
            "linkedin_tool", "li_unrelated_newer", "linkedin_composio_write"
        )
        assert unrelated is not None
        db.commit()

        def fake_talk(*, approval_ids_out, **_kwargs):
            if has_turn_approval:
                approval_ids_out.append(exact.approval_id)
            return "LinkedIn action is ready for approval.", False

        monkeypatch.setattr("app.surfaces.owner._talk_with_optional_agent", fake_talk)
        port = RecordingMessagePort()

        await process_inbound_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[
                {
                    "id": "evt.exact.turn.keyboard." + tool_name,
                    "from": _OWNER_ID,
                    "text": "prepare a linkedin post for approval",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={_OWNER_ID},
        )

        # The prose reply never carries a keyboard; a turn-created proposal gets its
        # own follow-up card message with its own button instead (C2b).
        if has_turn_approval:
            assert len(port.sent) == 2
            assert port.sent[0].reply_markup is None
            assert port.sent[-1].reply_markup == approval_keyboard(
                approval_token(exact.approval_id)
            )
        else:
            assert len(port.sent) == 1
            assert port.sent[0].reply_markup is None
        for message in port.sent:
            assert message.reply_markup != approval_keyboard(approval_token(unrelated.approval_id))
    finally:
        db.close()


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
        # Every pending proposal is its own card message (C2b), newest first, with
        # no separate leading digest -- so this row (just created) is always the
        # first message, regardless of any other pending row left over elsewhere.
        assert port.sent
        sent = port.sent[0]
        assert sent.parse_mode == "HTML"
        assert sent.reply_markup == approval_keyboard(approval_token(row.approval_id))
        assert "לאישור" in sent.text
        assert lead_id in sent.text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_file_sqlite_crm_callback_syncs_before_decision_and_executes_once(
    monkeypatch, tmp_path
) -> None:
    """The callback uses one transaction, so file SQLite cannot lock itself."""
    from app.api import telegram as telegram_api
    from app.capabilities.types import Principal
    from app.core.config import Settings
    from app.db.base import Base
    from app.db.session import make_engine
    from app.integrations import sheets as sheets_integration
    from app.integrations.sheets import FakeSheetsPort
    from app.services import owner_actions as owner_actions_service
    from app.services.crm_v2 import CrmService
    from app.services.owner_actions import propose_owner_action
    from sqlalchemy.orm import sessionmaker

    engine = make_engine(f"sqlite:///{tmp_path / 'crm-callback.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    try:
        settings = Settings(
            telegram_owner_user_ids=_OWNER_ID,
            crm_v2_enabled=True,
        )
        store = LeadStore(db)
        fields = {"name": "Callback Contact", "email": "callback@example.com"}
        snapshot = CrmService(db).snapshot_identity(fields)
        proposal = propose_owner_action(
            store,
            principal=Principal.owner(source="telegram", actor_id=_OWNER_ID),
            source_ref="telegram:file-sqlite-crm-callback",
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
        sheets_port = FakeSheetsPort()
        monkeypatch.setattr(telegram_api, "build_sheets_port", lambda _settings: sheets_port)
        monkeypatch.setattr(sheets_integration, "build_sheets_port", lambda _settings: sheets_port)
        outcomes = []
        original_execute = owner_actions_service.execute_approved_owner_action_with_adapters

        def capture_outcome(*args, **kwargs):
            outcome = original_execute(*args, **kwargs)
            outcomes.append(outcome)
            return outcome

        monkeypatch.setattr(
            owner_actions_service,
            "execute_approved_owner_action_with_adapters",
            capture_outcome,
        )
        port = _CallbackPort()
        callback = {
            "callback_query_id": "q-crm-file",
            "from": _OWNER_ID,
            "data": f"ok:{approval_token(proposal.approval_id)}",
            "chat_id": _OWNER_ID,
            "message_id": "44",
        }

        first = await telegram_api._handle_callback(
            callback=callback,
            port=port,
            owner_ids={_OWNER_ID},
            db=db,
            settings=settings,
        )
        db.commit()
        second = await telegram_api._handle_callback(
            callback={**callback, "callback_query_id": "q-crm-file-replay"},
            port=port,
            owner_ids={_OWNER_ID},
            db=db,
            settings=settings,
        )
        db.commit()

        assert first["processed"] == second["processed"] == 1
        assert outcomes[0].status == "executed", outcomes
        assert outcomes[1].status == "already_handled", outcomes
        assert "CRM contact" in port.edited[0]["text"]
        contacts = CrmService(db).lookup(query="callback@example.com")
        assert len(contacts) == 1
        assert contacts[0].revision == 1
    finally:
        db.close()
        engine.dispose()


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


def test_gmail_callback_defers_then_never_replays_an_ambiguous_send(monkeypatch) -> None:
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
        row = store.get_approval_by_resource(RESOURCE_GMAIL, draft_id, ACTION_GMAIL_SEND)
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
            assert gmail_port.sent_drafts == []
            assert send_attempts == 1
            assert "השליחה כבויה" in telegram_port.edited[0]["text"]
            assert "תוצאת השליחה אינה ודאית" in telegram_port.edited[1]["text"]
            assert "ממתינה לבדיקה" in telegram_port.edited[2]["text"]
            assert "ממתינה לבדיקה" in telegram_port.edited[3]["text"]
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
        row = store.get_approval_by_resource(RESOURCE_GMAIL, draft_id, ACTION_GMAIL_SEND)
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
    from app.domain.owner.callbacks import resolve_owner_callback

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


@pytest.mark.asyncio
async def test_two_turn_proposals_get_prose_then_own_cards(monkeypatch) -> None:
    """Each proposal created this turn gets its own follow-up card and keyboard (C2b)."""
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        first = propose_owner_action(
            store,
            principal=Principal.owner(source="telegram", actor_id=_OWNER_ID),
            source_ref="tg:two-cards:1",
            kind="gmail.create_draft",
            parameters={"to": "one@example.com", "subject": "First", "body": "First body"},
            target={"recipient": "one@example.com", "provider_binding": {}},
        )
        second = propose_owner_action(
            store,
            principal=Principal.owner(source="telegram", actor_id=_OWNER_ID),
            source_ref="tg:two-cards:2",
            kind="gmail.create_draft",
            parameters={"to": "two@example.com", "subject": "Second", "body": "Second body"},
            target={"recipient": "two@example.com", "provider_binding": {}},
        )
        db.commit()

        def fake_talk(*, approval_ids_out, **_kwargs):
            approval_ids_out.extend([first.approval_id, second.approval_id])
            return "הכנתי שתי טיוטות מייל.", False

        monkeypatch.setattr("app.surfaces.owner._talk_with_optional_agent", fake_talk)
        port = RecordingMessagePort()

        await process_inbound_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[{"id": "evt.two.cards", "from": _OWNER_ID, "text": "prepare two drafts"}],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={_OWNER_ID},
        )

        assert len(port.sent) == 3
        assert port.sent[0].reply_markup is None
        assert "הכנתי שתי טיוטות" in port.sent[0].text
        assert port.sent[1].reply_markup == approval_keyboard(approval_token(first.approval_id))
        assert "one@example.com" in port.sent[1].text
        assert port.sent[2].reply_markup == approval_keyboard(approval_token(second.approval_id))
        assert "two@example.com" in port.sent[2].text
        assert port.sent[1].text != port.sent[2].text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_long_card_splits_with_keyboard_only_on_last_chunk(monkeypatch) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        long_body = "פסקה ארוכה מאוד. " * 500  # far past the ~3900-char chunk budget
        proposal = propose_owner_action(
            store,
            principal=Principal.owner(source="telegram", actor_id=_OWNER_ID),
            source_ref="tg:long-card",
            kind="gmail.create_draft",
            parameters={"to": "long@example.com", "subject": "Long", "body": long_body},
            target={"recipient": "long@example.com", "provider_binding": {}},
        )
        db.commit()

        def fake_talk(*, approval_ids_out, **_kwargs):
            approval_ids_out.append(proposal.approval_id)
            return "הכנתי טיוטה ארוכה.", False

        monkeypatch.setattr("app.surfaces.owner._talk_with_optional_agent", fake_talk)
        port = RecordingMessagePort()

        await process_inbound_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[{"id": "evt.long.card", "from": _OWNER_ID, "text": "prepare a long draft"}],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={_OWNER_ID},
        )

        # prose, then the card split across at least two chunks.
        assert len(port.sent) >= 3
        card_chunks = port.sent[1:]
        assert len(card_chunks) >= 2
        for chunk in card_chunks[:-1]:
            assert chunk.reply_markup is None
        assert card_chunks[-1].reply_markup == approval_keyboard(
            approval_token(proposal.approval_id)
        )
        for message in port.sent:
            assert len(message.text) <= 4096
    finally:
        db.close()


@pytest.mark.asyncio
async def test_pending_view_caps_at_five_with_more_notice(tmp_path) -> None:
    from app.db.base import Base
    from app.db.session import make_engine
    from sqlalchemy.orm import sessionmaker

    engine = make_engine(f"sqlite:///{tmp_path / 'pending-cap.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    try:
        store = LeadStore(db)
        approval_ids = []
        for index in range(7):
            proposal = propose_owner_action(
                store,
                principal=Principal.owner(source="telegram", actor_id=_OWNER_ID),
                source_ref=f"tg:pending-cap:{index}",
                kind="gmail.create_draft",
                parameters={
                    "to": f"contact{index}@example.com",
                    "subject": f"Draft {index}",
                    "body": "body",
                },
                target={
                    "recipient": f"contact{index}@example.com",
                    "provider_binding": {},
                },
            )
            approval_ids.append(proposal.approval_id)
        db.commit()

        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[{"id": "evt.pending.cap", "from": _OWNER_ID, "text": "מה מחכה לאישור?"}],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={_OWNER_ID},
        )

        # 5 proposal cards, newest first, plus one "ועוד 2" notice with no keyboard.
        assert len(port.sent) == 6
        for message in port.sent[:5]:
            assert message.reply_markup is not None
        assert port.sent[5].reply_markup is None
        assert "ועוד 2" in port.sent[5].text
        assert port.sent[0].reply_markup == approval_keyboard(approval_token(approval_ids[-1]))
    finally:
        db.close()
        engine.dispose()


class _FailingEditPort(RecordingMessagePort):
    """An edit that always fails, so every callback must fall back to sendMessage."""

    def __init__(self) -> None:
        super().__init__()
        self.edit_attempts = 0

    async def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> None:
        del callback_query_id, text

    async def edit_message_text(self, **kwargs) -> None:  # noqa: ANN003
        del kwargs
        self.edit_attempts += 1
        from app.integrations.telegram import TelegramSendError

        raise TelegramSendError("edit failed")


@pytest.mark.asyncio
async def test_callback_edit_failure_falls_back_and_never_reruns_the_action(
    monkeypatch,
) -> None:
    from app.api import telegram as telegram_api
    from app.core.config import Settings
    from app.integrations.sheets import FakeSheetsPort
    from app.services.crm_v2 import CrmService

    init_db()
    db = get_session_factory()()
    try:
        settings = Settings(telegram_owner_user_ids=_OWNER_ID, crm_v2_enabled=True)
        store = LeadStore(db)
        fields = {"name": "Fallback Contact", "email": "fallback@example.com"}
        snapshot = CrmService(db).snapshot_identity(fields)
        proposal = propose_owner_action(
            store,
            principal=Principal.owner(source="telegram", actor_id=_OWNER_ID),
            source_ref="telegram:edit-fallback",
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
        sheets_port = FakeSheetsPort()
        monkeypatch.setattr(telegram_api, "build_sheets_port", lambda _settings: sheets_port)
        port = _FailingEditPort()
        callback = {
            "callback_query_id": "q-edit-fail",
            "from": _OWNER_ID,
            "data": f"ok:{approval_token(proposal.approval_id)}",
            "chat_id": _OWNER_ID,
            "message_id": "77",
        }

        result = await telegram_api._handle_callback(
            callback=callback,
            port=port,
            owner_ids={_OWNER_ID},
            db=db,
            settings=settings,
        )
        db.commit()

        assert result["sent"] is False
        assert port.edit_attempts == 1
        assert len(port.sent) == 1
        assert "CRM contact" in port.sent[0].text
        assert port.sent[0].parse_mode == "HTML"
        contacts = CrmService(db).lookup(query="fallback@example.com")
        assert len(contacts) == 1
        assert contacts[0].revision == 1

        # A replay (edit still failing) must not execute the write a second time.
        replay = await telegram_api._handle_callback(
            callback={**callback, "callback_query_id": "q-edit-fail-replay"},
            port=port,
            owner_ids={_OWNER_ID},
            db=db,
            settings=settings,
        )
        db.commit()

        assert replay["sent"] is False
        assert len(port.sent) == 2
        contacts_after = CrmService(db).lookup(query="fallback@example.com")
        assert len(contacts_after) == 1
        assert contacts_after[0].revision == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_legacy_pending_row_renders_as_a_card_and_still_resolves(monkeypatch) -> None:
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
            channel=Channel.WEBSITE, external_id="web_legacy_card_resolve"
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

        # Render: the pending view surfaces this pre-v2 row as its own card.
        pending_port = RecordingMessagePort()
        await process_inbound_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[{"id": "evt.legacy.render", "from": _OWNER_ID, "text": "מה מחכה לאישור?"}],
            store=store,
            port=pending_port,
            kill_switch=False,
            owner_ids={_OWNER_ID},
        )
        assert pending_port.sent
        card = pending_port.sent[0]
        assert card.reply_markup == approval_keyboard(approval_token(row.approval_id))
        assert lead_id in card.text
        assert "העברת ליד" in card.text

        # Resolve: the same exact row still decides through the callback path.
        callback_port = _CallbackPort()
        app.dependency_overrides[get_telegram_port] = lambda: callback_port
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/v1/telegram/webhook",
                    json={
                        "update_id": 501,
                        "callback_query": {
                            "id": "q-legacy-resolve",
                            "from": {"id": int(_OWNER_ID)},
                            "data": f"ok:{approval_token(row.approval_id)}",
                            "message": {"message_id": 44, "chat": {"id": int(_OWNER_ID)}},
                        },
                    },
                    headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
                )
            assert response.status_code == 200
            assert response.json()["processed"] == 1
        finally:
            app.dependency_overrides.pop(get_telegram_port, None)
        db.expire_all()
        refreshed = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert refreshed is not None
        assert refreshed.decision == DECISION_APPROVED
    finally:
        db.close()
