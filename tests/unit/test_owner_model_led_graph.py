"""Focused proof for model first ordering, evidence guards, budgets and timing."""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.owner_timing import owner_stage
from app.graph.owner_agent import run_owner_agent
from app.tools.registries.owner_tools import ToolResult

from tests.unit.test_brain_agent import (
    _assistant_text,
    _assistant_tool_call,
    _client,
    _ctx,
    _session,
)


def test_crm_is_selected_after_first_model_call(monkeypatch) -> None:
    session = _session()
    client, transport = _client(
        [_assistant_tool_call("c1", "crm_search", {"query": "Contacts"}), _assistant_text("מצאתי.")]
    )
    calls: list[str] = []

    def fake_tool(name: str, arguments: dict[str, Any], ctx: Any, **kwargs: Any) -> ToolResult:
        calls.append(name)
        return ToolResult(ok=True, text="Contact row")

    monkeypatch.setattr("app.graph.owner_agent._run_tool_with_timeout", fake_tool)
    outcome = run_owner_agent(client=client, ctx=_ctx(session), owner_message="בדוק CRM")
    assert outcome.completed
    assert outcome.tools_used == ("crm_search",)
    assert calls == ["crm_search"]
    assert len(transport.requests) >= 1


def test_full_profile_correction_is_bounded_and_needs_evidence(monkeypatch) -> None:
    session = _session()
    client, transport = _client(
        [_assistant_text("יש לי תקציר בלבד"), _assistant_text("עדיין תקציר")]
    )
    outcome = run_owner_agent(
        client=client,
        ctx=_ctx(session),
        owner_message="Show my full LinkedIn profile",
        max_steps=3,
    )
    assert outcome.completed is False
    assert outcome.completion == "incomplete_evidence"
    assert len(transport.requests) == 2
    assert (
        sum(
            "fresh applicable LinkedIn profile read" in str(m)
            for m in transport.requests[1]["messages"]
        )
        == 1
    )


def test_profile_discovery_does_not_count_as_profile_evidence(monkeypatch) -> None:
    session = _session()
    client, _ = _client(
        [
            _assistant_tool_call(
                "c1",
                "composio_search_tools",
                {"query": "profile", "toolkit": "LINKEDIN", "limit": 10},
            ),
            _assistant_text("אין פרופיל מלא"),
        ]
    )
    monkeypatch.setattr(
        "app.graph.owner_agent._run_tool_with_timeout",
        lambda *_args, **_kwargs: ToolResult(ok=True, text="LINKEDIN_PROFILE_EXPORT catalog entry"),
    )
    outcome = run_owner_agent(
        client=client,
        ctx=_ctx(session),
        owner_message="Show my full LinkedIn profile",
        max_steps=2,
    )
    assert outcome.completed is False
    assert outcome.completion == "incomplete_evidence"


def test_successful_typed_profile_evidence_allows_completion(monkeypatch) -> None:
    session = _session()
    client, _ = _client(
        [
            _assistant_tool_call("c1", "linkedin_snapshot", {"full_profile": True}),
            _assistant_text("הפרופיל המלא מעודכן."),
        ]
    )
    monkeypatch.setattr(
        "app.graph.owner_agent._run_tool_with_timeout",
        lambda *_args, **_kwargs: ToolResult(
            ok=True, text="typed profile", evidence="linkedin_profile"
        ),
    )
    outcome = run_owner_agent(
        client=client,
        ctx=_ctx(session),
        owner_message="Show my full LinkedIn profile",
    )
    assert outcome.completed is True


def test_owner_stage_does_not_log_text_or_unknown_tool(caplog) -> None:
    with caplog.at_level("INFO", logger="mia.owner_timing"):
        with owner_stage(
            "tool",
            source_ref="SECRET_SOURCE_TEXT",
            model="SECRET_MODEL",
            tool="SECRET_TOOL",
        ):
            pass
    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert "SECRET_SOURCE_TEXT" not in rendered
    assert "SECRET_MODEL" not in rendered
    assert "SECRET_TOOL" not in rendered
    assert "source_ref_hash=" in rendered


def test_nested_stage_inherits_hashed_source_reference(caplog) -> None:
    with caplog.at_level("INFO", logger="mia.owner_timing"):
        with owner_stage("model", source_ref="turn-a", model="gpt-test"):
            with owner_stage("model_attempt", model="gpt-test"):
                pass
    hashes = [record.getMessage().split("source_ref_hash=", 1)[1] for record in caplog.records]
    assert len(hashes) == 2
    assert hashes[0] == hashes[1]
    assert "turn-a" not in " ".join(record.getMessage() for record in caplog.records)


def test_owner_stage_records_cancellation(caplog) -> None:
    with caplog.at_level("INFO", logger="mia.owner_timing"):
        try:
            with owner_stage("model", source_ref="cancelled-turn"):
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            pass
    assert any("outcome=cancelled" in record.getMessage() for record in caplog.records)
