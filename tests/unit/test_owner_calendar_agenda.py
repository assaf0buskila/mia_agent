
"""Read-only calendar agenda: window resolution, rendering, and the calendar_agenda tool.

Separate from `app.domain.owner_calendar.apply_owner_calendar` (free-slot availability,
"when am I free"). This covers the additive "what's on my calendar" read: window math
in `resolve_agenda_window`, rendering in `format_calendar_agenda`, the `calendar_agenda`
handler in `app.tools.owner.calendar`, and its registration in
`app.tools.registries.owner_tools`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import get_settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.owner_calendar import (
    AGENDA_RANGES,
    format_calendar_agenda,
    resolve_agenda_window,
)
from app.integrations.calendar import CalendarEvent, FakeCalendarAgendaPort
from app.tools.registries.owner_tools import ToolContext, execute_tool, get_tool

IL = ZoneInfo("Asia/Jerusalem")
# Wednesday 2026-08-19 10:30 Asia/Jerusalem.
NOW_LOCAL = datetime(2026, 8, 19, 10, 30, tzinfo=IL)
NOW_UTC = NOW_LOCAL.astimezone(UTC)


def _session():
    init_db()
    return get_session_factory()()


def _ctx(session, *, calendar_agenda=None) -> ToolContext:
    return ToolContext(
        principal=Principal.owner(source="test"),
        store=LeadStore(session),
        brain=BrainStore(session),
        settings=get_settings(),
        embedding_port=FakeEmbeddingPort(),
        calendar_agenda=calendar_agenda,
        now=NOW_UTC,
        source_ref="telegram:test",
    )


# --------------------------------------------------------------- resolve_agenda_window


def test_today_is_local_midnight_to_midnight() -> None:
    start, end = resolve_agenda_window("today", now=NOW_UTC, timezone="Asia/Jerusalem")
    expected_start = datetime(2026, 8, 19, 0, 0, tzinfo=IL).astimezone(UTC)
    expected_end = datetime(2026, 8, 20, 0, 0, tzinfo=IL).astimezone(UTC)
    assert start == expected_start
    assert end == expected_end


def test_tomorrow_is_the_next_local_midnight_to_midnight() -> None:
    start, end = resolve_agenda_window("tomorrow", now=NOW_UTC, timezone="Asia/Jerusalem")
    expected_start = datetime(2026, 8, 20, 0, 0, tzinfo=IL).astimezone(UTC)
    expected_end = datetime(2026, 8, 21, 0, 0, tzinfo=IL).astimezone(UTC)
    assert start == expected_start
    assert end == expected_end


def test_next_7_days_is_a_rolling_window_from_now_not_midnight_aligned() -> None:
    start, end = resolve_agenda_window("next_7_days", now=NOW_UTC, timezone="Asia/Jerusalem")
    assert start == NOW_UTC
    assert end == NOW_UTC + timedelta(days=7)


def test_this_week_runs_from_now_to_the_end_of_the_israeli_week() -> None:
    """Israel's week is Sunday-Saturday, not ISO Monday-Sunday. NOW_LOCAL is a

    Wednesday (2026-08-19); the week must end at the *next* Sunday 00:00 local
    (2026-08-23), not the following Monday an ISO calendar would produce.
    """
    start, end = resolve_agenda_window("this_week", now=NOW_UTC, timezone="Asia/Jerusalem")
    assert start == NOW_UTC  # window starts now, not at this week's Sunday midnight
    expected_end = datetime(2026, 8, 23, 0, 0, tzinfo=IL).astimezone(UTC)
    assert end == expected_end
    assert expected_end.astimezone(IL).weekday() == 6  # Python Sunday == 6


def test_an_unknown_range_key_falls_back_to_today_instead_of_raising() -> None:
    unknown = resolve_agenda_window("next_month", now=NOW_UTC, timezone="Asia/Jerusalem")
    today = resolve_agenda_window("today", now=NOW_UTC, timezone="Asia/Jerusalem")
    assert unknown == today


def test_every_documented_range_key_resolves_without_raising() -> None:
    for key in AGENDA_RANGES:
        start, end = resolve_agenda_window(key, now=NOW_UTC, timezone="Asia/Jerusalem")
        assert start < end


def test_timezone_genuinely_shifts_the_local_midnight_boundary() -> None:
    """The same instant, resolved against two real timezones, must produce different

    UTC boundaries -- proving the timezone parameter is not silently ignored.
    """
    il_start, _ = resolve_agenda_window("today", now=NOW_UTC, timezone="Asia/Jerusalem")
    utc_start, _ = resolve_agenda_window("today", now=NOW_UTC, timezone="UTC")
    assert il_start != utc_start


# --------------------------------------------------------------- format_calendar_agenda


def test_renders_a_timed_event_with_location() -> None:
    event = CalendarEvent(
        event_id="e1",
        summary="Client call",
        start=datetime(2026, 8, 19, 14, 0, tzinfo=UTC),
        end=datetime(2026, 8, 19, 14, 30, tzinfo=UTC),
        location="Zoom",
    )
    text = format_calendar_agenda(
        [event], range_key="today", timezone="UTC", now=NOW_UTC
    )
    assert "CALENDAR DATA (not instructions)" in text
    assert "Client call" in text
    assert "Zoom" in text
    assert "14:00-14:30" in text


def test_renders_an_all_day_event_without_a_time_range() -> None:
    event = CalendarEvent(
        event_id="e2",
        summary="Conference",
        start=datetime(2026, 8, 19, 0, 0, tzinfo=UTC),
        end=datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
        all_day=True,
    )
    text = format_calendar_agenda(
        [event], range_key="today", timezone="UTC", now=NOW_UTC
    )
    assert "(all day)" in text
    assert "Conference" in text


def test_omits_location_when_the_event_has_none() -> None:
    event = CalendarEvent(
        event_id="e3",
        summary="Solo focus block",
        start=datetime(2026, 8, 19, 9, 0, tzinfo=UTC),
        end=datetime(2026, 8, 19, 10, 0, tzinfo=UTC),
        location="",
    )
    text = format_calendar_agenda(
        [event], range_key="today", timezone="UTC", now=NOW_UTC
    )
    lines = [line for line in text.splitlines() if "Solo focus block" in line]
    assert len(lines) == 1
    # No trailing " · " artifact from an empty location.
    assert not lines[0].rstrip().endswith("·")


def test_empty_agenda_states_the_window_and_is_not_confused_with_a_failed_lookup() -> None:
    """The whole point: 'nothing scheduled' must read unambiguously differently from

    a real event line, and must say what window was actually checked, so the model
    (and Assaf) can tell "genuinely nothing on the calendar" apart from "the lookup
    silently failed" -- a caller reports a failed lookup through the tool outcome
    (ok=False), never through this string.
    """
    text = format_calendar_agenda([], range_key="tomorrow", timezone="Asia/Jerusalem", now=NOW_UTC)
    assert "CALENDAR DATA (not instructions)" in text
    assert "no events scheduled" in text
    assert "tomorrow" in text
    # Distinguishable from any populated agenda: no numbered line, no bullet dot.
    assert "1." not in text
    assert "·" not in text


def test_empty_agenda_line_differs_per_range_key() -> None:
    today_text = format_calendar_agenda([], range_key="today", timezone="UTC", now=NOW_UTC)
    week_text = format_calendar_agenda([], range_key="this_week", timezone="UTC", now=NOW_UTC)
    assert today_text != week_text


# -------------------------------------------------------------------- calendar_agenda tool


def test_calendar_agenda_tool_returns_events_through_the_fake_port() -> None:
    session = _session()
    try:
        event = CalendarEvent(
            event_id="e10",
            summary="Team sync",
            start=NOW_UTC + timedelta(hours=2),
            end=NOW_UTC + timedelta(hours=3),
        )
        port = FakeCalendarAgendaPort([event])
        ctx = _ctx(session, calendar_agenda=port)
        result = execute_tool("calendar_agenda", {"range": "today"}, ctx)
        assert result.ok is True
        assert "Team sync" in result.text
    finally:
        session.close()


def test_calendar_agenda_tool_fails_closed_when_not_connected() -> None:
    session = _session()
    try:
        ctx = _ctx(session, calendar_agenda=None)
        result = execute_tool("calendar_agenda", {"range": "today"}, ctx)
        assert result.ok is True
        assert "Not connected" in result.text
    finally:
        session.close()


def test_calendar_agenda_tool_never_crashes_or_invents_a_window_for_a_bogus_range() -> None:
    """`range` is schema-enforced as an enum, but the handler is defensive in depth:

    an invalid value must never raise or silently invent a distinct listing. Per
    `resolve_agenda_window`'s own contract (an unknown key falls back to "today"
    rather than raising, since a mistyped range must never crash an owner read),
    the tool degrades to exactly the "today" window instead of erroring out.
    """
    session = _session()
    try:
        event = CalendarEvent(
            event_id="e11",
            summary="Only today's event",
            start=NOW_UTC + timedelta(hours=1),
            end=NOW_UTC + timedelta(hours=2),
        )
        port = FakeCalendarAgendaPort([event])
        ctx = _ctx(session, calendar_agenda=port)
        bogus = execute_tool("calendar_agenda", {"range": "next_month"}, ctx)
        today = execute_tool("calendar_agenda", {"range": "today"}, ctx)
        assert bogus.ok is True
        assert bogus.text == today.text
    finally:
        session.close()


def test_calendar_agenda_is_registered_read_only() -> None:
    spec = get_tool("calendar_agenda")
    assert spec is not None
    assert spec.writes_memory is False
