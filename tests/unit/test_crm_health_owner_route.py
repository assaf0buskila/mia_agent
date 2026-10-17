from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.owner.request_routing import is_crm_operational_health_request
from app.integrations.base import RecordingMessagePort
from app.surfaces.owner import run_owner_loop
from app.tools.owner.crm import _crm_operational_health
from app.tools.registries.owner_tools import get_tool, tool_definitions

OWNER = "12345"


@pytest.mark.parametrize("text", ["מצב CRM", "בדיקת crm?", "CRM health", "crm_operational_health"])
def test_explicit_health_route(text):
    assert is_crm_operational_health_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "מצב CRM ותעדכני את הליד",
        "תשלחי מצב CRM כל שעה",
        "בדיקת Sheets",
        "מצב CRM של יוסי",
    ],
)
def test_mixed_or_other_requests_use_existing_routing(text):
    assert not is_crm_operational_health_request(text)


def test_health_registered_without_expanding_normal_model_catalog():
    assert get_tool("crm_operational_health") is not None
    assert "crm_operational_health" not in {
        definition["function"]["name"] for definition in tool_definitions()
    }


@pytest.mark.parametrize(
    "principal",
    [
        Principal.client(source="website", actor_id=OWNER),
        Principal.owner(source="telegram", actor_id="unknown"),
        Principal.owner(source="telegram", actor_id="67890"),
        Principal.owner(source="telegram", actor_id=""),
    ],
)
def test_private_health_denies_visitor_and_spoofed_owner_before_db_read(principal):
    context = SimpleNamespace(
        principal=principal,
        settings=Settings(_env_file=None, telegram_owner_user_ids=OWNER),
    )
    # No store is supplied: denial must precede any customer/CRM access.
    assert _crm_operational_health(context, {}).ok is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor,configured,expected_reads",
    [
        (OWNER, OWNER, 1),
        (OWNER, "67890", 0),
        ("visitor", OWNER, 0),
    ],
)
async def test_health_owner_surface_requires_both_allowlists_and_bypasses_model(
    monkeypatch, actor, configured, expected_reads
):
    calls = []

    def diagnostic(session, **kwargs):
        calls.append(kwargs)
        assert kwargs["recipient_ids"] == {OWNER}
        return {"sanitized": True}

    def forbidden(**kwargs):
        raise AssertionError("standalone health must bypass the model and sync")

    monkeypatch.setattr("app.surfaces.owner._talk_with_optional_agent", forbidden)
    monkeypatch.setattr("app.services.crm_diagnostics.build_crm_diagnostics", diagnostic)
    monkeypatch.setattr(
        "app.services.crm_diagnostics.render_operational_health_he",
        lambda report: "מצב תפעולי של CRM (קריאה בלבד)",
    )
    init_db()
    session = get_session_factory()()
    port = RecordingMessagePort()
    item = {
        "id": "health-route-" + actor + configured,
        "from": actor,
        "chat_id": actor,
        "text": "מצב CRM",
    }
    store = LeadStore(session)
    store.claim_webhook(provider="telegram", provider_event_id=item["id"], channel="telegram")
    try:
        result = await run_owner_loop(
            item=item,
            store=store,
            port=port,
            settings=Settings(_env_file=None, telegram_owner_user_ids=configured),
            owner_ids={OWNER},
        )
        assert len(calls) == expected_reads
        if actor == "visitor":
            assert result.processed is False
            assert not port.sent
        elif expected_reads:
            assert result.sent is True
            assert "קריאה בלבד" in result.last_reply
        else:
            assert "מאומתים" in result.last_reply
    finally:
        session.close()


@pytest.mark.asyncio
async def test_normal_owner_request_keeps_existing_model_route(monkeypatch):
    calls = []

    def talk(**kwargs):
        calls.append(kwargs["text"])
        return "תשובה רגילה", False

    monkeypatch.setattr("app.surfaces.owner._talk_with_optional_agent", talk)
    init_db()
    session = get_session_factory()()
    port = RecordingMessagePort()
    store = LeadStore(session)
    item = {
        "id": "health-normal-route",
        "from": OWNER,
        "chat_id": OWNER,
        "text": "מה יש ביומן מחר?",
    }
    store.claim_webhook(provider="telegram", provider_event_id=item["id"], channel="telegram")
    try:
        result = await run_owner_loop(
            item=item,
            store=store,
            port=port,
            settings=Settings(_env_file=None, telegram_owner_user_ids=OWNER),
            owner_ids={OWNER},
        )
        assert calls == [item["text"]]
        assert result.sent is True
        assert result.last_reply == "תשובה רגילה"
    finally:
        session.close()
