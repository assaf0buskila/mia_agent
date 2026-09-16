import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from app.core.config import Settings
from app.domain.sales import FitLevel, NextAction, PainLevel, SalesState
from app.domain.tools import AdapterHttpError, AdapterResponseError, AdapterSchemaError
from app.integrations.calendar import (
    COMPOSIO_FIND_FREE_SLOTS_TOOL,
    COMPOSIO_GOOGLECALENDAR_VERSION,
    CalendarEvent,
    CalendarPort,
    ComposioCalendarAgendaPort,
    ComposioCalendarPort,
    DisabledCalendarPort,
    FakeCalendarAgendaPort,
    FakeCalendarPort,
    TimeSlot,
    build_calendar_port,
    enrich_meeting_offer,
    format_slot_time,
    prepare_meeting_offer,
    window_free_excluding_self,
)

LEAD_EMAIL = "cal.offer.1@example.com"
OFFER_COPY = "אפשר לקבוע שיחה קצרה עם אסף."
FIXED_NOW = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
IL_TZ = ZoneInfo("Asia/Jerusalem")


def _ready_to_meet_state(lead_id: str) -> SalesState:
    return SalesState(
        lead_id=lead_id,
        workflow_known=True,
        pain_level=PainLevel.P3,
        impact_confirmed=True,
        reflected=True,
        hypothesis_offered=True,
        authority_known=True,
        timeline_known=True,
        metric_known=True,
        buying_reality_known=True,
        fit=FitLevel.GOOD,
        willingness_to_meet=True,
        company_domain="clinic.co.il",
        missing_fields=[],
    )


def _slot_at(*, day_offset: int, hour: int, minutes: int = 30) -> TimeSlot:
    start = FIXED_NOW.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=day_offset, hours=hour
    )
    return TimeSlot(start=start, end=start + timedelta(minutes=minutes))


def _policy_gap(*, days_ahead: int, start_hour: int, end_hour: int) -> TimeSlot:
    local_date = (FIXED_NOW.astimezone(IL_TZ) + timedelta(days=days_ahead)).date()
    local_start = datetime(
        local_date.year, local_date.month, local_date.day, start_hour, 0, tzinfo=IL_TZ
    )
    local_end = datetime(
        local_date.year, local_date.month, local_date.day, end_hour, 0, tzinfo=IL_TZ
    )
    return TimeSlot(
        start=local_start.astimezone(UTC),
        end=local_end.astimezone(UTC),
    )


def test_fake_calendar_port_filters_by_duration_and_window() -> None:
    short = _slot_at(day_offset=1, hour=10, minutes=15)
    good = _slot_at(day_offset=1, hour=14, minutes=45)
    outside = TimeSlot(
        start=FIXED_NOW + timedelta(days=10, hours=10),
        end=FIXED_NOW + timedelta(days=10, hours=11),
    )
    port = FakeCalendarPort([short, good, outside])
    slots = port.find_free_slots(
        time_min=FIXED_NOW,
        time_max=FIXED_NOW + timedelta(days=7),
        duration_minutes=30,
    )
    assert len(slots) == 1
    assert slots[0].start == good.start


def test_disabled_calendar_port_returns_empty() -> None:
    port = DisabledCalendarPort()
    assert (
        port.find_free_slots(
            time_min=FIXED_NOW,
            time_max=FIXED_NOW + timedelta(days=7),
        )
        == []
    )


def test_enrich_meeting_offer_appends_slots_and_keeps_sales_copy() -> None:
    slot = _policy_gap(days_ahead=4, start_hour=10, end_hour=12)
    calendar = FakeCalendarPort([slot])
    enriched, _outcome = enrich_meeting_offer(
        reply=OFFER_COPY,
        next_action=NextAction.OFFER_MEETING.value,
        calendar=calendar,
        kill_switch=False,
        now=FIXED_NOW,
    )
    assert OFFER_COPY in enriched
    assert "אסף" in enriched
    assert "זמין:" in enriched
    assert "1." in enriched
    assert "השיבו 1, 2, 3 כדי" in enriched
    assert format_slot_time(slot.start, "Asia/Jerusalem")[:10] in enriched


