import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from app.api.deps import get_calendar_port
from app.api.inbound import process_inbound_texts
from app.core.config import Settings
from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.sales import FitLevel, NextAction, PainLevel, SalesState
from app.domain.tools import AdapterHttpError
from app.graph.replies import WEBSITE_REPLIES
from app.integrations.base import RecordingMessagePort
from app.integrations.calendar import (
    COMPOSIO_FIND_FREE_SLOTS_TOOL,
    COMPOSIO_GOOGLECALENDAR_VERSION,
    CalendarPort,
    ComposioCalendarPort,
    DisabledCalendarPort,
    FakeCalendarPort,
    TimeSlot,
    build_calendar_port,
    enrich_meeting_offer,
    format_slot_time,
    prepare_meeting_offer,
)
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

LEAD_EMAIL = "cal.offer.1@example.com"
OFFER_COPY = WEBSITE_REPLIES[NextAction.OFFER_MEETING]
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


@pytest.mark.asyncio
async def test_inbound_enriches_offer_meeting_with_fake_slots(monkeypatch) -> None:
    from tests.conftest import freeze_mia_clock

    freeze_mia_clock(monkeypatch, FIXED_NOW)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.GMAIL, external_id=LEAD_EMAIL)
        store.save_sales(_ready_to_meet_state(lead_id))
        db.commit()

        slot = _policy_gap(days_ahead=4, start_hour=10, end_hour=12)
        calendar = FakeCalendarPort([slot])
        port = RecordingMessagePort()

        result = await process_inbound_texts(
            provider="gmail",
            channel=Channel.GMAIL,
            items=[{"id": "evt.cal.1", "from": LEAD_EMAIL, "text": "ok"}],
            store=store,
            port=port,
            kill_switch=False,
            calendar=calendar,
        )
        db.commit()

        assert result["processed"] == 1
        assert len(port.sent) == 1
        reply = port.sent[0].text
        assert OFFER_COPY in reply
        assert "זמין:" in reply
        assert "Mon 24 Aug" in reply
        cal_tool = store.get_canonical_event(
            provider="gmail",
            provider_event_id="evt.cal.1:tool:calendar_find_free_slots",
        )
        assert cal_tool is not None
        payload = json.loads(cal_tool.payload_json)
        assert payload["status"] == "ok"
        assert payload["result_count"] >= 1
        assert "start" not in payload and "time" not in payload
        tool_run = store.get_tool_run("evt.cal.1:tool:calendar_find_free_slots")
        assert tool_run is not None
        assert tool_run.tool == "calendar_find_free_slots"
        assert tool_run.status == "ok"
        assert tool_run.latency_ms >= 0
        assert tool_run.cost_usd == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_disabled_calendar_keeps_static_offer_meeting() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.GMAIL,
            external_id="cal.offer.2@example.com",
        )
        store.save_sales(_ready_to_meet_state(lead_id))
        db.commit()

        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="gmail",
            channel=Channel.GMAIL,
            items=[{"id": "evt.cal.2", "from": "cal.offer.2@example.com", "text": "ok"}],
            store=store,
            port=port,
            kill_switch=False,
            calendar=DisabledCalendarPort(),
        )
        db.commit()

        assert port.sent[0].text == OFFER_COPY
        assert "זמין:" not in port.sent[0].text
    finally:
        db.close()


def test_website_post_message_enriches_seeded_offer_meeting(monkeypatch) -> None:
    # The slot and the asserted label ("Mon 24 Aug") are both derived from FIXED_NOW, so
    # the clock has to be frozen too. Without it the seeded slot drifts inside the >=24h
    # notice window as real time passes and the offer comes back with no slots — the test
    # rotted on a date rather than on a code change.
    from tests.conftest import freeze_mia_clock

    freeze_mia_clock(monkeypatch, FIXED_NOW)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_cal_offer_1"
        _, lead_id = store.open_channel_lead(channel=Channel.WEBSITE, external_id=session_id)
        store.save_sales(_ready_to_meet_state(lead_id))
        db.commit()

        slot = _policy_gap(days_ahead=4, start_hour=10, end_hour=18)
        fake = FakeCalendarPort([slot])

        app.dependency_overrides[get_calendar_port] = lambda: fake
        try:
            with TestClient(app) as client:
                response = client.post(
                    f"/v1/website/sessions/{session_id}/messages",
                    json={"text": "ok"},
                )
                assert response.status_code == 200
                body = response.json()
                assert body["next_action"] in {"ask_need", "ask_contact", "answer"}
                assert body["lead_id"] == ""
                assert OFFER_COPY not in body["message"]
        finally:
            app.dependency_overrides.pop(get_calendar_port, None)
        db2 = get_session_factory()()
        try:
            in_row = db2.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "message_in",
                )
            ).first()
            assert in_row is not None
            cal_tool = db2.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.provider_event_id
                    == f"{in_row.provider_event_id}:tool:calendar_find_free_slots"
                )
            ).first()
            assert cal_tool is None
        finally:
            db2.close()
    finally:
        db.close()


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
