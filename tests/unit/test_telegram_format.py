"""Telegram rendering: HTML escaping, buttons, callbacks, dates, chunking."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

import pytest
from app.domain.owner.callbacks import approval_token, resolve_owner_callback
from app.integrations.telegram import ALLOWED_UPDATES, parse_telegram_callback
from app.integrations.telegram_format import (
    MAX_CALLBACK_BYTES,
    MAX_MESSAGE_CHARS,
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
    render_owner_markdown,
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


# --------------------------------------------------------- render_owner_markdown


def test_render_owner_markdown_bold_mixes_hebrew_and_english() -> None:
    assert (
        render_owner_markdown("רוצה **לתאם פגישה** ב-Zoom מחר")
        == "רוצה <b>לתאם פגישה</b> ב-Zoom מחר"
    )


def test_render_owner_markdown_escapes_ampersand_and_angle_brackets() -> None:
    assert render_owner_markdown("A & B < C > D") == "A &amp; B &lt; C &gt; D"


def test_render_owner_markdown_never_passes_through_model_html() -> None:
    """A literal <b>hi</b> written or echoed by the model must stay inert text."""
    rendered = render_owner_markdown("<b>hi</b> and **actually bold**")
    assert rendered == "&lt;b&gt;hi&lt;/b&gt; and <b>actually bold</b>"


def test_render_owner_markdown_heading_and_bullets() -> None:
    rendered = render_owner_markdown("### עדכונים\n- דבר אחד\n- דבר שני\n* דבר שלישי")
    assert rendered == "<b>עדכונים</b>\n• דבר אחד\n• דבר שני\n• דבר שלישי"


def test_render_owner_markdown_inline_code() -> None:
    assert (
        render_owner_markdown("תריץ `git status` ותגיד לי")
        == "תריץ <code>git status</code> ותגיד לי"
    )


def test_render_owner_markdown_fenced_code_block_is_kept_literal() -> None:
    raw = "לפני:\n```\nprint('<b>x</b>')\n**not bold**\n```\nאחרי"
    rendered = render_owner_markdown(raw)
    assert rendered == (
        "לפני:\n<pre>print('&lt;b&gt;x&lt;/b&gt;')\n**not bold**</pre>\nאחרי"
    )


def test_render_owner_markdown_unbalanced_bold_stays_literal() -> None:
    assert render_owner_markdown("זה **לא נסגר תקין") == "זה **לא נסגר תקין"


def test_render_owner_markdown_two_pairs_on_one_line_both_convert() -> None:
    assert (
        render_owner_markdown("**א** וגם **ב**")
        == "<b>א</b> וגם <b>ב</b>"
    )


def test_render_owner_markdown_existing_escape_test_still_matches() -> None:
    """The pre-existing contract: plain text with no markdown is just escaped."""
    assert render_owner_markdown("a & b < c") == "a &amp; b &lt; c"


# ---------------------------------------------- split_message + <pre> safety


def test_split_message_keeps_a_long_fenced_block_whole() -> None:
    lead_in = "\n\n".join(f"פסקה {index} " + "x" * 150 for index in range(15))
    code_block = "<pre>" + "\n".join(f"line {i}" for i in range(80)) + "</pre>"
    tail = "\n\n".join(f"סיכום {index} " + "y" * 150 for index in range(15))
    body = f"{lead_in}\n\n{code_block}\n\n{tail}"
    assert len(body) > MAX_MESSAGE_CHARS
    chunks = split_message(body)
    assert len(chunks) > 1
    pre_open = pre_close = 0
    for chunk in chunks:
        pre_open += chunk.count("<pre>")
        pre_close += chunk.count("</pre>")
        # Every chunk is independently valid: an equal, matched count of open/close.
        assert chunk.count("<pre>") == chunk.count("</pre>")
        assert not chunk.startswith("</pre>")
        assert not chunk.endswith("<pre>")
    assert pre_open == pre_close == 1
    assert code_block in "".join(chunks)


def test_render_then_split_produces_balanced_html_per_chunk() -> None:
    paragraphs = [f"**כותרת {i}**\nשורה עם `code {i}` ועוד טקסט " + "מ" * 120 for i in range(60)]
    raw = "\n\n".join(paragraphs)
    rendered = render_owner_markdown(raw)
    chunks = split_message(rendered)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("<b>") == chunk.count("</b>")
        assert chunk.count("<code>") == chunk.count("</code>")
        assert chunk.count("<pre>") == chunk.count("</pre>")


def test_split_message_splits_a_single_oversized_fence_with_reopen() -> None:
    """A fence bigger than the limit alone must never ship as one oversized chunk."""
    code_block = "<pre>" + "\n".join(f"line {i:04d} " + "z" * 40 for i in range(200)) + "</pre>"
    assert len(code_block) > MAX_MESSAGE_CHARS
    chunks = split_message(code_block)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_CHARS
        assert chunk.count("<pre>") == chunk.count("</pre>")
        assert not chunk.startswith("</pre>")
        assert not chunk.endswith("<pre>")
    # The close/reopen seam is the only thing inserted; nothing else was lost or added.
    rejoined = "".join(chunks).replace("</pre><pre>", "")
    assert rejoined == code_block


def test_split_message_splits_two_oversized_fences_independently() -> None:
    fence_1 = "<pre>" + "\n".join(f"a-line {i:04d} " + "p" * 40 for i in range(150)) + "</pre>"
    fence_2 = "<pre>" + "\n".join(f"b-line {i:04d} " + "q" * 40 for i in range(150)) + "</pre>"
    body = f"{fence_1}\n\nmiddle text\n\n{fence_2}"
    assert len(body) > MAX_MESSAGE_CHARS * 2
    chunks = split_message(body)
    assert len(chunks) > 2
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_CHARS
        assert chunk.count("<pre>") == chunk.count("</pre>")
        assert not chunk.startswith("</pre>")
        assert not chunk.endswith("<pre>")
    assert "middle text" in "".join(chunks)
    assert "a-line 0000" in "".join(chunks)
    assert "b-line 0149" in "".join(chunks)


# ------------------------------------- bold/code must never interleave (P1-b)


def test_render_owner_markdown_bold_then_code_stays_balanced() -> None:
    rendered = render_owner_markdown("**a`b**c`")
    assert rendered == "**a<code>b**c</code>"
    assert rendered.count("<code>") == rendered.count("</code>")
    assert "<b>" not in rendered
    assert "<b><code>" not in rendered
    assert "<code></b>" not in rendered


def test_render_owner_markdown_code_then_bold_stays_balanced() -> None:
    rendered = render_owner_markdown("**`a**`")
    assert rendered == "**<code>a**</code>"
    assert rendered.count("<code>") == rendered.count("</code>")
    assert "<b>" not in rendered
    assert "<b><code>" not in rendered
    assert "<code></b>" not in rendered


# ------------------------------------------- hard-cut tag/entity safety (P2)


def test_split_message_hard_cut_avoids_breaking_tags_and_entities() -> None:
    """400x 'word **bold** ' on one line forces the raw hard-cut fallback."""
    rendered = render_owner_markdown("word **bold** " * 400)
    chunks = split_message(rendered)
    assert len(chunks) > 1
    entity_re = re.compile(r"&[a-zA-Z0-9#]+;")
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_CHARS
        assert chunk.count("<b>") == chunk.count("</b>")
        assert chunk.count("<") == chunk.count(">")
        # No stray "&" left over from a bisected entity.
        assert "&" not in entity_re.sub("", chunk)


def test_split_message_hard_cut_avoids_breaking_an_entity() -> None:
    """The naive `cut = limit` fallback is constructed to land INSIDE `&amp;`.

    `_CHUNK_BUDGET` is 3900. Placing the raw "&" at index 3898 puts the escaped
    "&amp;" at chars [3898, 3903) - so index 3900 (the "m") sits strictly inside it.
    Without `_safe_hard_cut` this test fails: `remaining[:3900]` ends in a bare "&a"
    with no closing ";" (so `entity_re` can't match and strip it), proving the fix is
    load-bearing rather than accidentally satisfied by an aligned repeating pattern.
    """
    raw = "x" * 3898 + "&" + "x" * 200
    rendered = render_owner_markdown(raw)
    assert rendered[3898:3903] == "&amp;"
    chunks = split_message(rendered)
    assert len(chunks) > 1
    entity_re = re.compile(r"&[a-zA-Z0-9#]+;")
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_CHARS
        assert "&" not in entity_re.sub("", chunk)
    assert "".join(chunks) == rendered


# ---------------------------------- hard-cut must never split <b>/<code> (P2)


def test_split_message_hard_cut_never_splits_a_bold_span() -> None:
    rendered = render_owner_markdown("x" * 3000 + " **" + "word " * 300 + "end**")
    chunks = split_message(rendered)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_CHARS
        assert chunk.count("<b>") == chunk.count("</b>")
        assert not chunk.startswith("</b>")
        assert not chunk.endswith("<b>")
    joined = "".join(chunks)
    assert "<b>" in joined and "end</b>" in joined
    assert "x" * 3000 in joined


def test_split_message_hard_cut_never_splits_a_bold_span_hebrew() -> None:
    rendered = render_owner_markdown("שלום " * 700 + "**" + "מודגש bold " * 200 + "סוף**")
    chunks = split_message(rendered)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_CHARS
        assert chunk.count("<b>") == chunk.count("</b>")
        assert not chunk.startswith("</b>")
        assert not chunk.endswith("<b>")
    joined = "".join(chunks)
    assert "<b>" in joined and "סוף</b>" in joined


def test_split_message_hard_cut_never_splits_a_code_span() -> None:
    rendered = render_owner_markdown("a " * 1900 + "`" + "c " * 300 + "`")
    chunks = split_message(rendered)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_CHARS
        assert chunk.count("<code>") == chunk.count("</code>")
        assert not chunk.startswith("</code>")
        assert not chunk.endswith("<code>")
    joined = "".join(chunks)
    assert "<code>" in joined and "</code>" in joined


def test_split_message_reopens_a_bold_span_bigger_than_the_limit() -> None:
    """Like an oversized <pre>, an oversized <b>/<code> is closed and reopened."""
    rendered = "<b>" + ("word " * 900) + "</b>"
    assert len(rendered) > MAX_MESSAGE_CHARS
    chunks = split_message(rendered)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_CHARS
        assert chunk.count("<b>") == chunk.count("</b>")
        assert not chunk.startswith("</b>")
        assert not chunk.endswith("<b>")
    rejoined = "".join(chunks).replace("</b><b>", "")
    assert rejoined == rendered


def test_split_message_avoids_an_empty_pre_pair_when_the_close_lands_at_the_limit() -> None:
    """A span oversized by only a few chars, whose content ends exactly at `limit`.

    Without folding this into the current chunk, the naive close/reopen produces a
    second, useless "<pre></pre>" chunk instead of one clean chunk.
    """
    from app.integrations.telegram_format import _CHUNK_BUDGET

    content_len = _CHUNK_BUDGET - len("<pre>")
    rendered = "<pre>" + "c" * content_len + "</pre>"
    assert len(rendered) > _CHUNK_BUDGET
    chunks = split_message(rendered)
    assert chunks == [rendered]
    assert "<pre></pre>" not in chunks


# --------------------------------------------------- fence language tag (P3)


def test_render_owner_markdown_fence_first_code_line_is_not_dropped_as_a_language() -> None:
    """A first line is a language tag only when it is nothing else - "echo hi" is code."""
    rendered = render_owner_markdown("```echo hi\necho bye```")
    assert rendered == "<pre>echo hi\necho bye</pre>"


def test_render_owner_markdown_fence_language_tag_is_still_dropped() -> None:
    rendered = render_owner_markdown("```python\nprint(1)\n```")
    assert rendered == "<pre>print(1)</pre>"


def test_render_owner_markdown_heading_does_not_double_bold() -> None:
    assert render_owner_markdown("# **h**") == "<b>h</b>"
    assert render_owner_markdown("## **שלום** עולם") == "<b>שלום עולם</b>"


def test_render_owner_markdown_fence_language_tag_accepts_crlf() -> None:
    rendered = render_owner_markdown("```python\r\nprint(1)\r\n```")
    assert rendered == "<pre>print(1)</pre>"


# --------------------------------------------- bold must never wrap a fence (P3)


def test_render_owner_markdown_bold_around_a_fence_stays_literal() -> None:
    """Telegram disallows <pre> nested inside another entity (e.g. <b>).

    "**```x```**" must never become "<b><pre>x</pre></b>" - the "**" markers around a
    fence placeholder are left as literal asterisks instead of being converted, since
    converting them would force exactly that illegal nesting.
    """
    rendered = render_owner_markdown("**```x```**")
    assert rendered == "**<pre>x</pre>**"
    assert "<b>" not in rendered


def test_render_owner_markdown_heading_with_a_fence_drops_the_bold_wrapper() -> None:
    rendered = render_owner_markdown("### ```x```")
    assert rendered == "<pre>x</pre>"
    assert "<b>" not in rendered


def test_render_owner_markdown_multiline_fence_inside_bold_stays_literal() -> None:
    rendered = render_owner_markdown("**```\nline1\nline2\n```**")
    assert rendered == "**<pre>line1\nline2</pre>**"
    assert "<b>" not in rendered


# ------------------------------------------- placeholder collision guard (P3)


def test_render_owner_markdown_strips_literal_placeholder_bytes_from_input() -> None:
    """Model text containing our own internal sentinel must not collide with it.

    Without stripping \\x00/\\x01 first, a literal "\\x00PRE0\\x00" in the input would
    be replaced a second time when the real fence's placeholder is restored.
    """
    rendered = render_owner_markdown("\x00PRE0\x00 and ```real fence```")
    assert "\x00" not in rendered
    assert "\x01" not in rendered
    assert rendered == "PRE0 and <pre>real fence</pre>"


def test_render_owner_markdown_strips_literal_code_placeholder_bytes() -> None:
    rendered = render_owner_markdown("\x01CODE0\x01 and `real code`")
    assert "\x01" not in rendered
    assert rendered == "CODE0 and <code>real code</code>"