def test_enrich_meeting_offer_disabled_calendar_keeps_static_reply() -> None:
    enriched, _outcome = enrich_meeting_offer(
        reply=OFFER_COPY,
        next_action=NextAction.OFFER_MEETING.value,
        calendar=DisabledCalendarPort(),
        kill_switch=False,
        now=FIXED_NOW,
    )
    assert enriched == OFFER_COPY


def test_enrich_meeting_offer_kill_switch_skips_port() -> None:
    class ExplodingCalendarPort:
        def find_free_slots(self, **_kwargs: object) -> list[TimeSlot]:
            raise RuntimeError("calendar must not be called when kill switch is on")

    enriched, outcome = enrich_meeting_offer(
        reply=OFFER_COPY,
        next_action=NextAction.OFFER_MEETING.value,
        calendar=ExplodingCalendarPort(),
        kill_switch=True,
        now=FIXED_NOW,
    )
    assert enriched == OFFER_COPY
    assert outcome.status == "denied"


def test_enrich_meeting_offer_ignores_non_meeting_actions() -> None:
    calendar = FakeCalendarPort([_slot_at(day_offset=1, hour=10)])
    enriched, outcome = enrich_meeting_offer(
        reply="hello",
        next_action=NextAction.UNDERSTAND_WORKFLOW.value,
        calendar=calendar,
        kill_switch=False,
        now=FIXED_NOW,
    )
    assert enriched == "hello"
    assert outcome is None


def test_calendar_protocol_is_read_only() -> None:
    names = {name for name in dir(CalendarPort) if not name.startswith("_")}
    assert "find_free_slots" in names
    assert not any(verb in name for name in names for verb in ("create", "delete", "update"))


def test_build_calendar_port_live_when_both_credentials_set() -> None:
    settings = Settings(composio_api_key="cmp-live", composio_user_id="user-123")
    port = build_calendar_port(settings)
    assert isinstance(port, ComposioCalendarPort)
    assert not isinstance(port, DisabledCalendarPort)


@pytest.mark.parametrize(
    "api_key,user_id",
    [
        ("", ""),
        ("cmp-live", ""),
        ("", "user-123"),
        ("   ", "user-123"),
        ("cmp-live", "   "),
    ],
)
def test_build_calendar_port_disabled_when_either_credential_missing(
    api_key: str,
    user_id: str,
) -> None:
    settings = Settings(composio_api_key=api_key, composio_user_id=user_id)
    port = build_calendar_port(settings)
    assert isinstance(port, DisabledCalendarPort)


def test_composio_calendar_port_http_500_raises_adapter_error() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    client = httpx.Client(transport=transport)
    port = ComposioCalendarPort(api_key="cmp-test", user_id="user-123", client=client)
    with pytest.raises(AdapterHttpError) as exc_info:
        port.find_free_slots(
            time_min=FIXED_NOW,
            time_max=FIXED_NOW + timedelta(days=7),
        )
    assert exc_info.value.status_code == 500


class _RaisingHttpClient:
    def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.HTTPError("network error")


def test_composio_calendar_port_network_error_raises_adapter_error() -> None:
    port = ComposioCalendarPort(
        api_key="cmp-test",
        user_id="user-123",
        client=_RaisingHttpClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.find_free_slots(
            time_min=FIXED_NOW,
            time_max=FIXED_NOW + timedelta(days=7),
        )
    assert exc_info.value.status_code is None


def test_prepare_meeting_offer_http_401_unauthorized_no_slots_suffix() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(401))
    client = httpx.Client(transport=transport)
    calendar = ComposioCalendarPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    result = prepare_meeting_offer(
        reply=OFFER_COPY,
        next_action=NextAction.OFFER_MEETING.value,
        calendar=calendar,
        kill_switch=False,
        now=FIXED_NOW,
    )
    assert result.reply == OFFER_COPY
    assert "זמין" not in result.reply
    assert result.slots == []
    assert result.outcome is not None
    assert result.outcome.status == "unauthorized"
    assert result.outcome.result_count == 0


