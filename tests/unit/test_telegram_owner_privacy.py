from __future__ import annotations

from copy import deepcopy

import pytest
from app.api import deps
from app.api import telegram as telegram_api
from app.api.deps import get_telegram_port, get_transcription_port
from app.core.config import Settings
from app.db.store import LeadStore
from app.integrations import telegram as telegram_integration
from app.integrations.base import RecordingMessagePort
from app.integrations.transcribe import FakeTranscriptionPort
from app.main import app
from app.workers import telegram_owner
from fastapi import FastAPI
from fastapi.testclient import TestClient

OWNER = "551122"
OTHER_OWNER = "661133"
SECRET = "telegram-owner-privacy-secret"


def _message_update(kind: str = "message") -> dict:
    return {
        "update_id": 99101,
        kind: {
            "message_id": 99102,
            "from": {"id": int(OWNER)},
            "chat": {"id": int(OWNER), "type": "private"},
            "text": "שלום",
        },
    }


def _callback_update(data: str = "ok:apr_1") -> dict:
    return {
        "update_id": 99103,
        "callback_query": {
            "id": "callback-privacy",
            "from": {"id": int(OWNER)},
            "data": data,
            "message": {
                "message_id": 99104,
                "chat": {"id": int(OWNER), "type": "private"},
            },
        },
    }


def _configure(monkeypatch) -> None:
    monkeypatch.setenv("MIA_TELEGRAM_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", f"{OWNER},{OTHER_OWNER}")
    monkeypatch.setenv("MIA_TELEGRAM_BOT_TOKEN", "test-token")


def _post(payload: dict, port: RecordingMessagePort) -> dict:
    app.dependency_overrides[get_telegram_port] = lambda: port
    try:
        with TestClient(app) as client:
            response = client.post(
                "/v1/telegram/webhook",
                json=payload,
                headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
            )
        assert response.status_code == 200
        return response.json()
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)


def _forbidden(message: str):
    raise AssertionError(message)


def _nonprivate_payload(kind: str, chat_type: str | None) -> dict:
    if kind in {"callback_approve", "callback_reject", "callback_lead_path"}:
        data = {
            "callback_approve": "ok:apr_1",
            "callback_reject": "no:apr_1",
            "callback_lead_path": "lp:l:apr_1",
        }[kind]
        payload = _callback_update(data)
        chat = {"id": -100991 if chat_type in {"group", "supergroup", "channel"} else int(OWNER)}
        if chat_type is not None:
            chat["type"] = chat_type
        payload["callback_query"]["message"]["chat"] = chat
        return payload
    envelope = "edited_message" if kind == "edited_text" else "message"
    payload = _message_update(envelope)
    message = payload[envelope]
    chat = {"id": -100991 if chat_type in {"group", "supergroup", "channel"} else int(OWNER)}
    if chat_type is not None:
        chat["type"] = chat_type
    message["chat"] = chat
    if kind == "voice":
        message.pop("text")
        message["voice"] = {"file_id": "voice-private", "mime_type": "audio/ogg"}
    elif kind == "photo":
        message.pop("text")
        message["photo"] = [{"file_id": "photo-private"}]
    return payload


def _rejected_http_payload(kind: str) -> dict:
    if kind in {
        "callback_approve",
        "callback_reject",
        "callback_lead_path",
        "callback_unrecognized",
    }:
        data = {
            "callback_approve": "ok:apr_1",
            "callback_reject": "no:apr_1",
            "callback_lead_path": "lp:a:apr_1",
            "callback_unrecognized": "invalid",
        }[kind]
        return _callback_update(data)
    envelope = "edited_message" if kind == "edited_text" else "message"
    payload = _message_update(envelope)
    message = payload[envelope]
    if kind == "voice":
        message.pop("text")
        message["voice"] = {"file_id": "voice-private", "mime_type": "audio/ogg"}
    elif kind == "photo":
        message.pop("text")
        message["photo"] = [{"file_id": "photo-private"}]
    return payload


