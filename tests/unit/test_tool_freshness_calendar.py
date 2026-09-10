from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from app.domain.meetings.availability import is_workday_local
from app.domain.sales import FitLevel, NextAction, PainLevel, SalesState
from app.integrations.calendar import (
    ComposioCalendarPort,
    DisabledCalendarPort,
    FakeCalendarPort,
    TimeSlot,
    prepare_meeting_offer,
)

IL_TZ = ZoneInfo("Asia/Jerusalem")
FIXED_NOW = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
OWNER_NOW = datetime(2026, 8, 21, 8, 0, tzinfo=UTC)
OFFER_COPY = "אפשר לקבוע שיחה קצרה עם אסף."
OFFER_LEAD_EMAIL = "cal.fresh.offer.1@example.com"
OWNER_FRESH_PHONE = "972509998601"
OWNER_FRESH_EMPTY_PHONE = "972509998602"


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


def _policy_gap(*, now: datetime, days_ahead: int = 4) -> TimeSlot:
    local = now.astimezone(IL_TZ) + timedelta(days=max(days_ahead, 2))
    for _ in range(8):
        if is_workday_local(local):
            break
        local = local + timedelta(days=1)
    gap_start = datetime(local.year, local.month, local.day, 10, 0, tzinfo=IL_TZ).astimezone(UTC)
    gap_end = datetime(local.year, local.month, local.day, 12, 0, tzinfo=IL_TZ).astimezone(UTC)
    return TimeSlot(start=gap_start, end=gap_end)


def test_prepare_meeting_offer_fake_freshness_live() -> None:
    slot = _policy_gap(now=FIXED_NOW)
    result = prepare_meeting_offer(
        reply=OFFER_COPY,
        next_action=NextAction.OFFER_MEETING.value,
        calendar=FakeCalendarPort([slot]),
        kill_switch=False,
        now=FIXED_NOW,
    )
    assert "זמין:" in result.reply
    assert "1." in result.reply
    assert "השיבו 1, 2, 3 כדי" in result.reply
    assert result.outcome is not None
    assert result.outcome.freshness == "live"
    assert result.outcome.status == "ok"


def test_prepare_meeting_offer_empty_freshness_unverified() -> None:
    for port in (DisabledCalendarPort(), FakeCalendarPort([])):
        result = prepare_meeting_offer(
            reply=OFFER_COPY,
            next_action=NextAction.OFFER_MEETING.value,
            calendar=port,
            kill_switch=False,
            now=FIXED_NOW,
        )
        assert result.reply == OFFER_COPY
        assert "1." not in result.reply
        assert result.outcome is not None
        assert result.outcome.freshness == "unverified"
        assert result.outcome.status == "empty"


def test_prepare_meeting_offer_kill_switch_freshness_empty() -> None:
    class RaisingCalendarPort:
        def find_free_slots(self, **_kwargs: object) -> list[TimeSlot]:
            raise RuntimeError("must not call port when kill switch is on")

    result = prepare_meeting_offer(
        reply=OFFER_COPY,
        next_action=NextAction.OFFER_MEETING.value,
        calendar=RaisingCalendarPort(),
        kill_switch=True,
        now=FIXED_NOW,
    )
    assert result.reply == OFFER_COPY
    assert result.outcome is not None
    assert result.outcome.freshness == ""
    assert result.outcome.status == "denied"


def test_prepare_meeting_offer_http_401_freshness_unverified() -> None:
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
    assert result.outcome is not None
    assert result.outcome.status == "unauthorized"
    assert result.outcome.freshness == "unverified"