def test_composio_calendar_port_unsuccessful_response_returns_empty() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"data": {}, "error": "tool failed", "successful": False},
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioCalendarPort(api_key="cmp-test", user_id="user-123", client=client)
    assert (
        port.find_free_slots(
            time_min=FIXED_NOW,
            time_max=FIXED_NOW + timedelta(days=7),
        )
        == []
    )


def test_composio_calendar_port_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": {"free_slots": []}, "error": None, "successful": True},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioCalendarPort(api_key="cmp-test", user_id="user-abc", client=client)
    port.find_free_slots(
        time_min=FIXED_NOW,
        time_max=FIXED_NOW + timedelta(days=7),
        calendar_id="primary",
        timezone="Asia/Jerusalem",
    )

    assert str(captured["url"]).endswith(f"/{COMPOSIO_FIND_FREE_SLOTS_TOOL}")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["user_id"] == "user-abc"
    assert body["version"] == COMPOSIO_GOOGLECALENDAR_VERSION
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["items"] == ["primary"]
    assert arguments["timezone"] == "Asia/Jerusalem"
    assert "time_min" in arguments
    assert "time_max" in arguments
    assert "text" not in body
    assert "text" not in arguments
    serialized = json.dumps(body)
    assert "CREATE" not in serialized.upper()


def test_composio_calendar_port_maps_free_slots_and_filters_duration() -> None:
    short_start = (FIXED_NOW + timedelta(days=1, hours=10)).isoformat()
    short_end = (FIXED_NOW + timedelta(days=1, hours=10, minutes=15)).isoformat()
    good_start = (FIXED_NOW + timedelta(days=1, hours=14)).isoformat()
    good_end = (FIXED_NOW + timedelta(days=1, hours=15)).isoformat()

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "free_slots": [
                        {"start": short_start, "end": short_end, "title": "ignore me"},
                        {"start": good_start, "end": good_end},
                    ]
                },
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioCalendarPort(api_key="cmp-test", user_id="user-123", client=client)
    slots = port.find_free_slots(
        time_min=FIXED_NOW,
        time_max=FIXED_NOW + timedelta(days=7),
        duration_minutes=30,
    )
    assert len(slots) == 1
    assert slots[0].start == datetime.fromisoformat(good_start)


def test_composio_calendar_port_computes_gaps_from_busy_calendars() -> None:
    window_start = FIXED_NOW
    window_end = FIXED_NOW + timedelta(hours=8)
    busy_start = (FIXED_NOW + timedelta(hours=2)).isoformat()
    busy_end = (FIXED_NOW + timedelta(hours=4)).isoformat()

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "calendars": {
                        "primary": {
                            "busy": [{"start": busy_start, "end": busy_end}],
                        }
                    }
                },
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioCalendarPort(api_key="cmp-test", user_id="user-123", client=client)
    slots = port.find_free_slots(
        time_min=window_start,
        time_max=window_end,
        duration_minutes=30,
    )
    assert len(slots) == 2
    assert slots[0].start == window_start
    assert slots[0].end == datetime.fromisoformat(busy_start)
    assert slots[1].start == datetime.fromisoformat(busy_end)
    assert slots[1].end == window_end


def test_composio_calendar_port_calendar_errors_return_empty() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "calendars": {
                        "primary": {
                            "errors": [{"domain": "global", "reason": "notFound"}],
                        }
                    }
                },
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioCalendarPort(api_key="cmp-test", user_id="user-123", client=client)
    assert (
        port.find_free_slots(
            time_min=FIXED_NOW,
            time_max=FIXED_NOW + timedelta(hours=8),
            duration_minutes=30,
        )
        == []
    )


