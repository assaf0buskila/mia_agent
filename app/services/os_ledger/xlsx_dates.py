"""Typed date cells in the owner xlsx import (contract section 22, erratum E27).

A numeric cell whose number format is a date or datetime format, and a ``t="d"`` ISO cell, are
dates. The field decides what they mean:

* A date-only field (``date`` in the physical descriptor: a payment, time or expense ``date``, a
  work ``start_date``, a task ``due_date``) takes the calendar date with no time zone conversion and
  maps to ``YYYY-MM-DD``. A time of day on it is refused.
* A datetime field (``datetime`` in the descriptor: ``created_at`` and ``updated_at``; and the
  accepted journal's ``accepted_at``) holds wall-clock time in ``Asia/Jerusalem``, the business time
  zone. It maps to the UTC instant of that wall-clock time, in the form the Apps Script uses for the
  same value (``Date.toISOString()``: ``YYYY-MM-DDTHH:MM:SS.mmmZ``), so a raw fingerprint taken over
  the string and one taken over the typed cell agree. An ambiguous (fall-back) or nonexistent
  (spring-forward) local time is refused.

Serials use the 1900 system unless the workbook sets ``date1904``. Excel's fictional 1900-02-29
(serial 60) is refused, and so is any serial below 1 or above 2958465. Every refusal is
``DateValueError``; the message is a fixed reason code and never carries the cell value.
"""

from __future__ import annotations

import decimal
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

BUSINESS_TIMEZONE = ZoneInfo("Asia/Jerusalem")  # ASSAF_OS_BUSINESS_TIMEZONE in UnifiedRuntime.gs
DATE_ONLY = "date"
DATETIME = "datetime"
MIN_SERIAL = 1
MAX_SERIAL = 2_958_465  # 9999-12-31 in the 1900 system
REASON = "unsafe_date_value"

_MAX_TEXT = 64  # a serial is at most ~25 characters; anything longer is not a date
_MS_PER_DAY = 86_400_000
_FICTIONAL_LEAP_DAY = 60  # Excel's 1900-02-29
# ASCII digits only: ``\d`` and ``Decimal`` both accept other scripts' digits and underscores.
_SERIAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?")
_ISO = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})"
    r"(?:T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?)?"
)


class DateValueError(ValueError):
    """The date cell is refused. The message is always ``unsafe_date_value``."""

    def __init__(self) -> None:
        super().__init__(REASON)


def _refuse() -> DateValueError:
    return DateValueError()


def _check_kind(kind: str) -> None:
    if kind not in {DATE_ONLY, DATETIME}:
        raise ValueError("unsupported date field kind")


def _instant(wall_clock: datetime) -> str:
    """The UTC instant of a naive Asia/Jerusalem wall-clock time, refusing a gap or a repeat."""
    first = wall_clock.replace(tzinfo=BUSINESS_TIMEZONE, fold=0)
    second = wall_clock.replace(tzinfo=BUSINESS_TIMEZONE, fold=1)
    # Two offsets for one wall-clock time: it occurs twice (fall back) or not at all (spring
    # forward).
    if first.utcoffset() != second.utcoffset():
        raise _refuse()
    utc = first.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def from_serial(text: str, kind: str, *, date_1904: bool = False) -> str:
    """Map a date-formatted number (decimal text, as the reader returns it) to its string."""
    _check_kind(kind)
    if not isinstance(text, str) or len(text) > _MAX_TEXT or _SERIAL.fullmatch(text) is None:
        raise _refuse()
    try:
        serial = Decimal(text)
        if not MIN_SERIAL <= serial <= MAX_SERIAL:  # exact comparisons: no context rounding
            raise _refuse()
        days = int(serial.to_integral_value(rounding=ROUND_FLOOR))
        if date_1904:
            base = date(1904, 1, 1)
        elif days == _FICTIONAL_LEAP_DAY:
            raise _refuse()
        else:
            # 1900 is not a leap year, but Excel counts a 29 February in it: serials from 61 on
            # are offset by one more day than serials 1..59.
            base = date(1899, 12, 30) if days > _FICTIONAL_LEAP_DAY else date(1899, 12, 31)
        day = base + timedelta(days=days)
        if kind == DATE_ONLY:
            if serial != days:  # any time of day on a date-only field
                raise _refuse()
            return day.isoformat()
        milliseconds = int(((serial - days) * _MS_PER_DAY).quantize(
            Decimal(1), rounding=ROUND_HALF_UP))
        return _instant(datetime.combine(day, time.min) + timedelta(milliseconds=milliseconds))
    except DateValueError:
        raise
    except (decimal.DecimalException, OverflowError, ValueError):
        raise _refuse() from None


def from_iso(text: str, kind: str) -> str:
    """Map a ``t="d"`` ISO cell (``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM:SS[.fff]``, no zone)."""
    _check_kind(kind)
    match = _ISO.fullmatch(text) if isinstance(text, str) and len(text) <= _MAX_TEXT else None
    if match is None:
        raise _refuse()
    year, month, day, hour, minute, second = (int(part or 0) for part in match.groups()[:6])
    fraction = match[7] or ""
    try:
        if year < 1900:
            raise _refuse()
        calendar_day = date(year, month, day)
        clock = time(hour, minute, second)
        milliseconds = int((Decimal("0." + fraction) * 1000).quantize(
            Decimal(1), rounding=ROUND_HALF_UP)) if fraction else 0
        if kind == DATE_ONLY:
            if clock != time.min or milliseconds:
                raise _refuse()
            return calendar_day.isoformat()
        return _instant(datetime.combine(calendar_day, clock) + timedelta(
            milliseconds=milliseconds))
    except DateValueError:
        raise
    except (decimal.DecimalException, OverflowError, ValueError):
        raise _refuse() from None