@pytest.mark.parametrize(
    "kind",
    [
        "text",
        "edited_text",
        "voice",
        "photo",
        "callback_approve",
        "callback_reject",
        "callback_lead_path",
        "callback_unrecognized",
    ],
)
@pytest.mark.parametrize("rejection", ["group", "unauthorized", "mismatched"])
def test_rejected_http_updates_never_open_a_database_session(
    monkeypatch, kind: str, rejection: str
) -> None:
    settings = Settings(
        _env_file=None,
        telegram_owner_user_ids=OWNER,
        telegram_webhook_secret=SECRET,
    )
    monkeypatch.setattr(telegram_api, "get_settings", lambda: settings)
    monkeypatch.setattr(
        deps,
        "get_session_factory",
        lambda: _forbidden("rejected HTTP update opened a database session"),
    )
    payload = _rejected_http_payload(kind)
    envelope = (
        payload.get("callback_query")
        or payload.get("edited_message")
        or payload["message"]
    )
    message = envelope.get("message", envelope)
    if rejection == "group":
        message["chat"] = {"id": -100991, "type": "supergroup"}
    elif rejection == "unauthorized":
        envelope["from"] = {"id": 771144}
        message["chat"] = {"id": 771144, "type": "private"}
    else:
        message["chat"] = {"id": int(OTHER_OWNER), "type": "private"}

    isolated_app = FastAPI()
    isolated_app.include_router(telegram_api.router)
    isolated_app.dependency_overrides[get_telegram_port] = RecordingMessagePort
    isolated_app.dependency_overrides[get_transcription_port] = lambda: FakeTranscriptionPort(
        "unused"
    )
    with TestClient(isolated_app) as client:
        response = client.post(
            "/v1/telegram/webhook",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        )

    assert response.status_code == 200
    assert response.json()["ignored"] is True


def test_unrecognized_private_callback_never_opens_a_database_session(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        telegram_owner_user_ids=OWNER,
        telegram_webhook_secret=SECRET,
    )
    monkeypatch.setattr(telegram_api, "get_settings", lambda: settings)
    monkeypatch.setattr(
        deps,
        "get_session_factory",
        lambda: _forbidden("unrecognized callback opened a database session"),
    )
    isolated_app = FastAPI()
    isolated_app.include_router(telegram_api.router)
    isolated_app.dependency_overrides[get_telegram_port] = RecordingMessagePort
    isolated_app.dependency_overrides[get_transcription_port] = lambda: FakeTranscriptionPort(
        "unused"
    )
    with TestClient(isolated_app) as client:
        response = client.post(
            "/v1/telegram/webhook",
            json=_callback_update("invalid"),
            headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        )

    assert response.status_code == 200
    assert response.json() == {
        "processed": 0,
        "ignored": True,
        "reason": "unrecognized_callback",
    }


@pytest.mark.parametrize("provided_secret", ["", "wrong-secret"])
def test_rejected_webhook_secret_never_opens_a_database_session(
    monkeypatch, provided_secret: str
) -> None:
    settings = Settings(
        _env_file=None,
        telegram_owner_user_ids=OWNER,
        telegram_webhook_secret=SECRET,
    )
    monkeypatch.setattr(telegram_api, "get_settings", lambda: settings)
    session_attempts: list[str] = []

    def explode_session_factory():
        session_attempts.append("session_factory")
        raise AssertionError("rejected webhook secret opened a database session")

    monkeypatch.setattr(deps, "get_session_factory", explode_session_factory)
    isolated_app = FastAPI()
    isolated_app.include_router(telegram_api.router)
    isolated_app.dependency_overrides[get_telegram_port] = RecordingMessagePort
    isolated_app.dependency_overrides[get_transcription_port] = lambda: FakeTranscriptionPort(
        "unused"
    )
    headers = (
        {"X-Telegram-Bot-Api-Secret-Token": provided_secret}
        if provided_secret
        else {}
    )
    with TestClient(isolated_app, raise_server_exceptions=False) as client:
        response = client.post("/v1/telegram/webhook", json=_message_update(), headers=headers)

    assert response.status_code == 500
    assert session_attempts == []


