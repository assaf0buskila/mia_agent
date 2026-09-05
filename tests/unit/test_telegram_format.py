"""Telegram rendering: HTML escaping, buttons, callbacks, dates, chunking."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from app.domain.owner.callbacks import approval_token, resolve_owner_callback
from app.integrations.telegram import ALLOWED_UPDATES, parse_telegram_callback
from app.integrations.telegram_format import (
    MAX_CALLBACK_BYTES,
    CallbackDataTooLong,
    approval_keyboard,
    blockquote,
    bold,
    bullets,
    callback_data,
    code,
    esc,
    hebrew_date,
    hebrew_datetime,
    isolate,
    join_sections,
    key_values,
    parse_callback_token,
    plain_text_length,
    relative_hebrew_day,
    section,
    split_message,
)

# ------------------------------------------------------------------- escaping


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a & b", "a &amp; b"),
        ("<script>", "&lt;script&gt;"),
        ("5 > 3", "5 &gt; 3"),
        ("plain", "plain"),
    ],
)
def test_escape_covers_exactly_the_three_required_characters(raw: str, expected: str) -> None:
    assert esc(raw) == expected


def test_escape_order_does_not_double_encode() -> None:
    """`&` must be replaced first or `<` becomes `&amp;lt;`."""
    assert esc("<&>") == "&lt;&amp;&gt;"


@pytest.mark.parametrize(
    "value",
    ["lead_ab12cd34", "a.b@example.co.il", "1,200.00", "(pending)", "R4-approval", "50%"],
)
def test_real_world_data_needs_no_escaping_in_html_mode(value: str) -> None:
    """Every one of these is a MarkdownV2 landmine and an HTML non-issue."""
    assert esc(value) == value


def test_hebrew_passes_through_untouched() -> None:
    assert esc("שלום, מה המצב?") == "שלום, מה המצב?"


def test_injected_markup_in_data_is_neutralized() -> None:
    rendered = key_values([("ליד", "<b>fake</b>")])
    assert "<b>fake</b>" not in rendered.replace(bold("ליד"), "")
    assert "&lt;b&gt;fake&lt;/b&gt;" in rendered


# -------------------------------------------------------------------- markup


def test_code_is_monospace_and_escaped() -> None:
    assert code("a<b") == "<code>a&lt;b</code>"


def test_expandable_blockquote_collapses_detail() -> None:
    assert blockquote("long", expandable=True).startswith("<blockquote expandable>")


def test_sections_and_bullets_render_cleanly() -> None:
    block = section("לידים חמים", bullets(["ליד אחד", "ליד שני"]), icon="🔥")
    assert "<b>לידים חמים</b>" in block
    assert "• ליד אחד" in block


def test_join_sections_drops_empties() -> None:
    assert join_sections("a", "", "   ", "b") == "a\n\nb"


def test_plain_text_length_ignores_tags() -> None:
    assert plain_text_length("<b>abc</b>") == 3


# ------------------------------------------------------------------- buttons


def test_callback_data_is_ascii_and_within_the_byte_limit() -> None:
    keyboard = approval_keyboard("apr_abc123")
    row = keyboard["inline_keyboard"][0]
    for button in row:
        assert len(button["callback_data"].encode("utf-8")) <= MAX_CALLBACK_BYTES
        assert button["callback_data"].isascii()
    # Hebrew belongs in the visible label, never in the payload.
    assert "אישור" in row[0]["text"]


def test_buttons_carry_native_styles() -> None:
    row = approval_keyboard("apr_1")["inline_keyboard"][0]
    assert row[0]["style"] == "success"
    assert row[1]["style"] == "danger"


def test_hebrew_callback_data_is_refused_because_of_the_byte_limit() -> None:
    """Hebrew is 2 bytes/char in UTF-8, so a Hebrew payload silently overflows 64 bytes."""
    with pytest.raises(CallbackDataTooLong):
        callback_data("אישור" * 20)


def test_callback_token_round_trip() -> None:
    assert parse_callback_token("ok:apr_1") == ("approve", "apr_1")
    assert parse_callback_token("no:apr_1") == ("reject", "apr_1")
    assert parse_callback_token("garbage") == ("", "")
    assert parse_callback_token("") == ("", "")


def test_allowed_updates_includes_callback_query() -> None:
    """Omitting this silently drops every button press, with nothing in the logs."""
    assert "callback_query" in ALLOWED_UPDATES
    assert "message" in ALLOWED_UPDATES


def test_parse_callback_reads_from_by_key_not_attribute() -> None:
    parsed = parse_telegram_callback(
        {
            "update_id": 7,
            "callback_query": {
                "id": "q1",
                "from": {"id": 12345},
                "data": "ok:apr_9",
                "message": {"message_id": 55, "chat": {"id": 999}},
            },
        }
    )
    assert parsed == {
        "id": "7",
        "callback_query_id": "q1",
        "from": "12345",
        "chat_id": "999",
        "message_id": "55",
        "data": "ok:apr_9",
    }


def test_parse_callback_ignores_a_plain_message_update() -> None:
    assert parse_telegram_callback({"update_id": 1, "message": {"text": "hi"}}) is None


def test_unknown_approval_token_is_refused() -> None:
    from app.db.session import get_session_factory, init_db
    from app.db.store import LeadStore

    init_db()
    store = LeadStore(get_session_factory()())
    result = resolve_owner_callback(store, decision="approve", token="apr_does_not_exist")
    assert "לא מצאתי" in result


def test_approval_token_fits_the_callback_budget() -> None:
    token = approval_token("apr_" + "x" * 100)
    assert len(f"ok:{token}".encode()) <= MAX_CALLBACK_BYTES


# --------------------------------------------------------------------- dates


def test_hebrew_date_reads_like_a_person_wrote_it() -> None:
    assert hebrew_date(date(2026, 8, 23)) == "23 באוגוסט 2026"


def test_hebrew_datetime_leads_with_the_weekday() -> None:
    rendered = hebrew_datetime(datetime(2026, 8, 23, 14, 30, tzinfo=UTC), timezone="UTC")
    assert rendered.startswith("יום ראשון")
    assert "23 באוגוסט" in rendered
    assert "14:30" in rendered


def test_relative_days_say_today_and_tomorrow() -> None:
    today = date(2026, 8, 23)
    assert relative_hebrew_day(date(2026, 8, 23), today=today) == "היום"
    assert relative_hebrew_day(date(2026, 8, 24), today=today) == "מחר"
    assert relative_hebrew_day(date(2026, 8, 22), today=today) == "אתמול"
    assert relative_hebrew_day(date(2026, 9, 1), today=today) == "1 בספטמבר 2026"


def test_ltr_runs_are_bidi_isolated() -> None:
    """A Hebrew line ending in a Latin token reorders visibly without isolation.

    FIRST STRONG ISOLATE (U+2068) rather than LRI (U+2066): it takes direction from the
    first strong character, so it is correct for both `14:30` and `AssafWeb` without the
    caller having to know which it is wrapping.
    """
    wrapped = isolate("14:30")
    assert wrapped.startswith("⁨")
    assert wrapped.endswith("⁩")
    assert "14:30" in wrapped


def test_isolate_ignores_an_empty_value() -> None:
    assert isolate("") == ""


# ------------------------------------------------------------------ chunking


def test_short_message_is_not_split() -> None:
    assert split_message("hello") == ["hello"]


def test_long_message_splits_under_the_limit() -> None:
    body = "\n\n".join(f"paragraph number {index} " + "x" * 200 for index in range(40))
    chunks = split_message(body)
    assert len(chunks) > 1
    assert all(len(chunk) <= 4096 for chunk in chunks)
    # Nothing is lost in the split.
    assert "paragraph number 39" in chunks[-1]


def test_split_prefers_paragraph_boundaries() -> None:
    body = ("a" * 1000 + "\n\n") * 6
    for chunk in split_message(body):
        assert not chunk.startswith("\n")


def test_empty_message_yields_no_chunks() -> None:
    assert split_message("   ") == []
