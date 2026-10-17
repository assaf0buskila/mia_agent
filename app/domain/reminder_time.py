"""Strict, deterministic owner-reminder time parsing."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Jerusalem"
_HEBREW = "\u0590-\u05ff"
_WEEKDAY = re.compile(
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
    rf"(?<![{_HEBREW}])ב?יום\s+(?:ראשון|שני|שלישי|רביעי|חמישי|שישי|שבת)(?![{_HEBREW}])",
    re.IGNORECASE,
)
_COMPOUND_DATE = re.compile(
    r"\b(?:before|after|from)\s+(?:today|tomorrow|next\s+week)\b|"
    rf"(?<![{_HEBREW}])(?:לפני|אחרי|לאחר)\s+"
    rf"(?:היום|מחר|בשבוע\s+הבא)(?![{_HEBREW}])",
    re.IGNORECASE,
)


class ReminderTimeError(ValueError):
    """A safe parser failure with a stable machine-readable reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _now_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _relative_matches(text: str) -> list[timedelta]:
    matches: list[tuple[int, timedelta]] = []
    patterns = (
        (r"\b(?:in|for)\s+(\d+)\s+(minutes?|mins?)\b", "minutes"),
        (r"\b(?:in|for)\s+(\d+)\s+(hours?|hrs?)\b", "hours"),
        (rf"(?<![{_HEBREW}])בעוד\s+(\d+)\s+דקות(?![{_HEBREW}])", "minutes"),
        (rf"(?<![{_HEBREW}])בעוד\s+(\d+)\s+שעות(?![{_HEBREW}])", "hours"),
    )
    for pattern, unit in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = int(match.group(1))
            delta = timedelta(**{unit: value})
            matches.append((match.start(), delta))
    return [delta for _, delta in sorted(matches)]


def _date_matches(text: str, *, local_today: date) -> tuple[list[date], list[tuple[int, int]]]:
    found: list[tuple[int, int, date]] = []
    explicit_patterns = (
        r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)",
        r"(?<!\d)(\d{2})/(\d{2})/(\d{4})(?!\d)",
    )
    for index, pattern in enumerate(explicit_patterns):
        for match in re.finditer(pattern, text):
            try:
                if index == 0:
                    value = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                else:
                    value = date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
            except ValueError as exc:
                raise ReminderTimeError("missing_date") from exc
            found.append((match.start(), match.end(), value))

    relative_dates = (
        (r"\btoday\b", 0),
        (r"\btomorrow\b", 1),
        (r"\bnext\s+week\b", 7),
        (rf"(?<![{_HEBREW}])היום(?![{_HEBREW}])", 0),
        (rf"(?<![{_HEBREW}])מחר(?![{_HEBREW}])", 1),
        (rf"(?<![{_HEBREW}])בשבוע\s+הבא(?![{_HEBREW}])", 7),
    )
    for pattern, offset in relative_dates:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            found.append((match.start(), match.end(), local_today + timedelta(days=offset)))
    found.sort()
    return [item[2] for item in found], [(item[0], item[1]) for item in found]


def _without_spans(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)


def _time_matches(text: str) -> tuple[list[tuple[time, timezone | None]], bool]:
    matches: list[tuple[int, int, time, timezone | None]] = []
    occupied: list[tuple[int, int]] = []

    twelve_hour = re.compile(
        r"(?<!\d)(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])",
        re.IGNORECASE,
    )
    for match in twelve_hour.finditer(text):
        hour = int(match.group(1))
        minute = int(match.group(2) or "0")
        if not 1 <= hour <= 12 or minute > 59:
            raise ReminderTimeError("invalid_time")
        if match.group(3).lower().startswith("p") and hour != 12:
            hour += 12
        if match.group(3).lower().startswith("a") and hour == 12:
            hour = 0
        matches.append((match.start(), match.end(), time(hour, minute), None))
        occupied.append((match.start(), match.end()))

    clock = re.compile(
        r"(?<![\d:])(?:at\s+|ב-?\s*)?"
        r"(\d{1,2}):(\d{2})(?:\s*(Z|[+-]\d{2}:\d{2}))?(?![\d:])",
        re.IGNORECASE,
    )
    for match in clock.finditer(text):
        if any(match.start() < end and match.end() > start for start, end in occupied):
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ReminderTimeError("invalid_time")
        offset_text = match.group(3)
        offset = None
        if offset_text:
            if offset_text.upper() == "Z":
                offset = UTC
            else:
                sign = 1 if offset_text[0] == "+" else -1
                offset_hours, offset_minutes = map(int, offset_text[1:].split(":"))
                if offset_hours > 23 or offset_minutes > 59:
                    raise ReminderTimeError("invalid_time")
                offset = timezone(sign * timedelta(hours=offset_hours, minutes=offset_minutes))
        matches.append((match.start(), match.end(), time(hour, minute), offset))

    matches.sort()
    ambiguous_bare = bool(
        re.search(r"\bat\s+\d{1,2}(?!\s*[:\d]|\s*(?:a\.?m\.?|p\.?m\.?))", text, re.I)
        or re.search(rf"(?<![{_HEBREW}])ב-?\s*\d{{1,2}}(?!\s*[:\d])", text)
    )
    return [(item[2], item[3]) for item in matches], ambiguous_bare