def test_composio_calendar_port_protocol_is_read_only() -> None:
    forbidden = ("create", "update", "delete")
    for name in dir(ComposioCalendarPort):
        if name.startswith("_"):
            continue
        lowered = name.lower()
        assert not any(token in lowered for token in forbidden)



# ------------------------------------------------------------- window_free_excluding_self


class _RaisingCalendarPort:
    """Proves the destination-overlaps-self branch never consults merged
    free/busy -- it cannot tell the event's own busy block apart from a
    different meeting that happens to overlap it (see the scenario B/C/D
    tests below), so it must go through the per-event agenda read instead.
    """

    def find_free_slots(self, **_kwargs: object) -> list[TimeSlot]:
        raise AssertionError("free/busy must not be queried when self overlaps the destination")


def test_window_free_excluding_self_ignores_the_events_own_current_span() -> None:
    """The event being moved is still "busy" at its old time until the move
    executes; that busy block must not be read as a conflict with the event's
    own destination time (the C5 reschedule-into-self-overlap defect) -- as
    long as no *other* event also overlaps the destination.
    """
    self_start = FIXED_NOW
    self_end = FIXED_NOW + timedelta(minutes=30)
    window_start = FIXED_NOW + timedelta(minutes=15)
    window_end = window_start + timedelta(minutes=30)
    agenda = FakeCalendarAgendaPort(
        [CalendarEvent(event_id="self-evt", summary="Self", start=self_start, end=self_end)]
    )
    assert (
        window_free_excluding_self(
            _RaisingCalendarPort(),
            window_start=window_start,
            window_end=window_end,
            self_start=self_start,
            self_end=self_end,
            self_event_id="self-evt",
            agenda=agenda,
        )
        is True
    )


def test_window_free_excluding_self_still_refuses_another_events_slot() -> None:
    """Excluding the event's own span must not swallow a genuine conflict with
    a different event sitting in the destination window.
    """
    self_start = FIXED_NOW
    self_end = FIXED_NOW + timedelta(minutes=30)
    other_start = FIXED_NOW + timedelta(hours=1)
    other_end = other_start + timedelta(minutes=30)
    window_start = other_start + timedelta(minutes=15)
    window_end = window_start + timedelta(minutes=30)
    calendar = FakeCalendarPort(
        [
            TimeSlot(start=self_end, end=other_start),
            TimeSlot(start=other_end, end=other_end + timedelta(minutes=15)),
        ]
    )
    assert (
        window_free_excluding_self(
            calendar,
            window_start=window_start,
            window_end=window_end,
            self_start=self_start,
            self_end=self_end,
        )
        is False
    )


def test_window_free_excluding_self_scenario_b_other_meeting_overlapping_self_span() -> None:
    """Reviewer counterexample B: other 10:00-11:00, event(self) 10:30-11:30,
    move to 10:45-11:15. Must be refused -- "other" occupies 10:45-11:00 of
    the destination -- even though the destination also overlaps self's own
    current span.
    """
    other_start = FIXED_NOW
    other_end = other_start + timedelta(hours=1)
    self_start = FIXED_NOW + timedelta(minutes=30)
    self_end = self_start + timedelta(hours=1)
    window_start = FIXED_NOW + timedelta(minutes=45)
    window_end = window_start + timedelta(minutes=30)
    agenda = FakeCalendarAgendaPort(
        [
            CalendarEvent(event_id="self-evt", summary="Self", start=self_start, end=self_end),
            CalendarEvent(event_id="other-evt", summary="Other", start=other_start, end=other_end),
        ]
    )
    assert (
        window_free_excluding_self(
            _RaisingCalendarPort(),
            window_start=window_start,
            window_end=window_end,
            self_start=self_start,
            self_end=self_end,
            self_event_id="self-evt",
            agenda=agenda,
        )
        is False
    )