@pytest.mark.parametrize(
    "kind",
    [
        "text",
        "edited_text",
        "voice",
        "photo",
        "callback_approve",
        "callback_reject",
        "callback_lead_path",
    ],
)
@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel", "unknown", None])
def test_authorized_owner_group_updates_stop_before_claim_or_callback_ack(
    monkeypatch, kind: str, chat_type: str | None
) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(
        deps,
        "get_session_factory",
        lambda: _forbidden("invalid context opened a database session"),
    )
    monkeypatch.setattr(
        LeadStore,
        "claim_webhook",
        lambda *_args, **_kwargs: _forbidden("invalid context reached webhook claim"),
    )

    class NoSideEffects(RecordingMessagePort):
        async def answer_callback_query(self, *_args, **_kwargs):
            raise AssertionError("invalid callback was acknowledged")

        async def download_voice(self, *_args, **_kwargs):
            raise AssertionError("invalid voice was downloaded")

        async def download_photo(self, *_args, **_kwargs):
            raise AssertionError("invalid photo was downloaded")

    result = _post(_nonprivate_payload(kind, chat_type), NoSideEffects())
    assert result["processed"] == 0
    assert result["ignored"] is True


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel", "", "unknown"])
def test_owner_context_requires_exact_private_chat_type(chat_type: str) -> None:
    item = {"from": OWNER, "chat_id": OWNER, "chat_type": chat_type}
    assert not telegram_integration.is_valid_telegram_owner_context(item, {OWNER})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["message"].pop("from"),
        lambda p: p["message"].pop("chat"),
        lambda p: p["message"]["from"].update(id=int(OTHER_OWNER)),
        lambda p: p["message"]["chat"].update(id=int(OTHER_OWNER)),
        lambda p: p["message"]["from"].update(id=True),
        lambda p: p["message"]["from"].update(id=551122.0),
        lambda p: p["message"]["from"].update(id="551122"),
        lambda p: p["message"]["from"].update(id={"value": 551122}),
        lambda p: p["message"]["from"].update(id=[551122]),
        lambda p: p["message"]["chat"].update(id=True),
        lambda p: p["message"]["chat"].update(id=551122.0),
        lambda p: p["message"]["chat"].update(id="551122"),
        lambda p: p["message"]["chat"].update(id={"value": 551122}),
        lambda p: p["message"]["chat"].update(id=[551122]),
        lambda p: p["message"].update(sender_chat={"id": -1001}),
        lambda p: p["message"].update(business_connection_id="business-1"),
        lambda p: p["message"].update(guest_query_id="guest-1"),
        lambda p: p["message"].update(guest_bot_caller_user={"id": 551122}),
        lambda p: p["message"].update(guest_bot_caller_chat={"id": -1001}),
    ],
)
def test_malformed_or_ambiguous_message_identity_never_reaches_claim(monkeypatch, mutate) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(
        deps,
        "get_session_factory",
        lambda: _forbidden("malformed message opened a database session"),
    )
    payload = _message_update()
    mutate(payload)
    monkeypatch.setattr(
        LeadStore,
        "claim_webhook",
        lambda *_args, **_kwargs: _forbidden("invalid context reached webhook claim"),
    )
    result = _post(payload, RecordingMessagePort())
    assert result["processed"] == 0
    assert result["ignored"] is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["callback_query"].pop("message"),
        lambda p: p["callback_query"]["message"].pop("chat"),
        lambda p: p["callback_query"].update(inline_message_id="inline-1"),
        lambda p: p["callback_query"]["from"].update(id=int(OTHER_OWNER)),
        lambda p: p["callback_query"]["message"]["chat"].update(id=int(OTHER_OWNER)),
        lambda p: p["callback_query"]["from"].update(id=False),
        lambda p: p["callback_query"]["from"].update(id=551122.0),
        lambda p: p["callback_query"]["from"].update(id="551122"),
        lambda p: p["callback_query"]["from"].update(id={"value": 551122}),
        lambda p: p["callback_query"]["from"].update(id=[551122]),
        lambda p: p["callback_query"]["message"]["chat"].update(id=True),
        lambda p: p["callback_query"]["message"]["chat"].update(id=551122.0),
        lambda p: p["callback_query"]["message"]["chat"].update(id="551122"),
        lambda p: p["callback_query"]["message"]["chat"].update(id={"value": 551122}),
        lambda p: p["callback_query"]["message"]["chat"].update(id=[551122]),
        lambda p: p["callback_query"]["message"].update(sender_chat={"id": -1001}),
        lambda p: p["callback_query"]["message"].update(business_connection_id="business-1"),
        lambda p: p["callback_query"]["message"].update(guest_query_id="guest-1"),
        lambda p: p["callback_query"]["message"].update(
            guest_bot_caller_user={"id": 551122}
        ),
        lambda p: p["callback_query"]["message"].update(
            guest_bot_caller_chat={"id": -1001}
        ),
    ],
)
def test_malformed_or_ambiguous_callback_identity_has_no_side_effects(monkeypatch, mutate) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(
        deps,
        "get_session_factory",
        lambda: _forbidden("malformed callback opened a database session"),
    )
    payload = _callback_update()
    mutate(payload)

    class NoCallbackEffects(RecordingMessagePort):
        async def answer_callback_query(self, *_args, **_kwargs):
            raise AssertionError("invalid callback was acknowledged")

        async def edit_message_text(self, **_kwargs):
            raise AssertionError("invalid callback edited a message")

    result = _post(payload, NoCallbackEffects())
    assert result["processed"] == 0
    assert result["ignored"] is True