def _local_to_utc(value: datetime, zone: ZoneInfo, offset: timezone | None) -> datetime:
    if offset is not None:
        explicit = value.replace(tzinfo=offset)
        local = explicit.astimezone(zone)
        if local.replace(tzinfo=None) != value:
            raise ReminderTimeError("invalid_time")
        return explicit.astimezone(UTC)

    valid: list[datetime] = []
    for fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=fold)
        roundtrip = candidate.astimezone(UTC).astimezone(zone)
        if roundtrip.replace(tzinfo=None) == value and roundtrip.fold == fold:
            valid.append(candidate)
    unique = {candidate.utcoffset() for candidate in valid}
    if not valid:
        raise ReminderTimeError("dst_gap")
    if len(unique) > 1:
        raise ReminderTimeError("dst_fold")
    return valid[0].astimezone(UTC)


def parse_reminder_time(
    text: str,
    *,
    now: datetime,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> datetime:
    """Parse one exact future instant and return an aware UTC datetime."""
    now_utc = _now_utc(now)
    if _WEEKDAY.search(text) or _COMPOUND_DATE.search(text):
        # Weekday arithmetic is intentionally unsupported. Ignoring a weekday next to
        # "next week" would silently schedule the wrong day, so require clarification.
        raise ReminderTimeError("ambiguous_time")
    relative = _relative_matches(text)
    if relative:
        if len(relative) != 1:
            raise ReminderTimeError("ambiguous_time")
        unit_mentions = re.findall(
            rf"(?<!\d)\d+\s*(?:minutes?|mins?|hours?|hrs?|דקות|שעות)(?![{_HEBREW}a-z])",
            text,
            re.IGNORECASE,
        )
        if len(unit_mentions) != 1:
            raise ReminderTimeError("ambiguous_time")
        if re.search(
            rf"\b(?:today|tomorrow)\b|\bnext\s+week\b|"
            rf"(?<![{_HEBREW}])(?:היום|מחר|בשבוע\s+הבא)(?![{_HEBREW}])|"
            r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})(?!\d)|"
            r"(?<!\d)\d{1,2}:\d{2}(?!\d)|"
            r"(?<!\d)\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|"
            r"\bat\s+\d{1,2}(?!\s*[:\d])|"
            rf"(?<![{_HEBREW}])ב-?\s*\d{{1,2}}(?!\s*[:\d])",
            text,
            re.IGNORECASE,
        ):
            raise ReminderTimeError("ambiguous_time")
        result = (now_utc + relative[0]).replace(second=0, microsecond=0)
        if result <= now_utc:
            raise ReminderTimeError("past")
        return result

    zone = ZoneInfo(timezone_name)
    local_today = now_utc.astimezone(zone).date()
    dates, spans = _date_matches(text, local_today=local_today)
    if not dates:
        raise ReminderTimeError("missing_date")
    if len(dates) != 1:
        raise ReminderTimeError("ambiguous_time")

    times, ambiguous_bare = _time_matches(_without_spans(text, spans))
    if ambiguous_bare:
        raise ReminderTimeError("ambiguous_time")
    if not times:
        if re.search(r"(?<!\d)\d{1,2}:\d{2}(?!\d)", _without_spans(text, spans)):
            raise ReminderTimeError("invalid_time")
        raise ReminderTimeError("missing_time")
    if len(times) != 1:
        raise ReminderTimeError("ambiguous_time")

    clock, offset = times[0]
    result = _local_to_utc(datetime.combine(dates[0], clock), zone, offset)
    if result <= now_utc:
        raise ReminderTimeError("past")
    return result