def test_window_free_excluding_self_scenario_c_other_meeting_inside_self_span() -> None:
    """Reviewer counterexample C: event(self) 10:00-11:00, other 10:15-10:45
    sits entirely inside self's own span, move to 10:30-11:30. Must be
    refused -- "other" occupies 10:30-10:45 of the destination.
    """
    self_start = FIXED_NOW
    self_end = self_start + timedelta(hours=1)
    other_start = FIXED_NOW + timedelta(minutes=15)
    other_end = FIXED_NOW + timedelta(minutes=45)
    window_start = FIXED_NOW + timedelta(minutes=30)
    window_end = window_start + timedelta(hours=1)
    agenda = FakeCalendarAgendaPort(
        [
            CalendarEvent(event_id="self-evt", summary="Self", start=self_start, end=self_end),
            CalendarEvent(event_id="other-evt", summary="Other", start=other_start, end=other_end),
        ]
    )
    assert (
        window_free_excluding_self(
            _RaisingCalendarPort(),
            window_start=window_start,
            window_end=window_end,
            self_start=self_start,
            self_end=self_end,
            self_event_id="self-evt",
            agenda=agenda,
        )
        is False
    )


def test_window_free_excluding_self_scenario_d_destination_equals_current_slot() -> None:
    """Reviewer counterexample D: destination equals the event's own current
    slot 10:00-11:00, with another meeting at 10:30-11:00 inside it. Must be
    refused even though the destination is exactly the self span.
    """
    self_start = FIXED_NOW
    self_end = self_start + timedelta(hours=1)
    other_start = FIXED_NOW + timedelta(minutes=30)
    other_end = self_end
    agenda = FakeCalendarAgendaPort(
        [
            CalendarEvent(event_id="self-evt", summary="Self", start=self_start, end=self_end),
            CalendarEvent(event_id="other-evt", summary="Other", start=other_start, end=other_end),
        ]
    )
    assert (
        window_free_excluding_self(
            _RaisingCalendarPort(),
            window_start=self_start,
            window_end=self_end,
            self_start=self_start,
            self_end=self_end,
            self_event_id="self-evt",
            agenda=agenda,
        )
        is False
    )


def test_window_free_excluding_self_refuses_when_the_agenda_read_fails() -> None:
    """If the destination overlaps self and the per-event agenda read fails,
    the reschedule must be refused (fail closed), never silently allowed.
    """

    class _RaisingAgenda:
        def list_events(self, **_kwargs: object) -> list[CalendarEvent]:
            raise AdapterResponseError()

        def list_events_strict(self, **_kwargs: object) -> list[CalendarEvent]:
            raise AdapterResponseError()

    self_start = FIXED_NOW
    self_end = self_start + timedelta(minutes=30)
    window_start = FIXED_NOW + timedelta(minutes=15)
    window_end = window_start + timedelta(minutes=30)
    assert (
        window_free_excluding_self(
            _RaisingCalendarPort(),
            window_start=window_start,
            window_end=window_end,
            self_start=self_start,
            self_end=self_end,
            self_event_id="self-evt",
            agenda=_RaisingAgenda(),
        )
        is False
    )


def test_window_free_excluding_self_refuses_when_agenda_is_unavailable() -> None:
    """No agenda port at all (house not connected) must also fail closed when
    the destination overlaps self, rather than defaulting to "free".
    """
    self_start = FIXED_NOW
    self_end = self_start + timedelta(minutes=30)
    window_start = FIXED_NOW + timedelta(minutes=15)
    window_end = window_start + timedelta(minutes=30)
    assert (
        window_free_excluding_self(
            _RaisingCalendarPort(),
            window_start=window_start,
            window_end=window_end,
            self_start=self_start,
            self_end=self_end,
            self_event_id="self-evt",
            agenda=None,
        )
        is False
    )


