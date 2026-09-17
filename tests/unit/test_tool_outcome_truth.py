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
from app.graph.owner_agent import TOOL_DEADLINE_REPLY, _run_tool_with_timeout
from app.integrations.sheets import FakeSheetsPort
from app.services.crm_v2 import CONTACT_FIELDS, WRITER_OWNER, CrmService
from app.tools.registries.owner_tools import (
    OUTCOME_FAILURE,
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


class _CrmImportBrokenSheets(FakeSheetsPort):
    """Current CRM reads must fail closed when Contacts import is unavailable."""

    def read_crm_contacts_chunk(self, **_kwargs) -> list[list[str]]:
        raise RuntimeError("contacts import unavailable")


class _CurrentCrmSheets(FakeSheetsPort):
    def __init__(self) -> None:
        super().__init__()
        # Contacts A:N plus the stable Mia id in O, as imported by the v2 service.
        self.locked_contacts.append(
            ["Dana", "050-0000000"] + [""] * 12 + ["crm_outcome_truth_1"]
        )


def test_outcome_defaults_follow_ok() -> None:
    assert ToolResult(ok=True, text="x").outcome_label() == OUTCOME_SUCCESS
    assert ToolResult(ok=False, error="boom").outcome_label() == OUTCOME_FAILURE


def test_a_timeout_is_not_a_success() -> None:
    init_db()
    db = get_session_factory()()
    try:
        import app.graph.owner_agent as owner_agent

        finished = []

        def _hang(_name, _args, _ctx):
            import time

            time.sleep(0.1)
            finished.append(True)
            raise AssertionError("should have timed out")

        original = owner_agent.execute_tool
        original_timeout = owner_agent.TOOL_TIMEOUT_SECONDS
        owner_agent.execute_tool = _hang  # type: ignore[method-assign]
        owner_agent.TOOL_TIMEOUT_SECONDS = 0.01
        try:
            result = _run_tool_with_timeout("gmail_inbox", {}, _ctx(db))
        finally:
            owner_agent.execute_tool = original  # type: ignore[method-assign]
            owner_agent.TOOL_TIMEOUT_SECONDS = original_timeout

        # The owner still hears something honest and natural.
        assert finished == [True], "active work must drain before the DB closes"
        assert result.text == TOOL_DEADLINE_REPLY
        # But nothing counts it as a tool that worked.
        assert result.ok is False
        assert result.outcome_label() == OUTCOME_TIMEOUT
        payload = result.payload()
        assert payload["ok"] is False
        assert payload["outcome"] == OUTCOME_TIMEOUT
        # The copy survives into the model payload so the turn is not left blank.
        assert payload["result"] == TOOL_DEADLINE_REPLY
    finally:
        db.close()


def test_a_lost_crm_import_is_reported_as_failure() -> None:
    init_db()
    db = get_session_factory()()
    try:
        result = execute_tool(
            "crm_search", {"query": "Dana"}, _ctx(db, _CrmImportBrokenSheets())
        )
        assert result.ok is False
        assert result.outcome_label() == OUTCOME_FAILURE
        assert "stale target" in result.error
    finally:
        db.close()


def test_a_current_crm_import_is_a_clean_success() -> None:
    init_db()
    db = get_session_factory()()
    try:
        sheets = _CurrentCrmSheets()
        seeded = CrmService(db).capture(
            {"name": "Dana", "phone": "050-0000000"},
            writer=WRITER_OWNER,
            source_ref="seed:outcome-truth",
        )
        assert seeded.contact is not None
        sheets.locked_contacts[0] = [
            seeded.contact.fields.get(name, "") for name in CONTACT_FIELDS
        ] + [seeded.contact.id]
        db.commit()
        result = execute_tool("crm_search", {"query": "Dana"}, _ctx(db, sheets))
        assert result.ok is True
        assert result.outcome_label() == OUTCOME_SUCCESS
        assert "Dana" in result.text
    finally:
        db.close()
