from __future__ import annotations

from time import perf_counter
from types import SimpleNamespace

import pytest
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.owner.brain import OwnerBrainResult
from app.domain.owner.callbacks import approval_token
from app.domain.owner.request_routing import (
    categorized_tool_names,
    is_tool_inventory_request,
    owner_tool_inventory_reply,
    requests_no_history,
)
from app.domain.owner.tasks import OwnerTaskType
from app.integrations.base import RecordingMessagePort
from app.integrations.telegram_format import approval_keyboard, isolate
from app.surfaces.crm import DisabledContactsCrm
from app.surfaces.owner import _talk_with_optional_agent, run_owner_loop
from app.tools.registries.owner_tools import tool_names

OWNER_ID = "550077"


def test_inventory_route_is_narrow_and_no_history_is_explicit() -> None:
    assert is_tool_inventory_request("מה הכלים שלך אל תשתמשי בהיסטוריה?")
    assert is_tool_inventory_request("What tools do you have? Don't use history.")
    assert requests_no_history("מה הכלים שלך אל תשתמשי בהיסטוריה?")
    assert requests_no_history("answer without history")
    assert not is_tool_inventory_request("מה הכלים שלך ותבדקי לי את המייל")
    assert not is_tool_inventory_request("use your tools to check the calendar")


def test_inventory_covers_the_runtime_registry_and_states_live_boundary() -> None:
    assert categorized_tool_names() == frozenset(tool_names())
    reply = owner_tool_inventory_reply()
    assert f"{len(tool_names())} כלים רשומים" in reply
    assert all(name in reply for name in tool_names())
    assert "לא בדיקת חיבור חיה" in reply
    assert "באישור" in reply


@pytest.mark.asyncio
async def test_live_owner_surface_answers_inventory_without_history_model_or_learning(
    monkeypatch,
) -> None:
    calls = {"talk": 0, "history": 0, "learn": 0}

    def fail_talk(**_kwargs):
        calls["talk"] += 1
        raise AssertionError("inventory must bypass the model/CRM path")

    def fail_history(*_args, **_kwargs):
        calls["history"] += 1
        raise AssertionError("inventory must not read conversation history")

    def fail_learn(**_kwargs):
        calls["learn"] += 1
        raise AssertionError("inventory must not start post-send learning")

    monkeypatch.setattr("app.surfaces.owner._talk_with_optional_agent", fail_talk)
    monkeypatch.setattr(LeadStore, "list_conversation_turns", fail_history)

    init_db()
    db = get_session_factory()()
    port = RecordingMessagePort()
    store = LeadStore(db)
    item = {
        "id": "tg.inventory.no-history.1",
        "from": OWNER_ID,
        "chat_id": OWNER_ID,
        "text": "מה הכלים שלך אל תשתמשי בהיסטוריה?",
    }
    store.claim_webhook(provider="telegram", provider_event_id=item["id"], channel="telegram")
    started = perf_counter()
    try:
        result = await run_owner_loop(
            item=item,
            store=store,
            port=port,
            settings=Settings(_env_file=None),
            owner_ids={OWNER_ID},
        )
        elapsed = perf_counter() - started
        db.commit()
    finally:
        db.close()

    assert result.sent is True
    # C9: the live send path goes through owner_text() at egress, which
    # isolates the LTR digit run -- the visible count is unchanged.
    assert port.sent and f"{isolate(len(tool_names()))} כלים רשומים" in port.sent[0].text
    assert calls == {"talk": 0, "history": 0, "learn": 0}
    assert elapsed < 1.0


def test_no_history_skips_surface_history_and_prior_toolkit_hint(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fail_history(*_args, **_kwargs):
        raise AssertionError("explicit no-history turn must not query prior turns")

    def fake_answer_owner(**kwargs):
        captured.update(kwargs)
        return OwnerBrainResult("ok", True, ())

    monkeypatch.setattr(Settings, "owner_agent_ready", lambda _self: True)
    monkeypatch.setattr(LeadStore, "list_conversation_turns", fail_history)
    monkeypatch.setattr("app.domain.owner.brain.answer_owner", fake_answer_owner)
    monkeypatch.setattr("app.surfaces.owner.persist_ai_run", lambda *_a, **_kw: None)

    init_db()
    db = get_session_factory()()
    try:
        reply, wrote = _talk_with_optional_agent(
            text="תבדקי את המייל בלי היסטוריה",
            crm=DisabledContactsCrm(spreadsheet_id=""),
            settings=Settings(_env_file=None),
            store=LeadStore(db),
            item={"id": "tg.no-history.2", "from": OWNER_ID, "text": "x"},
        )
    finally:
        db.close()

    assert (reply, wrote) == ("ok", False)
    assert captured["history"] == ()
    assert captured["owner_text"] == "תבדקי את המייל בלי היסטוריה"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approval_ids", "task_type"),
    [
        (("apr_exact_turn",), OwnerTaskType.NOTE),
        ((), OwnerTaskType.NOTE),
        ((), OwnerTaskType.GMAIL_DRAFT),
        ((), OwnerTaskType.CALENDAR_WRITE),
    ],
)
async def test_live_surface_uses_only_current_agent_approval_metadata(
    monkeypatch, approval_ids, task_type
) -> None:
    def fake_talk(*, approval_ids_out, **_kwargs):
        approval_ids_out.extend(approval_ids)
        return "prepared", False

    monkeypatch.setattr("app.surfaces.owner._talk_with_optional_agent", fake_talk)
    monkeypatch.setattr(
        LeadStore,
        "list_all_pending_approvals",
        lambda _self: [SimpleNamespace(approval_id="apr_old_unrelated")],
    )

    init_db()
    db = get_session_factory()()
    port = RecordingMessagePort()
    store = LeadStore(db)
    item = {
        "id": f"tg.exact.active-surface.{task_type.value}.{'yes' if approval_ids else 'no'}",
        "from": OWNER_ID,
        "chat_id": OWNER_ID,
        "text": "prepare this action",
    }
    store.claim_webhook(provider="telegram", provider_event_id=item["id"], channel="telegram")
    try:
        await run_owner_loop(
            item=item,
            store=store,
            port=port,
            settings=Settings(_env_file=None),
            owner_ids={OWNER_ID},
        )
        db.commit()
    finally:
        db.close()

    # The prose reply never carries a keyboard; a turn-created approval id gets its
    # own follow-up card message with its own button instead (C2b). "apr_exact_turn"
    # has no backing row here, so its card renders the generic fallback text -- the
    # button still binds to the exact id the turn returned, which is what this test
    # actually pins.
    if approval_ids:
        assert len(port.sent) == 2
        assert port.sent[0].reply_markup is None
        assert port.sent[-1].reply_markup == approval_keyboard(approval_token("apr_exact_turn"))
    else:
        assert len(port.sent) == 1
        assert port.sent[0].reply_markup is None
    for message in port.sent:
        assert message.reply_markup != approval_keyboard(approval_token("apr_old_unrelated"))