@pytest.mark.asyncio
async def test_direct_callback_bypass_stops_before_ack_db_or_send(monkeypatch) -> None:
    class NoCallbackEffects(RecordingMessagePort):
        async def answer_callback_query(self, *_args, **_kwargs):
            raise AssertionError("invalid callback was acknowledged")

        async def edit_message_text(self, **_kwargs):
            raise AssertionError("invalid callback edited a message")

    class NoDb:
        def __getattr__(self, _name):
            raise AssertionError("invalid callback touched the database")

    result = await telegram_api._handle_callback(
        callback={
            "callback_query_id": "direct-bypass",
            "from": OWNER,
            "chat_id": "-100991",
            "chat_type": "group",
            "message_id": "7",
            "data": "ok:apr_1",
        },
        port=NoCallbackEffects(),
        owner_ids={OWNER},
        db=NoDb(),
        settings=Settings(_env_file=None, telegram_owner_user_ids=OWNER),
    )
    assert result == {"processed": 0, "ignored": True, "reason": "invalid_owner_context"}


@pytest.mark.asyncio
async def test_direct_worker_bypass_stops_before_session_media_model_typing_or_send(
    monkeypatch,
) -> None:
    settings = Settings(_env_file=None, telegram_owner_user_ids=OWNER)
    monkeypatch.setattr(telegram_owner, "get_settings", lambda: settings)
    monkeypatch.setattr(
        telegram_owner,
        "get_session_factory",
        lambda: _forbidden("invalid worker context built a DB session"),
    )
    monkeypatch.setattr(
        telegram_owner,
        "run_owner_loop",
        lambda **_kwargs: _forbidden("invalid worker context reached model"),
    )

    class NoWorkerEffects(RecordingMessagePort):
        async def send(self, _message):
            raise AssertionError("invalid worker context sent a message")

        async def send_chat_action(self, *_args, **_kwargs):
            raise AssertionError("invalid worker context started typing")

        async def download_voice(self, *_args, **_kwargs):
            raise AssertionError("invalid worker context downloaded media")

    await telegram_owner.process_telegram_owner_update(
        item={
            "id": "worker-bypass",
            "from": OWNER,
            "chat_id": "-100991",
            "chat_type": "supergroup",
            "text": "private request",
        },
        envelope_kind="audio",
        voice_file_id="voice-private",
        photo_file_id=None,
        port=NoWorkerEffects(),
        transcribe_port=FakeTranscriptionPort("private transcript"),
    )


def test_legitimate_forwarded_private_owner_request_keeps_owner_context() -> None:
    payload = _message_update()
    payload["message"]["forward_origin"] = {
        "type": "user",
        "sender_user": {"id": 771144},
    }
    parsed = telegram_integration.parse_telegram_update(deepcopy(payload))
    assert parsed is not None
    assert telegram_integration.is_valid_telegram_owner_context(parsed, {OWNER})
    assert parsed["from"] == parsed["chat_id"] == OWNER
    assert parsed["chat_type"] == "private"
