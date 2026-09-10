import importlib
import inspect
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.capabilities import CapabilityId, require_alive
from app.db.models import CanonicalEventRow, OwnerTaskRow, ToolRunRow
from app.domain.meetings.availability import is_workday_local
from app.integrations.calendar import (
    TimeSlot,
)
from sqlalchemy import delete

IL = ZoneInfo("Asia/Jerusalem")
BASE_NOW = datetime(2026, 8, 21, 8, 0, tzinfo=UTC)
OWNER_PHONE = "972509994901"
OWNER_EVENT = "evt.owner.cal.inbound.1"
OWNER_EVENT_KILL = "evt.owner.cal.inbound.2"


def _next_workday(local_dt: datetime) -> datetime:
    """Advance to the next Sun-Thu day, using the product's own workday rule."""
    for _ in range(7):
        if is_workday_local(local_dt):
            return local_dt
        local_dt = local_dt + timedelta(days=1)
    return local_dt


def _policy_gap(*, now: datetime | None = None) -> TimeSlot:
    clock = now if now is not None else BASE_NOW
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)
    # Seed the free window on a real working day. A fixed +4 lands on Friday whenever the
    # suite runs on a Monday, and the Sun-Thu policy correctly rejects it — which looked
    # like a broken calendar every Monday instead of a broken fixture.
    local_date = _next_workday(clock.astimezone(IL) + timedelta(days=4)).date()
    gap_start = datetime(
        local_date.year, local_date.month, local_date.day, 8, 0, tzinfo=IL
    ).astimezone(UTC)
    gap_end = datetime(
        local_date.year, local_date.month, local_date.day, 18, 0, tzinfo=IL
    ).astimezone(UTC)
    return TimeSlot(start=gap_start, end=gap_end)


class RaisingCalendarPort:
    def find_free_slots(self, **_kwargs: object) -> list[TimeSlot]:
        raise RuntimeError("port must not be called")


def _delete_test_rows(db, *, event_ids: tuple[str, ...]) -> None:
    for event_id in event_ids:
        db.execute(
            delete(OwnerTaskRow).where(
                OwnerTaskRow.provider == "whatsapp",
                OwnerTaskRow.provider_event_id == event_id,
            )
        )
        db.execute(
            delete(ToolRunRow).where(
                ToolRunRow.provider_event_id == f"{event_id}:tool:calendar_find_free_slots",
            )
        )
        db.execute(
            delete(CanonicalEventRow).where(
                CanonicalEventRow.provider == "whatsapp",
                CanonicalEventRow.provider_event_id == event_id,
            )
        )
        db.execute(
            delete(CanonicalEventRow).where(
                CanonicalEventRow.provider == "whatsapp",
                CanonicalEventRow.provider_event_id == f"{event_id}:out",
            )
        )
        db.execute(
            delete(CanonicalEventRow).where(
                CanonicalEventRow.provider == "whatsapp",
                CanonicalEventRow.provider_event_id == f"{event_id}:tool:calendar_find_free_slots",
            )
        )
    db.commit()




def test_owner_calendar_module_no_forbidden_imports() -> None:
    module = importlib.import_module("app.domain.owner.calendar")
    source = inspect.getsource(module)
    assert "MessagePort" not in source
    assert "CREATE_EVENT" not in source
    assert "PATCH_EVENT" not in source
    assert "attendees" not in source


def test_require_alive_owner_calendar() -> None:
    require_alive(CapabilityId.OWNER_CALENDAR)
