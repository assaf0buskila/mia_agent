"""Multi-owner Telegram fan-out (`notify_owners`, formerly `_notify_telegram`).

Before this fix only `sorted(owner_ids)[0]` was ever notified, so a second allowlisted
owner id was silently never told about a hot lead or a website->WhatsApp handoff. That
was a listed blocker on selling Mia as a multi-owner product.
"""

from __future__ import annotations

import httpx
from app.core.config import Settings
from app.domain.handoff.hot import notify_owners
from app.services import notifications as notifications_mod


class _RecordingClient:
    """Fake `httpx.Client`: records every `sendMessage` call, fails on chosen chat ids."""

    def __init__(self, *, fail_chat_ids: frozenset[str] = frozenset()) -> None:
        self.fail_chat_ids = fail_chat_ids
        self.calls: list[dict[str, object]] = []

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
        self.calls.append(json)
        if json["chat_id"] in self.fail_chat_ids:
            raise httpx.ConnectError("boom", request=httpx.Request("POST", url))
        return httpx.Response(200, json={"ok": True})


def _patch_client(monkeypatch, client: _RecordingClient) -> None:
    monkeypatch.setattr(notifications_mod.httpx, "Client", lambda **kwargs: client)


def test_notify_owners_sends_to_every_allowlisted_owner(monkeypatch) -> None:
    client = _RecordingClient()
    _patch_client(monkeypatch, client)
    settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111,222")

    delivered = notify_owners(brief="ליד חם", inbound_id="in_1", settings=settings)

    assert delivered == ("111", "222")
    assert [call["chat_id"] for call in client.calls] == ["111", "222"]
    assert all(call["text"] == "ליד חם" for call in client.calls)


def test_notify_owners_second_failure_does_not_stop_first_and_is_not_success(
    monkeypatch,
) -> None:
    client = _RecordingClient(fail_chat_ids=frozenset({"222"}))
    _patch_client(monkeypatch, client)
    settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111,222")

    delivered = notify_owners(brief="ליד חם", inbound_id="in_1", settings=settings)

    # Both sends were attempted (owner 111 was not skipped because 222 would fail)...
    assert [call["chat_id"] for call in client.calls] == ["111", "222"]
    # ...but the failed recipient is never reported as delivered.
    assert delivered == ("111",)
    assert "222" not in delivered


def test_notify_owners_single_owner_matches_previous_behavior(monkeypatch) -> None:
    client = _RecordingClient()
    _patch_client(monkeypatch, client)
    settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111")

    delivered = notify_owners(brief="ליד חם", inbound_id="in_1", settings=settings)

    assert delivered == ("111",)
    assert len(client.calls) == 1
    assert client.calls[0]["chat_id"] == "111"


def test_notify_owners_http_error_status_is_not_delivery(monkeypatch) -> None:
    class _BadStatus(_RecordingClient):
        def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
            self.calls.append(json)
            return httpx.Response(400, json={"ok": False, "description": "bad request"})

    client = _BadStatus()
    _patch_client(monkeypatch, client)
    settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111")
    assert notify_owners(brief="ליד חם", inbound_id="in_1", settings=settings) == ()
    assert client.calls


def test_notify_owners_no_token_or_owner_ids_sends_nothing(monkeypatch) -> None:
    client = _RecordingClient()
    _patch_client(monkeypatch, client)

    assert notify_owners(
        brief="x", inbound_id="in_1", settings=Settings(telegram_owner_user_ids="111")
    ) == ()
    assert notify_owners(
        brief="x", inbound_id="in_1", settings=Settings(telegram_bot_token="tok")
    ) == ()
    assert client.calls == []
