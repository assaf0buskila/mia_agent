"""Multi-owner Telegram fan-out (`notify_owners`, formerly `_notify_telegram`).

Before this fix only `sorted(owner_ids)[0]` was ever notified, so a second allowlisted
owner id was silently never told about a hot lead or a website->WhatsApp handoff. That
was a listed blocker on selling Mia as a multi-owner product.
"""

from __future__ import annotations

import httpx
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.conversation_scope import TakeoverState
from app.domain.events import Channel
from app.domain.hot_handoff import KIND_HOT_LEAD, apply_hot_handoff, notify_owners
from app.domain.sales import FitLevel, PainLevel, SalesState
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


class _FakeHotHandoffStore:
    def __init__(self) -> None:
        self.takeover_states: dict[str, str] = {}
        self.cancelled: list[str] = []
        self.upserts: list[dict[str, str]] = []
        self.released: list[tuple[str, str, str]] = []
        self.recipient_claims: set[tuple[str, str, str]] = set()
        self.sales = SalesState(
            lead_id="lead_hot123456", fit=FitLevel.GOOD, pain_level=PainLevel.P3
        )

    def set_takeover_state(self, lead_id: str, state: str) -> None:
        self.takeover_states[lead_id] = state

    def cancel_pending_follow_up(self, lead_id: str) -> None:
        self.cancelled.append(lead_id)

    def get_sales(self, lead_id: str) -> SalesState:
        return self.sales

    def upsert_owner_notification(
        self, *, kind: str, lead_id: str, scheduled_at: str
    ) -> None:
        self.upserts.append({"kind": kind, "lead_id": lead_id, "scheduled_at": scheduled_at})

    def try_insert_owner_notification(
        self, *, kind: str, lead_id: str, scheduled_at: str, conversation_id: str = ""
    ) -> bool:
        # Deliberately always True: this file tests the Telegram fan-out, so the fake must
        # never be the thing that suppresses a send. Claim-once is proved against the real
        # LeadStore in tests/unit/test_vnext_finalization.py.
        del conversation_id
        self.upsert_owner_notification(
            kind=kind, lead_id=lead_id, scheduled_at=scheduled_at
        )
        return True

    def release_owner_notification_claim(
        self, *, kind: str, lead_id: str, conversation_id: str = ""
    ) -> None:
        self.released.append((kind, lead_id, conversation_id))

    def try_claim_owner_notification_recipient(
        self, *, kind: str, lead_id: str, recipient_id: str, claimed_at: str
    ) -> bool:
        del claimed_at
        key = (kind, lead_id, recipient_id)
        if key in self.recipient_claims:
            return False
        self.recipient_claims.add(key)
        return True

    def release_owner_notification_recipient_claim(
        self, *, kind: str, lead_id: str, recipient_id: str
    ) -> None:
        self.recipient_claims.discard((kind, lead_id, recipient_id))


def test_apply_hot_handoff_notifies_every_owner(monkeypatch) -> None:
    """End to end: the hot-lead path now fans the Telegram brief out to every owner id,
    not just the first, while still persisting the notification exactly once."""
    client = _RecordingClient()
    _patch_client(monkeypatch, client)
    store = _FakeHotHandoffStore()
    settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111,222")

    apply_hot_handoff(
        store,
        lead_id="lead_hot123456",
        inbound_id="in_1",
        want="לדבר איתכם",
        kill_switch=False,
        settings=settings,
    )

    assert [call["chat_id"] for call in client.calls] == ["111", "222"]
    assert len(store.upserts) == 1
    assert store.upserts[0]["kind"] == KIND_HOT_LEAD


def test_hot_handoff_releases_only_after_confirmed_full_rejection(monkeypatch) -> None:
    class _RejectedClient(_RecordingClient):
        def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
            self.calls.append(json)
            return httpx.Response(400, json={"ok": False})

    client = _RejectedClient()
    _patch_client(monkeypatch, client)
    store = _FakeHotHandoffStore()
    apply_hot_handoff(
        store,
        lead_id="lead_hot123456",
        inbound_id="in_rejected",
        want="human",
        kill_switch=False,
        settings=Settings(telegram_bot_token="tok", telegram_owner_user_ids="111"),
    )
    assert (KIND_HOT_LEAD, "lead_hot123456", "111") not in store.recipient_claims


def test_hot_handoff_retains_claim_after_ambiguous_transport_error(monkeypatch) -> None:
    client = _RecordingClient(fail_chat_ids=frozenset({"111"}))
    _patch_client(monkeypatch, client)
    store = _FakeHotHandoffStore()
    apply_hot_handoff(
        store,
        lead_id="lead_hot123456",
        inbound_id="in_transport",
        want="human",
        kill_switch=False,
        settings=Settings(telegram_bot_token="tok", telegram_owner_user_ids="111"),
    )
    assert store.released == []


def test_hot_handoff_partial_success_retains_claim(monkeypatch) -> None:
    client = _RecordingClient(fail_chat_ids=frozenset({"222"}))
    _patch_client(monkeypatch, client)
    store = _FakeHotHandoffStore()
    attempt = apply_hot_handoff(
        store,
        lead_id="lead_hot123456",
        inbound_id="in_partial",
        want="human",
        kill_switch=False,
        settings=Settings(telegram_bot_token="tok", telegram_owner_user_ids="111,222"),
    )
    assert attempt.delivered == ("111",)
    assert store.released == []


def test_hot_handoff_retains_claim_after_malformed_success_response(monkeypatch) -> None:
    class _MalformedClient(_RecordingClient):
        def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
            self.calls.append(json)
            return httpx.Response(200, content=b"not-json")

    client = _MalformedClient()
    _patch_client(monkeypatch, client)
    store = _FakeHotHandoffStore()
    apply_hot_handoff(
        store,
        lead_id="lead_hot123456",
        inbound_id="in_malformed",
        want="human",
        kill_switch=False,
        settings=Settings(telegram_bot_token="tok", telegram_owner_user_ids="111"),
    )
    assert store.released == []


def test_hot_handoff_kill_switch_mutates_nothing_in_the_real_store(monkeypatch) -> None:
    client = _RecordingClient()
    _patch_client(monkeypatch, client)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_hot_kill_order"
        )
        before_state = store.get_takeover_state(lead_id)
        before_follow_up = store.get_follow_up(lead_id)
        attempt = apply_hot_handoff(
            store,
            lead_id=lead_id,
            inbound_id="hot:kill",
            want="human",
            kill_switch=True,
            settings=Settings(telegram_bot_token="tok", telegram_owner_user_ids="111"),
        )
        assert attempt.attempted is False
        assert store.get_takeover_state(lead_id) == before_state == TakeoverState.MIA_ACTIVE.value
        assert store.get_follow_up(lead_id) is before_follow_up is None
        assert not store.has_owner_notification(kind=KIND_HOT_LEAD, lead_id=lead_id)
        assert client.calls == []
    finally:
        db.close()