def test_window_free_excluding_self_refuses_when_agenda_lacks_list_events_strict() -> None:
    """An agenda port that does not implement `list_events_strict` (a partial
    double, or a future port that only supports merged free/busy) must fail
    closed like a missing agenda, never raise AttributeError at approval time.
    """

    class _NoStrictReadAgenda:
        def list_events(self, **_kwargs: object) -> list[CalendarEvent]:
            raise AssertionError("must not fall back to the merged read")

    self_start = FIXED_NOW
    self_end = self_start + timedelta(minutes=30)
    window_start = FIXED_NOW + timedelta(minutes=15)
    window_end = window_start + timedelta(minutes=30)
    assert (
        window_free_excluding_self(
            _RaisingCalendarPort(),
            window_start=window_start,
            window_end=window_end,
            self_start=self_start,
            self_end=self_end,
            self_event_id="self-evt",
            agenda=_NoStrictReadAgenda(),
        )
        is False
    )


def test_window_free_excluding_self_with_no_self_falls_back_to_plain_free_check() -> None:
    slot = _slot_at(day_offset=1, hour=10, minutes=30)
    calendar = FakeCalendarPort([slot])
    assert (
        window_free_excluding_self(
            calendar,
            window_start=slot.start,
            window_end=slot.end,
            self_start=None,
            self_end=None,
        )
        is True
    )


# --------------------------------------------------------- ComposioCalendarAgendaPort errors


def test_composio_calendar_agenda_port_unsuccessful_response_raises_response_error() -> None:
    """`successful: false` is a real provider failure, never a silently empty day."""
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"data": {}, "error": "tool failed", "successful": False},
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioCalendarAgendaPort(api_key="cmp-test", user_id="user-123", client=client)
    with pytest.raises(AdapterResponseError):
        port.list_events(start=FIXED_NOW, end=FIXED_NOW + timedelta(days=1))


def test_composio_calendar_agenda_port_missing_successful_key_raises_schema_error() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"data": {"items": []}})
    )
    client = httpx.Client(transport=transport)
    port = ComposioCalendarAgendaPort(api_key="cmp-test", user_id="user-123", client=client)
    with pytest.raises(AdapterSchemaError):
        port.list_events(start=FIXED_NOW, end=FIXED_NOW + timedelta(days=1))


def test_composio_calendar_agenda_port_successful_but_missing_items_raises_schema_error() -> None:
    """`successful: true` with no usable `items` list is a shape this adapter does
    not understand -- it must never be read back as "nothing scheduled".
    """
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, json={"data": {"response_data": {}}, "successful": True}
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioCalendarAgendaPort(api_key="cmp-test", user_id="user-123", client=client)
    with pytest.raises(AdapterSchemaError):
        port.list_events(start=FIXED_NOW, end=FIXED_NOW + timedelta(days=1))


def test_composio_calendar_agenda_port_genuinely_empty_day_returns_empty_list() -> None:
    """The one case that must NOT raise: a real, successful, empty agenda."""
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, json={"data": {"response_data": {"items": []}}, "successful": True}
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioCalendarAgendaPort(api_key="cmp-test", user_id="user-123", client=client)
    assert port.list_events(start=FIXED_NOW, end=FIXED_NOW + timedelta(days=1)) == []



# ---------------------------------------- C5-hardening: fail-closed P3 follow-ups


class _FixedAgenda:
    """Test double whose list_events_strict returns a fixed, pre-resolved
    event list regardless of the requested window -- stands in for a
    provider (Google) that has already resolved the true overlap (including
    a correctly-interpreted all-day local calendar day), which our own
    parsed CalendarEvent fields cannot always reproduce with plain UTC math.
    """

    def __init__(self, events: list[CalendarEvent]) -> None:
        self._events = events

    def list_events_strict(self, **_kwargs: object) -> list[CalendarEvent]:
        return self._events


class _StrictRaisingAgenda:
    def list_events_strict(self, **_kwargs: object) -> list[CalendarEvent]:
        raise AdapterSchemaError()


