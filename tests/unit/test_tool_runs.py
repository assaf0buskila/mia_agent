import json

from app.db.models import ToolRunRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel, persist_tool_outcome
from app.domain.tools import ToolOutcome
from sqlalchemy import func, select

VISITOR_TEXT = "hi"
CALENDAR_OUTCOME = ToolOutcome(
    tool="calendar_find_free_slots", status="ok", result_count=2
)
SHEETS_OUTCOME = ToolOutcome(tool="sheets_mirror", status="ok", result_count=1)
DENIED_CALENDAR = ToolOutcome(
    tool="calendar_find_free_slots", status="denied", result_count=0
)


def _all_tool_run_values(row: ToolRunRow) -> str:
    return json.dumps(
        {
            "provider_event_id": row.provider_event_id,
            "provider": row.provider,
            "channel": row.channel,
            "lead_id": row.lead_id,
            "conversation_id": row.conversation_id,
            "tool": row.tool,
            "status": row.status,
            "result_count": row.result_count,
            "latency_ms": row.latency_ms,
            "cost_usd": row.cost_usd,
            "correlation_id": row.correlation_id,
        }
    )


def test_persist_tool_outcome_duplicate_calendar_writes_one_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        provider_event_id = "in_cal_1:tool:calendar_find_free_slots"
        for status in ("ok", "denied"):
            persist_tool_outcome(
                store,
                provider="website",
                channel=Channel.WEBSITE,
                inbound_provider_event_id="in_cal_1",
                conversation_id="web_cal_1",
                lead_id="lead_cal_1",
                outcome=ToolOutcome(
                    tool="calendar_find_free_slots",
                    status=status,
                    result_count=1 if status == "ok" else 0,
                ),
            )
        db.commit()
        count = db.scalar(
            select(func.count())
            .select_from(ToolRunRow)
            .where(ToolRunRow.provider_event_id == provider_event_id)
        )
        assert count == 1
        row = store.get_tool_run(provider_event_id)
        assert row is not None
        assert row.tool == "calendar_find_free_slots"
        assert row.status == "ok"
        assert row.result_count == 1
        assert row.latency_ms == 0
        assert row.cost_usd == 0
        assert row.channel == Channel.WEBSITE.value
        assert VISITOR_TEXT not in _all_tool_run_values(row)
        assert "@" not in _all_tool_run_values(row)
    finally:
        db.close()


def test_persist_tool_outcome_different_tools_write_two_rows() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        persist_tool_outcome(
            store,
            provider="website",
            channel=Channel.WEBSITE,
            inbound_provider_event_id="in_multi_1",
            conversation_id="web_multi_1",
            lead_id="lead_multi_1",
            outcome=CALENDAR_OUTCOME,
        )
        persist_tool_outcome(
            store,
            provider="website",
            channel=Channel.WEBSITE,
            inbound_provider_event_id="in_multi_1",
            conversation_id="web_multi_1",
            lead_id="lead_multi_1",
            outcome=SHEETS_OUTCOME,
        )
        db.commit()
        rows = list(
            db.scalars(
                select(ToolRunRow).where(ToolRunRow.lead_id == "lead_multi_1")
            ).all()
        )
        assert len(rows) == 2
        tools = {row.tool for row in rows}
        assert tools == {"calendar_find_free_slots", "sheets_mirror"}
        for row in rows:
            assert row.latency_ms == 0
            assert row.cost_usd == 0
            assert VISITOR_TEXT not in _all_tool_run_values(row)
    finally:
        db.close()


def test_persist_tool_outcome_denied_still_persists_tool_run() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        persist_tool_outcome(
            store,
            provider="gmail",
            channel=Channel.GMAIL,
            inbound_provider_event_id="in_denied_1",
            conversation_id="gmail_thread_1",
            lead_id="lead_denied_1",
            outcome=DENIED_CALENDAR,
        )
        db.commit()
        row = store.get_tool_run("in_denied_1:tool:calendar_find_free_slots")
        assert row is not None
        assert row.status == "denied"
        assert row.result_count == 0
        assert "@" not in _all_tool_run_values(row)
    finally:
        db.close()


def test_persist_tool_outcome_stores_sanitized_correlation_id() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        run_id = "run_abc123def456"
        persist_tool_outcome(
            store,
            provider="website",
            channel=Channel.WEBSITE,
            inbound_provider_event_id="corr.tool.1",
            conversation_id="web_corr_1",
            lead_id="lead_corr_1",
            outcome=SHEETS_OUTCOME,
            correlation_id=run_id,
        )
        db.commit()
        row = store.get_tool_run("corr.tool.1:tool:sheets_mirror")
        assert row is not None
        assert row.correlation_id == run_id
        assert VISITOR_TEXT not in _all_tool_run_values(row)
        assert "@" not in _all_tool_run_values(row)
    finally:
        db.close()


def test_persist_tool_outcome_invalid_correlation_id_stores_empty() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        persist_tool_outcome(
            store,
            provider="website",
            channel=Channel.WEBSITE,
            inbound_provider_event_id="corr.tool.invalid.1",
            conversation_id="web_corr_invalid_1",
            lead_id="lead_corr_invalid_1",
            outcome=SHEETS_OUTCOME,
            correlation_id="x y",
        )
        db.commit()
        row = store.get_tool_run("corr.tool.invalid.1:tool:sheets_mirror")
        assert row is not None
        assert row.correlation_id == ""
    finally:
        db.close()


def test_persist_tool_outcome_duplicate_correlation_first_write_wins() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        first_id = "run_firstwrite001"
        second_id = "run_secondwrite02"
        persist_tool_outcome(
            store,
            provider="website",
            channel=Channel.WEBSITE,
            inbound_provider_event_id="corr.tool.dup.1",
            conversation_id="web_corr_dup_1",
            lead_id="lead_corr_dup_1",
            outcome=SHEETS_OUTCOME,
            correlation_id=first_id,
        )
        persist_tool_outcome(
            store,
            provider="website",
            channel=Channel.WEBSITE,
            inbound_provider_event_id="corr.tool.dup.1",
            conversation_id="web_corr_dup_1",
            lead_id="lead_corr_dup_1",
            outcome=ToolOutcome(tool="sheets_mirror", status="denied", result_count=0),
            correlation_id=second_id,
        )
        db.commit()
        row = store.get_tool_run("corr.tool.dup.1:tool:sheets_mirror")
        assert row is not None
        assert row.correlation_id == first_id
        assert row.status == "ok"
    finally:
        db.close()
