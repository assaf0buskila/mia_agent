"""Gmail row/body timestamp rendering: absolute date, relative tag, timezone.

`format_inbox_rows` / `format_email_body` gained keyword-only `timezone` and `now`
so the owner can be told *when* a message arrived, not just who it is from. Composio's
timestamp shape is undocumented (raw epoch millis or an ISO-8601 string have both been
observed), so parsing must be best-effort and never raise.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.integrations.gmail import InboundEmail, InboxRow, format_email_body, format_inbox_rows

# Frozen reference clock: Monday 2026-08-24 12:00 UTC.
NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)


def _row(timestamp: str) -> InboxRow:
    return InboxRow(
        message_id="m1",
        sender="lead@example.com",
        subject="Proposal follow-up",
        snippet="just checking in",
        timestamp=timestamp,
    )


# ------------------------------------------------------------------------- rows


def test_row_renders_absolute_date_and_today_tag() -> None:
    text = format_inbox_rows([_row("2026-08-24T09:15:00+00:00")], timezone="UTC", now=NOW)
    assert "2026-08-24 09:15 (today)" in text


def test_row_renders_yesterday_tag() -> None:
    text = format_inbox_rows([_row("2026-08-23T09:15:00+00:00")], timezone="UTC", now=NOW)
    assert "2026-08-23 09:15 (yesterday)" in text


def test_row_renders_n_days_ago_tag() -> None:
    text = format_inbox_rows([_row("2026-08-19T09:15:00+00:00")], timezone="UTC", now=NOW)
    assert "2026-08-19 09:15 (5d ago)" in text


def test_relative_tag_is_dropped_past_30_days_but_the_date_stays() -> None:
    text = format_inbox_rows([_row("2026-07-24T09:15:00+00:00")], timezone="UTC", now=NOW)
    assert "2026-07-24 09:15" in text
    # No relative-age parenthetical for anything older than 30 days.
    assert "2026-07-24 09:15 (" not in text


def test_boundary_at_exactly_30_days_still_carries_the_tag() -> None:
    text = format_inbox_rows([_row("2026-07-25T09:15:00+00:00")], timezone="UTC", now=NOW)
    assert "(30d ago)" in text


def test_missing_timestamp_drops_only_the_date_prefix_and_never_raises() -> None:
    text = format_inbox_rows([_row("")], timezone="UTC", now=NOW)
    assert "1. lead@example.com · Proposal follow-up" in text
    assert "lead@example.com" in text


def test_garbage_timestamp_drops_only_the_date_prefix_and_never_raises() -> None:
    text = format_inbox_rows([_row("not-a-real-timestamp")], timezone="UTC", now=NOW)
    assert "1. lead@example.com · Proposal follow-up" in text


def test_epoch_seconds_string_parses_correctly() -> None:
    # 2026-08-24T09:15:00Z as whole epoch seconds (10 digits, well under the millis cutoff).
    text = format_inbox_rows([_row("1787562900")], timezone="UTC", now=NOW)
    assert "2026-08-24 09:15 (today)" in text


def test_epoch_millis_string_parses_correctly() -> None:
    # Same instant, but as Gmail's internalDate-shaped milliseconds.
    text = format_inbox_rows([_row("1787562900000")], timezone="UTC", now=NOW)
    assert "2026-08-24 09:15 (today)" in text


def test_timezone_genuinely_shifts_the_rendered_local_time() -> None:
    utc_text = format_inbox_rows([_row("2026-08-24T09:15:00+00:00")], timezone="UTC", now=NOW)
    il_text = format_inbox_rows(
        [_row("2026-08-24T09:15:00+00:00")], timezone="Asia/Jerusalem", now=NOW
    )
    assert "09:15" in utc_text
    # Israel is UTC+3 in August (IDT).
    assert "12:15" in il_text
    assert utc_text != il_text


def test_multiple_rows_each_get_their_own_date_prefix() -> None:
    rows = [
        _row("2026-08-24T09:15:00+00:00"),
        InboxRow(
            message_id="m2",
            sender="other@example.com",
            subject="Old thread",
            snippet="",
            timestamp="2026-08-23T08:00:00+00:00",
        ),
    ]
    text = format_inbox_rows(rows, timezone="UTC", now=NOW)
    assert "(today)" in text
    assert "(yesterday)" in text


def test_default_now_and_timezone_do_not_raise() -> None:
    # No now/timezone passed: must fall back to real UTC "now" without crashing.
    text = format_inbox_rows([_row("2026-08-24T09:15:00+00:00")])
    assert "lead@example.com" in text


# ------------------------------------------------------------------------- bodies


def _email(timestamp: str) -> InboundEmail:
    return InboundEmail(
        message_id="msg_1",
        sender="lead@example.com",
        subject="Proposal follow-up",
        text="Looking forward to it.",
        thread_id="t1",
        timestamp=timestamp,
    )


def test_email_body_carries_a_date_line() -> None:
    text = format_email_body(_email("2026-08-24T09:15:00+00:00"), timezone="UTC", now=NOW)
    lines = text.splitlines()
    date_lines = [line for line in lines if line.startswith("date:")]
    assert len(date_lines) == 1
    assert "2026-08-24 09:15 (today)" in date_lines[0]


def test_email_body_omits_the_date_line_when_timestamp_is_missing() -> None:
    text = format_email_body(_email(""), timezone="UTC", now=NOW)
    assert not any(line.startswith("date:") for line in text.splitlines())
    assert "from: lead@example.com" in text


def test_email_body_omits_the_date_line_for_a_garbage_timestamp_without_raising() -> None:
    text = format_email_body(_email("not-a-real-timestamp"), timezone="UTC", now=NOW)
    assert not any(line.startswith("date:") for line in text.splitlines())


def test_email_body_timezone_shifts_the_date_line() -> None:
    utc_text = format_email_body(_email("2026-08-24T09:15:00+00:00"), timezone="UTC", now=NOW)
    il_text = format_email_body(
        _email("2026-08-24T09:15:00+00:00"), timezone="Asia/Jerusalem", now=NOW
    )
    assert "09:15" in utc_text
    assert "12:15" in il_text