def test_window_free_excluding_self_refuses_all_day_event_near_local_midnight() -> None:
    """P3 #1: an all-day event's date-only start/end is the provider's local
    calendar day, not UTC midnight (see _parse_agenda_date) -- comparing it
    against the destination window with plain UTC math can wrongly say
    "free" for a move that lands inside that local day (e.g. 00:30-01:00
    Asia/Jerusalem on the same date the all-day event covers). Any other
    all-day item the agenda read returns is now busy outright, without
    re-doing that unreliable comparison.
    """
    self_start = FIXED_NOW
    self_end = self_start + timedelta(minutes=30)
    window_start = self_start + timedelta(minutes=15)
    window_end = window_start + timedelta(minutes=30)
    all_day_event = CalendarEvent(
        event_id="all-day-evt",
        summary="Conference",
        start=datetime(2026, 9, 21, tzinfo=UTC),
        end=datetime(2026, 9, 22, tzinfo=UTC),
        all_day=True,
    )
    agenda = _FixedAgenda([all_day_event])
    assert (
        window_free_excluding_self(
            _RaisingCalendarPort(),
            window_start=window_start,
            window_end=window_end,
            self_start=self_start,
            self_end=self_end,
            self_event_id="self-evt",
            agenda=agenda,
        )
        is False
    )


def test_window_free_excluding_self_still_ignores_the_self_event_even_if_all_day() -> None:
    """The moved event itself may be all-day; excluding it by id must still
    work even though the new all-day handling would otherwise treat any
    *other* returned all-day item as busy outright.
    """
    self_start = datetime(2026, 9, 21, tzinfo=UTC)
    self_end = datetime(2026, 9, 22, tzinfo=UTC)
    window_start = self_start
    window_end = self_start + timedelta(hours=1)
    self_event = CalendarEvent(
        event_id="self-evt",
        summary="Self all-day",
        start=self_start,
        end=self_end,
        all_day=True,
    )
    agenda = _FixedAgenda([self_event])
    assert (
        window_free_excluding_self(
            _RaisingCalendarPort(),
            window_start=window_start,
            window_end=window_end,
            self_start=self_start,
            self_end=self_end,
            self_event_id="self-evt",
            agenda=agenda,
        )
        is True
    )


def test_window_free_excluding_self_refuses_on_strict_pagination_or_skip() -> None:
    """P3 #2/#3: window_free_excluding_self must call the strict read and
    fail closed on whatever it raises (pagination or an unparseable item),
    not just a transport-level AdapterHttpError.
    """
    self_start = FIXED_NOW
    self_end = self_start + timedelta(minutes=30)
    window_start = self_start + timedelta(minutes=15)
    window_end = window_start + timedelta(minutes=30)
    assert (
        window_free_excluding_self(
            _RaisingCalendarPort(),
            window_start=window_start,
            window_end=window_end,
            self_start=self_start,
            self_end=self_end,
            self_event_id="self-evt",
            agenda=_StrictRaisingAgenda(),
        )
        is False
    )


def _agenda_item(event_id: str, *, start_iso: str, end_iso: str) -> dict[str, object]:
    return {
        "id": event_id,
        "status": "confirmed",
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }


def test_composio_calendar_agenda_port_list_events_strict_raises_on_next_page_token() -> None:
    """P3 #2: a page with items plus a nextPageToken means more results exist
    beyond what was fetched -- the strict read must refuse rather than let
    the safety check reason from a partial page.
    """
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "response_data": {
                        "items": [
                            _agenda_item(
                                "evt-1",
                                start_iso=FIXED_NOW.isoformat(),
                                end_iso=(FIXED_NOW + timedelta(minutes=30)).isoformat(),
                            )
                        ],
                        "nextPageToken": "page-2",
                    }
                },
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioCalendarAgendaPort(api_key="cmp-test", user_id="user-123", client=client)
    with pytest.raises(AdapterSchemaError):
        port.list_events_strict(start=FIXED_NOW, end=FIXED_NOW + timedelta(hours=1))


