"""crm_upsert must fail loudly, and must not write the same contact twice.

Before this, the handler said "Wrote Contacts on <sheet>" on any non-exception, and
had no idempotency claim at all — so a retried owner message wrote the row again.
"""

from __future__ import annotations

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.tools import AdapterResponseError
from app.integrations.sheets import FakeSheetsPort
from app.tools.registries.owner_tools import ToolContext, execute_tool

OWNER = "12345"
ARGS = {"name": "דנה", "phone": "0501234567", "want": "ניהול תורים"}


class RefusingSheetsPort(FakeSheetsPort):
    """Composio answered HTTP 200 and reported the write did not happen."""

    def write_locked_contact(self, cells: list[str], *, key_column: str) -> None:
        raise AdapterResponseError()


class FlakySheetsPort(FakeSheetsPort):
    """Fails once, then works — a transient provider blip."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def write_locked_contact(self, cells: list[str], *, key_column: str) -> None:
        self.attempts += 1
        if self.attempts == 1:
            raise AdapterResponseError()
        super().write_locked_contact(cells, key_column=key_column)


def _ctx(db, sheets, *, source_ref: str) -> ToolContext:
    return ToolContext(
        principal=Principal.owner(source="telegram", actor_id=OWNER),
        store=LeadStore(db),
        brain=BrainStore(db),
        settings=Settings(_env_file=None, sheets_spreadsheet_id=""),
        embedding_port=FakeEmbeddingPort(),
        sheets=sheets,
        owner_text="תרשמי את דנה 0501234567",
        source_ref=source_ref,
    )


def test_a_good_write_reports_success() -> None:
    init_db()
    db = get_session_factory()()
    try:
        out = execute_tool(
            "crm_upsert", dict(ARGS), _ctx(db, FakeSheetsPort(), source_ref="tg.ok.1")
        )
        assert out.ok is True
        assert "Wrote Contacts" in out.text
    finally:
        db.close()


def test_a_refused_write_is_never_reported_as_success() -> None:
    """The regression this batch exists for."""
    init_db()
    db = get_session_factory()()
    try:
        out = execute_tool(
            "crm_upsert", dict(ARGS), _ctx(db, RefusingSheetsPort(), source_ref="tg.bad.1")
        )
        assert out.ok is False, "a rejected CRM write must not be a success"
        assert "Wrote Contacts" not in (out.text or "")
        assert "nothing was saved" in (out.error or "")
    finally:
        db.close()


def test_no_secret_in_the_failure_text() -> None:
    init_db()
    db = get_session_factory()()
    try:
        out = execute_tool(
            "crm_upsert", dict(ARGS), _ctx(db, RefusingSheetsPort(), source_ref="tg.bad.2")
        )
        blob = f"{out.text or ''}{out.error or ''}"
        for secret in ("cmp-", "api_key", "x-api-key", "Bearer"):
            assert secret not in blob
    finally:
        db.close()


def test_the_same_owner_message_does_not_write_twice() -> None:
    init_db()
    db = get_session_factory()()
    try:
        sheets = FakeSheetsPort()
        ctx = _ctx(db, sheets, source_ref="tg.dup.1")
        first = execute_tool("crm_upsert", dict(ARGS), ctx)
        second = execute_tool("crm_upsert", dict(ARGS), ctx)
        assert first.ok and second.ok
        assert "already written" in second.text
        assert len(sheets.locked_contacts) == 1, "the row must be written once"
    finally:
        db.close()


def test_a_different_owner_message_may_write_again() -> None:
    """The claim is per owner event, not a permanent block on the contact."""
    init_db()
    db = get_session_factory()()
    try:
        sheets = FakeSheetsPort()
        execute_tool("crm_upsert", dict(ARGS), _ctx(db, sheets, source_ref="tg.a"))
        execute_tool("crm_upsert", dict(ARGS), _ctx(db, sheets, source_ref="tg.b"))
        assert len(sheets.locked_contacts) == 2
    finally:
        db.close()


def test_a_failed_write_does_not_silently_retry_into_a_duplicate() -> None:
    """The row may have landed before the failure was reported, so the same owner
    event must not quietly write it again."""
    init_db()
    db = get_session_factory()()
    try:
        sheets = FlakySheetsPort()
        ctx = _ctx(db, sheets, source_ref="tg.flaky.1")
        first = execute_tool("crm_upsert", dict(ARGS), ctx)
        assert first.ok is False
        second = execute_tool("crm_upsert", dict(ARGS), ctx)
        assert second.ok is True
        assert "already written" in second.text
        assert sheets.attempts == 1, "the same event must not hit the provider twice"
    finally:
        db.close()
