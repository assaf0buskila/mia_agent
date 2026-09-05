"""Named write flags: defaults, R4/R5 hard gates, calendar create gate."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from app.core.config import Settings
from app.core.risk import PolicyDecision, RiskLevel, decide
from app.core.write_flags import named_write_may_auto, write_flag_enabled
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.meetings.booking import BookingResultKind, attempt_meeting_booking
from app.domain.tools import ToolOutcome
from app.integrations.calendar import FakeCalendarPort
from app.integrations.calendar_booking import FakeCalendarBookingPort

from tests.unit.test_calendar_booking import FIXED_NOW, _seed_offered, _slot

_FLAG_ENV_KEYS = (
    "MIA_CALENDAR_WRITE",
    "MIA_GMAIL_SEND",
    "MIA_META_WRITE",
    "MIA_AUTO_REPLY_INSTAGRAM",
)

_FLAG_ATTRS = (
    "calendar_write",
    "gmail_send",
    "meta_write",
    "auto_reply_instagram",
)


def test_settings_write_flags_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _FLAG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MIA_CALENDAR_WRITE", "false")
    settings = Settings()
    for attr in _FLAG_ATTRS:
        assert getattr(settings, attr) is False


def test_named_write_may_auto_respects_risk_and_enabled() -> None:
    assert named_write_may_auto(enabled=True, risk=RiskLevel.R4_FINANCIAL_MARKETING) is False
    assert named_write_may_auto(enabled=True, risk=RiskLevel.R5_DESTRUCTIVE) is False
    assert named_write_may_auto(enabled=True, risk=RiskLevel.R1_LOW_WRITE) is True
    assert named_write_may_auto(enabled=False, risk=RiskLevel.R1_LOW_WRITE) is False


def test_decide_r4_r5_unaffected_by_meta_write_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIA_META_WRITE", "true")
    from app.core.risk import RiskAction

    assert (
        decide(
            RiskAction(name="meta_budget", risk=RiskLevel.R4_FINANCIAL_MARKETING),
            kill_switch=False,
        )
        == PolicyDecision.APPROVAL
    )
    assert (
        decide(
            RiskAction(name="delete_data", risk=RiskLevel.R5_DESTRUCTIVE),
            kill_switch=False,
        )
        == PolicyDecision.DENY
    )


def test_calendar_booking_denied_when_calendar_write_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIA_CALENDAR_WRITE", "false")
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_writeflag_cal_1"
        )
        _seed_offered(store, lead_id, [_slot(4, 10)])
        db.commit()
        booking = FakeCalendarBookingPort()
        calendar = FakeCalendarPort([_slot(4, 10)])
        result = attempt_meeting_booking(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            provider="website",
            conversation_id="web_writeflag_cal_1",
            inbound_provider_event_id="evt.writeflag.1",
            message="1",
            calendar=calendar,
            booking_port=booking,
            kill_switch=False,
            timezone="Asia/Jerusalem",
            now=FIXED_NOW,
        )
        assert result.kind == BookingResultKind.DENIED
        assert booking.lookup_calls == []
        assert booking.create_calls == []
        assert result.tool_outcomes == [
            ToolOutcome(tool="calendar_create", status="denied", result_count=0)
        ]
    finally:
        db.close()


def test_write_flags_module_has_no_forbidden_imports() -> None:
    source = Path("app/core/write_flags.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "app.graph" not in source
    assert "MessagePort" not in source
    assert "graph" not in imported
    assert "integrations" not in imported


def test_write_flag_enabled_unknown_name_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIA_CALENDAR_WRITE", "true")
    settings = Settings()
    assert write_flag_enabled(settings, "unknown_flag") is False
    assert write_flag_enabled(settings, "calendar_write") is True
