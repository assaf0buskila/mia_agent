"""A tool call reports what actually happened, not what reads nicely.

Two ways Mia used to lie to her own telemetry: a tool that ran out of time came back
`ok=True` because the owner-facing copy said "still checking", and a CRM read that
lost the Activity tab printed the same sentence as a genuinely empty tab. Both looked
healthy on a dashboard while an integration was down.
"""

from __future__ import annotations

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.two_state import STILL_CHECKING
from app.graph.owner_agent import _run_tool_with_timeout
from app.tools.registries.owner_tools import (
    OUTCOME_FAILURE,
    OUTCOME_PARTIAL,
    OUTCOME_SUCCESS,
    OUTCOME_TIMEOUT,
    ToolContext,
    ToolResult,
    execute_tool,
)


def _ctx(db, sheets=None) -> ToolContext:
    return ToolContext(
        principal=Principal.owner(source="telegram", actor_id="1"),
        store=LeadStore(db),
        brain=BrainStore(db),
        settings=Settings(_env_file=None),
        embedding_port=FakeEmbeddingPort(),
        sheets=sheets,
    )


class _ActivityBrokenSheets:
    """Contacts reads fine. Activity raises — exactly the half-failure case."""

    def read_locked_contacts(self) -> list[list[str]]:
        return [["name", "phone"], ["Dana", "050-0000000"]]

    def read_values(self, **_kwargs) -> list[list[str]]:
        raise RuntimeError("activity tab unavailable")


class _ActivityEmptySheets:
    def read_locked_contacts(self) -> list[list[str]]:
        return [["name", "phone"], ["Dana", "050-0000000"]]

    def read_values(self, **_kwargs) -> list[list[str]]:
        return []


def test_outcome_defaults_follow_ok() -> None:
    assert ToolResult(ok=True, text="x").outcome_label() == OUTCOME_SUCCESS
    assert ToolResult(ok=False, error="boom").outcome_label() == OUTCOME_FAILURE


def test_a_timeout_is_not_a_success() -> None:
    init_db()
    db = get_session_factory()()
    try:
        import app.graph.owner_agent as owner_agent

        def _hang(_name, _args, _ctx):
            import time

            time.sleep(20)
            raise AssertionError("should have timed out")

        original = owner_agent.execute_tool
        original_timeout = owner_agent.TOOL_TIMEOUT_SECONDS
        owner_agent.execute_tool = _hang  # type: ignore[method-assign]
        owner_agent.TOOL_TIMEOUT_SECONDS = 0.2
        try:
            result = _run_tool_with_timeout("gmail_inbox", {}, _ctx(db))
        finally:
            owner_agent.execute_tool = original  # type: ignore[method-assign]
            owner_agent.TOOL_TIMEOUT_SECONDS = original_timeout

        # The owner still hears something honest and natural.
        assert result.text == STILL_CHECKING
        # But nothing counts it as a tool that worked.
        assert result.ok is False
        assert result.outcome_label() == OUTCOME_TIMEOUT
        payload = result.payload()
        assert payload["ok"] is False
        assert payload["outcome"] == OUTCOME_TIMEOUT
        # The copy survives into the model payload so the turn is not left blank.
        assert payload["result"] == STILL_CHECKING
    finally:
        db.close()


def test_a_lost_activity_tab_is_reported_as_partial() -> None:
    init_db()
    db = get_session_factory()()
    try:
        result = execute_tool("crm_search", {"query": "Dana"}, _ctx(db, _ActivityBrokenSheets()))
        assert result.ok is True
        assert result.outcome_label() == OUTCOME_PARTIAL
        assert "could not be read" in result.text
        assert result.payload()["outcome"] == OUTCOME_PARTIAL
    finally:
        db.close()


def test_an_empty_activity_tab_is_still_a_clean_success() -> None:
    """The point of the previous test is the distinction, so pin the other side."""
    init_db()
    db = get_session_factory()()
    try:
        result = execute_tool("crm_search", {"query": "Dana"}, _ctx(db, _ActivityEmptySheets()))
        assert result.ok is True
        assert result.outcome_label() == OUTCOME_SUCCESS
        assert "could not be read" not in result.text
    finally:
        db.close()
