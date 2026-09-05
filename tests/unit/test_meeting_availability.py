"""ADR-012 meeting availability policy tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.domain.meetings.availability import (
    BUSINESS_CLOSE,
    carve_policy_slots,
    is_workday_local,
    meets_notice_requirement,
    slot_is_bookable,
)
from app.integrations.calendar import TimeSlot

IL = ZoneInfo("Asia/Jerusalem")
# Thursday 2026-08-20 09:00 IL
BASE_NOW = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)


def _local_slot(*, days_ahead: int, hour: int, minute: int = 0) -> tuple[datetime, datetime]:
    local_now = BASE_NOW.astimezone(IL)
    local_start = (local_now + timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    utc_start = local_start.astimezone(UTC)
    return utc_start, utc_start + timedelta(minutes=30)


def test_workdays_sun_thu_only() -> None:
    fri = datetime(2026, 8, 21, 10, 0, tzinfo=IL)
    sat = datetime(2026, 8, 22, 10, 0, tzinfo=IL)
    sun = datetime(2026, 8, 23, 10, 0, tzinfo=IL)
    assert is_workday_local(fri) is False
    assert is_workday_local(sat) is False
    assert is_workday_local(sun) is True


def test_rejects_friday_and_weekend_slots() -> None:
    fri_start, fri_end = _local_slot(days_ahead=1, hour=10)
    assert slot_is_bookable(
        fri_start, fri_end, now=BASE_NOW, timezone="Asia/Jerusalem"
    ) is False


def test_thursday_to_sunday_first_valid_workday() -> None:
    thu_evening = datetime(2026, 8, 20, 16, 0, tzinfo=IL).astimezone(UTC)
    sun_start, sun_end = _local_slot(days_ahead=3, hour=10)
    assert meets_notice_requirement(start_utc=sun_start, now_utc=thu_evening)


def test_24h_boundary_exact() -> None:
    start, _end = _local_slot(days_ahead=4, hour=10)
    exactly = start - timedelta(hours=24)
    assert meets_notice_requirement(start_utc=start, now_utc=exactly)
    inside = start - timedelta(hours=24) + timedelta(seconds=1)
    assert not meets_notice_requirement(start_utc=start, now_utc=inside)


def test_half_hour_alignment_from_gap() -> None:
    local_date = (BASE_NOW.astimezone(IL) + timedelta(days=4)).date()
    gap_start = datetime(
        local_date.year, local_date.month, local_date.day, 9, 17, tzinfo=IL
    ).astimezone(UTC)
    gap_end = datetime(
        local_date.year, local_date.month, local_date.day, 12, 0, tzinfo=IL
    ).astimezone(UTC)
    slots = carve_policy_slots(
        [TimeSlot(start=gap_start, end=gap_end)],
        timezone="Asia/Jerusalem",
        now=BASE_NOW,
        max_slots=3,
    )
    assert slots
    for slot in slots:
        local = slot.start.astimezone(IL)
        assert local.minute in {0, 30}
        assert local.time() >= datetime(2026, 1, 1, 9, 0, tzinfo=IL).time()
        assert slot.end.astimezone(IL).time() <= BUSINESS_CLOSE


def test_dst_transition_still_bookable() -> None:
    # Israel DST ends last Sunday of October; 2026-10-25 10:00 IL
    dst_now = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)
    local = datetime(2026, 10, 25, 10, 0, tzinfo=IL)
    start = local.astimezone(UTC)
    end = start + timedelta(minutes=30)
    assert slot_is_bookable(start, end, now=dst_now, timezone="Asia/Jerusalem")


def test_long_gap_carves_max_three_exact_slots() -> None:
    local_date = (BASE_NOW.astimezone(IL) + timedelta(days=4)).date()
    gap_start = datetime(
        local_date.year, local_date.month, local_date.day, 8, 0, tzinfo=IL
    ).astimezone(UTC)
    gap_end = datetime(
        local_date.year, local_date.month, local_date.day, 18, 0, tzinfo=IL
    ).astimezone(UTC)
    slots = carve_policy_slots(
        [TimeSlot(start=gap_start, end=gap_end)],
        timezone="Asia/Jerusalem",
        now=BASE_NOW,
        max_slots=3,
    )
    assert len(slots) == 3
    assert slots[0].end == slots[1].start
    assert slots[1].end == slots[2].start


@pytest.mark.parametrize("count", [1, 2, 3])
def test_prepare_prompt_dynamic_indices(count: int) -> None:
    from app.integrations.calendar import prepare_meeting_offer

    local_date = (BASE_NOW.astimezone(IL) + timedelta(days=4)).date()
    gap_start = datetime(
        local_date.year, local_date.month, local_date.day, 8, 0, tzinfo=IL
    ).astimezone(UTC)
    gap_end = datetime(
        local_date.year, local_date.month, local_date.day, 18, 0, tzinfo=IL
    ).astimezone(UTC)
    from app.integrations.calendar import FakeCalendarPort

    result = prepare_meeting_offer(
        reply="base",
        next_action="offer_meeting",
        calendar=FakeCalendarPort([TimeSlot(start=gap_start, end=gap_end)]),
        kill_switch=False,
        timezone="Asia/Jerusalem",
        now=BASE_NOW,
        max_slots=count,
    )
    assert len(result.slots) == count
    expected = ", ".join(str(i) for i in range(1, count + 1))
    assert f"השיבו {expected} כדי לאשר." in result.reply
