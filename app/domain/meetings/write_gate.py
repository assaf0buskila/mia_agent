"""Tel Aviv calendar write gate.

Write only when the event is a meeting near Tel Aviv, 09:00–17:00
Asia/Jerusalem, and the slot is empty. Weather chats never become meetings.
Otherwise ask Assaf. Missing facts are allowed. Inventing a location or time
is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.integrations.calendar import TimeSlot

IL_ZONE = "Asia/Jerusalem"
BUSINESS_OPEN = time(9, 0)
BUSINESS_CLOSE = time(17, 0)

ASK_ASSAF = (
    "לא כותבת ביומן. זה לא עובר את שער הפגישות בתל אביב "
    "(פגישה, 09:00–17:00 שעון ישראל, מקום ליד תל אביב, ומשבצת פנויה). "
    "תגיד לי במפורש אם ליצור, ואז אאשר איתך."
)

_MEETING_MARKERS = (
    "פגישה",
    "פגישת",
    "meeting",
    "שיחה",
    "call",
    "intro",
    "consult",
    "זום",
    "zoom",
    "sync",
)
_WEATHER_MARKERS = (
    "מזג",
    "weather",
    "גשם",
    "rain",
    "חום",
    "קור",
    "temperature",
    "forecast",
    "לחות",
    "humidity",
    "תחזית",
)
_TEL_AVIV_MARKERS = (
    "תל אביב",
    "תל-אביב",
    'ת"א',
    "tel aviv",
    "tel-aviv",
    "tlv",
    "יפו",
    "jaffa",
    "givatayim",
    "גבעתיים",
    "ramat gan",
    "רמת גן",
    "holon",
    "חולון",
    "bat yam",
    "בת ים",
    "bnei brak",
    "בני ברק",
)


@dataclass(frozen=True)
class CalendarWriteVerdict:
    allowed: bool
    reason: str
    ask_assaf: str = ASK_ASSAF


def looks_like_weather(text: str) -> bool:
    blob = text.casefold()
    return any(marker in blob for marker in _WEATHER_MARKERS)


def looks_like_meeting(text: str) -> bool:
    blob = text.casefold()
    return any(marker in blob for marker in _MEETING_MARKERS)


def near_tel_aviv(text: str) -> bool:
    blob = text.casefold()
    return any(marker in blob for marker in _TEL_AVIV_MARKERS)


def within_jerusalem_business_hours(start: datetime, end: datetime) -> bool:
    try:
        zone = ZoneInfo(IL_ZONE)
    except ZoneInfoNotFoundError:
        return False
    if start.tzinfo is None or end.tzinfo is None:
        return False
    local_start = start.astimezone(zone)
    local_end = end.astimezone(zone)
    if local_start.date() != local_end.date():
        return False
    if local_start.time() < BUSINESS_OPEN:
        return False
    if local_end.time() > BUSINESS_CLOSE:
        return False
    return local_start < local_end


def slot_covers(slots: list[TimeSlot], start: datetime, end: datetime) -> bool:
    return any(slot.start <= start and slot.end >= end for slot in slots)


def assess_calendar_write(
    *,
    title: str,
    start: datetime,
    end: datetime,
    location: str = "",
    slots: list[TimeSlot] | None = None,
) -> CalendarWriteVerdict:
    """Deterministic write gate. Missing location or a busy slot means ask Assaf."""
    place = f"{title} {location}".strip()
    if looks_like_weather(place):
        return CalendarWriteVerdict(False, "weather")
    if not looks_like_meeting(title):
        return CalendarWriteVerdict(False, "not_a_meeting")
    if not near_tel_aviv(place):
        return CalendarWriteVerdict(False, "not_tel_aviv")
    if not within_jerusalem_business_hours(start, end):
        return CalendarWriteVerdict(False, "outside_hours")
    if slots is not None and not slot_covers(slots, start, end):
        return CalendarWriteVerdict(False, "slot_busy")
    return CalendarWriteVerdict(True, "ok", ask_assaf="")
