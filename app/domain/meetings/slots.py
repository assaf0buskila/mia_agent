"""Offered meeting slot validation, persistence helpers, and confirmation parser."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from pydantic import BaseModel, field_validator

from app.domain.meetings.availability import MAX_POLICY_SLOTS, slot_is_bookable
from app.integrations.calendar import DEFAULT_MEETING_MINUTES, TimeSlot

MAX_OFFERED_SLOTS = 3
_SLOT_DURATION = timedelta(minutes=DEFAULT_MEETING_MINUTES)
_EVENT_ID_MAX = 1024
_MEET_LINK_MAX = 512

_STATUS_OFFERED = "offered"

_EXACT_INDEX_RE = re.compile(r"^(?:slot\s+|option\s+|אפשרות\s+)?([123])$", re.IGNORECASE)
_HEBREW_ORDINAL = {
    "הראשון": 1,
    "השני": 2,
    "השלישי": 3,
}
_EVENT_ID_RE = re.compile(r"^[\x20-\x7E]+$")


class OfferedSlot(BaseModel):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def must_be_utc_aware(cls, value: datetime) -> datetime:
        normalized = to_utc_aware(value)
        if normalized is None:
            raise ValueError("datetime must be timezone-aware")
        return normalized


def to_utc_aware(value: datetime) -> datetime | None:
    """Reject naive datetimes; normalize aware values to UTC."""
    if value.tzinfo is None:
        return None
    return value.astimezone(UTC)


def normalize_scheduled_at_utc(value: str) -> str | None:
    """Parse ISO scheduled_at and return normalized UTC ISO string."""
    if not value or not isinstance(value, str):
        return None
    try:
        normalized = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None
    utc = to_utc_aware(parsed)
    if utc is None:
        return None
    return utc.isoformat()


def sanitize_event_id(value: str) -> str | None:
    """Printable nonempty event id, max 1024, no control characters."""
    if not value or not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _EVENT_ID_MAX:
        return None
    if not _EVENT_ID_RE.fullmatch(cleaned):
        return None
    if any(ord(ch) < 32 for ch in cleaned):
        return None
    return cleaned


def sanitize_meet_link(value: str) -> str:
    """Strict https meet.google.com link, max 512, no credentials/port/control chars."""
    if not value or not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _MEET_LINK_MAX:
        return ""
    if any(ord(ch) < 32 for ch in cleaned):
        return ""
    if "@" in cleaned:
        return ""
    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.netloc != "meet.google.com":
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port is not None:
        return ""
    if parsed.username or parsed.password:
        return ""
    path = parsed.path or ""
    if not path or path == "/":
        return ""
    if any(ord(ch) < 32 for ch in path):
        return ""
    return cleaned


def is_explicit_slot_selection(message: str) -> bool:
    """True when message matches confirmation grammar (without requiring stored slots)."""
    text = message.strip()
    if not text:
        return False
    if text in _HEBREW_ORDINAL:
        return True
    return _EXACT_INDEX_RE.fullmatch(text) is not None


def compute_booking_key(*, lead_id: str, start: datetime, end: datetime) -> str:
    """Deterministic private-property key from UTC-normalized instants."""
    start_utc = to_utc_aware(start)
    end_utc = to_utc_aware(end)
    if start_utc is None or end_utc is None:
        raise ValueError("booking key requires timezone-aware start/end")
    digest = hashlib.sha256(
        f"{lead_id}|{start_utc.isoformat()}|{end_utc.isoformat()}".encode()
    ).hexdigest()
    return f"mia_{digest}"


def parse_slot_selection(
    message: str,
    *,
    offered_slots: list[OfferedSlot],
    meeting_status: str,
) -> int | None:
    """Return 1-based index when message is explicit slot confirmation; else None."""
    if meeting_status != _STATUS_OFFERED or not offered_slots:
        return None
    text = message.strip()
    if not text:
        return None
    if text in _HEBREW_ORDINAL:
        index = _HEBREW_ORDINAL[text]
        return index if 1 <= index <= len(offered_slots) else None
    match = _EXACT_INDEX_RE.fullmatch(text)
    if match:
        index = int(match.group(1))
        return index if 1 <= index <= len(offered_slots) else None
    return None


def validate_offered_slots(
    slots: list[TimeSlot],
    *,
    now: datetime | None = None,
    timezone: str = "Asia/Jerusalem",
) -> list[OfferedSlot]:
    """Validate up to 3 policy-compliant future 30-minute UTC-aware slots."""
    clock = to_utc_aware(now or datetime.now(UTC))
    if clock is None:
        clock = datetime.now(UTC)
    candidates: list[OfferedSlot] = []
    for slot in slots:
        start = to_utc_aware(slot.start)
        end = to_utc_aware(slot.end)
        if start is None or end is None:
            continue
        if end - start != _SLOT_DURATION:
            continue
        if start >= end:
            continue
        candidates.append(OfferedSlot(start=start, end=end))
    clean: list[OfferedSlot] = []
    for slot in candidates:
        if slot_is_bookable(slot.start, slot.end, now=clock, timezone=timezone):
            clean.append(slot)
        if len(clean) >= MAX_POLICY_SLOTS:
            break
    return clean


def offered_slots_to_json(slots: list[OfferedSlot]) -> str:
    payload = [
        {
            "start": to_utc_aware(s.start).isoformat(),  # type: ignore[union-attr]
            "end": to_utc_aware(s.end).isoformat(),  # type: ignore[union-attr]
        }
        for s in slots[:MAX_OFFERED_SLOTS]
    ]
    return json.dumps(payload)


def offered_slots_from_json(raw: str) -> list[OfferedSlot]:
    if not raw or raw == "[]":
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    slots: list[OfferedSlot] = []
    for item in data[:MAX_OFFERED_SLOTS]:
        if not isinstance(item, dict):
            continue
        start_raw = item.get("start")
        end_raw = item.get("end")
        if not isinstance(start_raw, str) or not isinstance(end_raw, str):
            continue
        start = _parse_iso_utc(start_raw)
        end = _parse_iso_utc(end_raw)
        if start is None or end is None:
            continue
        if end - start != _SLOT_DURATION:
            continue
        slots.append(OfferedSlot(start=start, end=end))
    return slots


def slot_at_index(slots: list[OfferedSlot], index: int) -> OfferedSlot | None:
    if index < 1 or index > len(slots):
        return None
    return slots[index - 1]


def slot_interval_exactly_available(
    calendar_slots: list[TimeSlot],
    *,
    selected: OfferedSlot,
) -> bool:
    """True when a returned free slot fully covers the selected start/end."""
    sel_start = to_utc_aware(selected.start)
    sel_end = to_utc_aware(selected.end)
    if sel_start is None or sel_end is None:
        return False
    for slot in calendar_slots:
        free_start = to_utc_aware(slot.start)
        free_end = to_utc_aware(slot.end)
        if free_start is None or free_end is None:
            continue
        if free_start <= sel_start and free_end >= sel_end:
            return True
    return False


def _parse_iso_utc(value: str) -> datetime | None:
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None
    return to_utc_aware(parsed)
