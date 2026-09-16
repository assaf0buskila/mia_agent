"""Owner Telegram replies are HTML and attach one-tap approval buttons."""

from datetime import UTC, datetime

import pytest
from app.api.inbound_common import outbound_reply as _outbound_reply
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


@pytest.mark.asyncio
async def test_pending_approvals_owner_turn_sends_digest_then_its_own_card(tmp_path) -> None:
    """The digest is message 0 with no keyboard; the row's own card, WITH its
    keyboard, is message 1 -- an isolated database, so there is exactly one
    pending row and exactly one card. This is the one property that actually
    distinguishes the C2b design from the pre-C2b one, where a single combined
    message carried the digest text and the keyboard together: reverting
    app/surfaces/owner.py (and app/api/inbound_common.py) to their pre-C2b
    content sends exactly one message here, so `len(port.sent) == 2` below
    fails against that revert instead of passing by accident.
    """
    from app.db.base import Base
    from app.db.session import make_engine
    from sqlalchemy.orm import sessionmaker

    engine = make_engine(f"sqlite:///{tmp_path / 'pending-single.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
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
        assert len(port.sent) == 2
        digest, card = port.sent
        assert digest.parse_mode == "HTML"
        assert digest.reply_markup is None
        assert "מחכים לאישור: 1" in digest.text
        assert card.reply_markup == approval_keyboard(approval_token(row.approval_id))
        assert lead_id in card.text
        assert digest.text != card.text
    finally:
        db.close()
        engine.dispose()


@pytest.mark.asyncio
async def test_pending_approvals_zero_rows_sends_only_digest_with_no_keyboard(
    tmp_path,
) -> None:
    """Zero pending rows -> exactly one message (the digest), no keyboard at all.

    Coverage that `test_pending_approvals_markup_skips_empty_and_non_telegram`
    used to pin before it was deleted in the C2b rework: the behaviour
    (`pending_approval_cards` returning no cards short-circuits
    `_pending_approvals_messages` to just the digest) is correct today, but
    nothing asserted it -- an isolated, genuinely empty database, unlike the
    shared test database other tests may have already populated.
    """
    from app.db.base import Base
    from app.db.session import make_engine
    from sqlalchemy.orm import sessionmaker

    engine = make_engine(f"sqlite:///{tmp_path / 'pending-empty.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    try:
        store = LeadStore(db)
        assert store.count_pending_approvals() == 0
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[
                {
                    "id": "evt.owner.apr.empty",
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
        digest = port.sent[0]
        assert digest.reply_markup is None
        assert "אין כרגע" in digest.text
    finally:
        db.close()
        engine.dispose()


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

        # The digest, then 5 proposal cards newest first, then one "ועוד 2" notice --
        # neither the digest nor the trailing notice carries a keyboard.
        assert len(port.sent) == 7
        assert port.sent[0].reply_markup is None
        assert "מחכים לאישור: 7" in port.sent[0].text
        for message in port.sent[1:6]:
            assert message.reply_markup is not None
        assert port.sent[6].reply_markup is None
        assert "ועוד 2" in port.sent[6].text
        assert port.sent[1].reply_markup == approval_keyboard(approval_token(approval_ids[-1]))
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
        # sent[0] is always the digest (no keyboard); this row, just created, is
        # the newest pending proposal and so is always the first card after it,
        # regardless of any other pending row left over elsewhere.
        assert len(pending_port.sent) >= 2
        assert pending_port.sent[0].reply_markup is None
        card = pending_port.sent[1]
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


class _FlakyPort(RecordingMessagePort):
    """Fails exactly one send, by overall attempt order; every other send records."""

    def __init__(self, *, fail_at_attempt: int) -> None:
        super().__init__()
        self._fail_at_attempt = fail_at_attempt
        self._attempts = 0

    async def send(self, message) -> None:
        attempt = self._attempts
        self._attempts += 1
        if attempt == self._fail_at_attempt:
            from app.integrations.telegram import TelegramSendError

            raise TelegramSendError("simulated mid-card failure")
        await super().send(message)


@pytest.mark.asyncio
async def test_middle_chunk_failure_never_delivers_the_approve_button(monkeypatch) -> None:
    """A long card that fails partway through must never still hand out its approve
    button on a chunk that did make it out (P1 in the C2b review: a `FlakyPort`
    failing message index 1 on a multi-chunk gmail draft used to still deliver the
    keyboard-bearing final chunk).
    """
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        long_body = "פסקה ארוכה מאוד. " * 500  # far past the ~3900-char chunk budget
        proposal = propose_owner_action(
            store,
            principal=Principal.owner(source="telegram", actor_id=_OWNER_ID),
            source_ref="tg:flaky-middle-chunk",
            kind="gmail.create_draft",
            parameters={"to": "flaky@example.com", "subject": "Flaky", "body": long_body},
            target={"recipient": "flaky@example.com", "provider_binding": {}},
        )
        db.commit()

        def fake_talk(*, approval_ids_out, **_kwargs):
            approval_ids_out.append(proposal.approval_id)
            return "הכנתי טיוטה.", False

        monkeypatch.setattr("app.surfaces.owner._talk_with_optional_agent", fake_talk)
        # Attempt 0 is the prose (must succeed); attempt 1 is the card's first,
        # non-final chunk -- the same "message index 1" the reviewer's probe used.
        port = _FlakyPort(fail_at_attempt=1)

        await process_inbound_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[
                {"id": "evt.flaky.middle", "from": _OWNER_ID, "text": "prepare a flaky draft"}
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={_OWNER_ID},
        )

        # No delivered message carries a live approve button for this proposal:
        # a partially-shown card must never still hand out its keyboard, even
        # though a later chunk of that same card would otherwise have sent fine.
        target_keyboard = approval_keyboard(approval_token(proposal.approval_id))
        assert not any(message.reply_markup == target_keyboard for message in port.sent)
        row = store.get_approval_by_approval_id(proposal.approval_id)
        assert row is not None
        assert row.decision == DECISION_PENDING
    finally:
        db.close()


@pytest.mark.asyncio
async def test_card_delivered_when_prose_fails_still_marks_webhook_sent(monkeypatch) -> None:
    """C2b review follow-up: when message index 0 (the prose/digest) fails but a
    later card fully sends, the webhook must be marked `sent` (not `processed`) --
    a retry of the same webhook must not re-send a card whose keyboard already
    reached Telegram. The card is complete, so nothing is approved unseen. But
    the MESSAGE_OUT canonical event -- whose `text` is the prose Mia never
    actually managed to say -- must NOT be recorded: `render_transcript` replays
    canonical events as what Mia said on the next owner turn, so recording a
    reply that never reached Assaf would give her a false memory of her own
    output (round-2 review P2 on the first version of this fix, which gated both
    on the same flag).
    """
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        proposal = propose_owner_action(
            store,
            principal=Principal.owner(source="telegram", actor_id=_OWNER_ID),
            source_ref="tg:prose-fails-card-sends",
            kind="gmail.create_draft",
            parameters={"to": "x@example.com", "subject": "Hi", "body": "short body"},
            target={"recipient": "x@example.com", "provider_binding": {}},
        )
        db.commit()

        def fake_talk(*, approval_ids_out, **_kwargs):
            approval_ids_out.append(proposal.approval_id)
            return "הכנתי טיוטה.", False

        monkeypatch.setattr("app.surfaces.owner._talk_with_optional_agent", fake_talk)
        # Attempt 0 is the prose (fails); attempt 1 is the card's only chunk (sends).
        port = _FlakyPort(fail_at_attempt=0)
        item_id = "evt.prose-fails-card-sends"

        await process_inbound_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[{"id": item_id, "from": _OWNER_ID, "text": "prepare a draft"}],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={_OWNER_ID},
        )

        target_keyboard = approval_keyboard(approval_token(proposal.approval_id))
        assert any(message.reply_markup == target_keyboard for message in port.sent)
        webhook = store.get_webhook(provider="telegram", provider_event_id=item_id)
        assert webhook is not None
        assert webhook.status == "sent"
        # No MESSAGE_OUT for the undelivered prose: Mia must not remember saying
        # something Assaf never received.
        outgoing = store.get_canonical_event(
            provider="telegram", provider_event_id=f"{item_id}:out"
        )
        assert outgoing is None
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("should_fail", [False, True])
async def test_send_stage_outcome_reflects_actual_delivery(caplog, should_fail: bool) -> None:
    """`owner_stage("send", ...)` must see a send failure, not just log it as ok
    (P2 in the C2b review: moving the try/except inside the `with` block let a
    swallowed exception exit the stage successfully).
    """
    import logging as logging_module

    class _Port(RecordingMessagePort):
        async def send(self, message) -> None:
            if should_fail:
                from app.integrations.telegram import TelegramSendError

                raise TelegramSendError("simulated failure")
            await super().send(message)

    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        with caplog.at_level(logging_module.INFO, logger="mia.owner_timing"):
            await process_inbound_texts(
                provider="telegram",
                channel=Channel.TELEGRAM,
                items=[
                    {
                        "id": f"evt.stage.outcome.{should_fail}",
                        "from": _OWNER_ID,
                        "text": "מה קורה היום?",
                    }
                ],
                store=store,
                port=_Port(),
                kill_switch=False,
                owner_ids={_OWNER_ID},
            )
        stage_records = [
            record.getMessage()
            for record in caplog.records
            if "owner_stage stage=send" in record.getMessage()
        ]
        assert stage_records
        expected = "outcome=error" if should_fail else "outcome=ok"
        unexpected = "outcome=ok" if should_fail else "outcome=error"
        assert all(expected in message for message in stage_records)
        assert not any(unexpected in message for message in stage_records)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_pending_digest_persisted_and_returned_matches_what_was_sent(tmp_path) -> None:
    """`OwnerTurnResult.last_reply` must equal the text of the message actually
    delivered as index 0 -- never a stale digest the owner did not receive (P2 in
    the C2b review: the digest used to be computed but only ever sent when there
    were zero pending cards, while still being persisted/returned unconditionally).
    """
    from app.core.config import Settings
    from app.db.base import Base
    from app.db.session import make_engine
    from app.integrations.telegram_format import render_owner_markdown
    from app.surfaces.owner import run_owner_loop
    from sqlalchemy.orm import sessionmaker

    engine = make_engine(f"sqlite:///{tmp_path / 'pending-digest-match.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = session_factory()
    try:
        store = LeadStore(db)
        propose_owner_action(
            store,
            principal=Principal.owner(source="telegram", actor_id=_OWNER_ID),
            source_ref="tg:digest-match",
            kind="gmail.create_draft",
            parameters={"to": "match@example.com", "subject": "S", "body": "B"},
            target={"recipient": "match@example.com", "provider_binding": {}},
        )
        db.commit()
        item = {"id": "evt.digest.match", "from": _OWNER_ID, "text": "מה מחכה לאישור?"}
        store.claim_webhook(provider="telegram", provider_event_id=item["id"])
        port = RecordingMessagePort()
        settings = Settings(telegram_owner_user_ids=_OWNER_ID)

        result = await run_owner_loop(
            item=item,
            store=store,
            port=port,
            settings=settings,
            owner_ids={_OWNER_ID},
        )
        db.commit()

        assert result.sent is True
        assert len(port.sent) == 2  # digest, then the one card
        assert render_owner_markdown(result.last_reply) == port.sent[0].text
    finally:
        db.close()
        engine.dispose()