def test_composio_calendar_agenda_port_list_events_strict_raises_on_empty_page_with_token() -> None:
    """Reviewer probe: `items: []` plus a `nextPageToken` must not read as a
    genuinely free window.
    """
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {"response_data": {"items": [], "nextPageToken": "page-2"}},
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioCalendarAgendaPort(api_key="cmp-test", user_id="user-123", client=client)
    with pytest.raises(AdapterSchemaError):
        port.list_events_strict(start=FIXED_NOW, end=FIXED_NOW + timedelta(hours=1))


def test_composio_calendar_agenda_port_list_events_strict_raises_on_unparseable_item() -> None:
    """P3 #3: an item missing `end` cannot be parsed and is silently dropped
    by `_parse_agenda_event` for display -- but the safety check must refuse
    rather than treat the window as if that event did not exist.
    """
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "response_data": {
                        "items": [
                            _agenda_item(
                                "evt-1",
                                start_iso=FIXED_NOW.isoformat(),
                                end_iso=(FIXED_NOW + timedelta(minutes=30)).isoformat(),
                            ),
                            {
                                "id": "evt-2",
                                "status": "confirmed",
                                "start": {"dateTime": FIXED_NOW.isoformat()},
                                "end": {},
                            },
                        ]
                    }
                },
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioCalendarAgendaPort(api_key="cmp-test", user_id="user-123", client=client)
    with pytest.raises(AdapterSchemaError):
        port.list_events_strict(start=FIXED_NOW, end=FIXED_NOW + timedelta(hours=1))


def test_composio_calendar_agenda_port_list_events_strict_ignores_cancelled_items() -> None:
    """A cancelled event is legitimately absent, not a parse failure -- it
    must not trip the strict skipped-item refusal.
    """
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "response_data": {
                        "items": [
                            _agenda_item(
                                "evt-1",
                                start_iso=FIXED_NOW.isoformat(),
                                end_iso=(FIXED_NOW + timedelta(minutes=30)).isoformat(),
                            ),
                            {"id": "evt-2", "status": "cancelled"},
                        ]
                    }
                },
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioCalendarAgendaPort(api_key="cmp-test", user_id="user-123", client=client)
    events = port.list_events_strict(start=FIXED_NOW, end=FIXED_NOW + timedelta(hours=1))
    assert [event.event_id for event in events] == ["evt-1"]


def test_composio_calendar_agenda_port_list_events_tolerates_pagination_and_skips() -> None:
    """Display (`list_events`) is unaffected by the strict hardening -- it
    still truncates/skips silently rather than raising.
    """
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "response_data": {
                        "items": [
                            _agenda_item(
                                "evt-1",
                                start_iso=FIXED_NOW.isoformat(),
                                end_iso=(FIXED_NOW + timedelta(minutes=30)).isoformat(),
                            ),
                            {
                                "id": "evt-2",
                                "start": {"dateTime": FIXED_NOW.isoformat()},
                                "end": {},
                            },
                        ],
                        "nextPageToken": "page-2",
                    }
                },
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioCalendarAgendaPort(api_key="cmp-test", user_id="user-123", client=client)
    events = port.list_events(start=FIXED_NOW, end=FIXED_NOW + timedelta(hours=1))
    assert [event.event_id for event in events] == ["evt-1"]


def test_composio_calendar_agenda_port_sends_connected_account_id_when_bound() -> None:
    """C5-hardening #4: the agenda adapter must be able to bind to one
    specific provider connection, the same as ComposioCalendarPort and
    ComposioCalendarBookingPort, so the approval re-check can pin it to the
    approved connection.
    """
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200, json={"data": {"response_data": {"items": []}}, "successful": True}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = ComposioCalendarAgendaPort(
        api_key="cmp-test",
        user_id="user-123",
        connected_account_id="conn-abc",
        client=client,
    )
    port.list_events(start=FIXED_NOW, end=FIXED_NOW + timedelta(hours=1))
    assert captured.get("connected_account_id") == "conn-abc"
