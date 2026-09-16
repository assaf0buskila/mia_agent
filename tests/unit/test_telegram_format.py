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
    dotted_date,
    esc,
    hebrew_date,
    hebrew_datetime,
    isolate,
    join_sections,
    key_values,
    owner_text,
    parse_callback_token,
    plain_text_length,
    relative_hebrew_day,
    render_owner_markdown,
    section,
    split_message,
)

_FSI = "⁨"
_PDI = "⁩"
_RLM = "‏"

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

    Without folding a cut landing at the span's end into the current chunk, the naive
    close/reopen produces a second, useless "<pre></pre>" chunk. Every chunk must
    still be non-empty, balanced, and within the limit (the exact chunk count is not
    load-bearing — the close-tag budget reservation can legitimately split this into
    more than one non-empty chunk).
    """
    from app.integrations.telegram_format import _CHUNK_BUDGET

    content_len = _CHUNK_BUDGET - len("<pre>")
    rendered = "<pre>" + "c" * content_len + "</pre>"
    assert len(rendered) > _CHUNK_BUDGET
    chunks = split_message(rendered)
    assert "<pre></pre>" not in chunks
    for chunk in chunks:
        assert chunk.count("<pre>") == chunk.count("</pre>") == 1
        assert len(chunk) <= MAX_MESSAGE_CHARS
    rejoined = "".join(chunks).replace("</pre><pre>", "")
    assert rejoined == rendered


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


# ------------------------------------ heading + inline code stays plain (P2)


def test_render_owner_markdown_heading_with_inline_code_is_a_plain_bold_span() -> None:
    """A heading is always exactly one plain <b>...</b> span - never <b>...<code>...

    `_UNSPLITTABLE_SPAN_RE`'s <b> alternative requires no inner "<", so a <code> nested
    inside it would go unprotected and a hard cut could land inside it, unclosed.
    Backticks inside a heading stay literal instead of becoming <code>.
    """
    raw = "# " + "title " * 700 + "`code here` " + "more " * 200
    rendered = render_owner_markdown(raw)
    assert "<code>" not in rendered
    assert rendered.count("<b>") == 1
    assert rendered.startswith("<b>") and rendered.endswith("</b>")
    for limit in (3900, 4096, 50):
        chunks = split_message(rendered, limit=limit)
        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.count("<b>") == chunk.count("</b>")
            assert not chunk.startswith("</b>")
            assert not chunk.endswith("<b>")


# --------------------------- span opening exactly at the cut is deferred (P3)


def test_split_message_defers_an_oversized_pre_that_opens_at_the_cut() -> None:
    raw = "w" * 3895 + "```\n" + "m" * 5000 + "\n```"
    rendered = render_owner_markdown(raw)
    chunks = split_message(rendered)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_CHARS
        assert chunk.count("<pre>") == chunk.count("</pre>")
        assert not chunk.startswith("</pre>")
        assert not chunk.endswith("<pre>")
        assert "<pre></pre>" not in chunk


def test_split_message_defers_an_oversized_bold_span_that_opens_at_the_cut() -> None:
    rendered = "w" * 3897 + "<b>" + "m" * 5000 + "</b>"
    chunks = split_message(rendered)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_CHARS
        assert chunk.count("<b>") == chunk.count("</b>")
        assert not chunk.startswith("</b>")
        assert not chunk.endswith("<b>")
        assert "<b></b>" not in chunk


def test_split_message_defers_an_oversized_code_span_that_opens_at_the_cut() -> None:
    rendered = "w" * 3894 + "<code>" + "m" * 5000 + "</code>"
    chunks = split_message(rendered)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= MAX_MESSAGE_CHARS
        assert chunk.count("<code>") == chunk.count("</code>")
        assert not chunk.startswith("</code>")
        assert not chunk.endswith("<code>")
        assert "<code></code>" not in chunk


# ------------------------------- minimum-limit guard against zero progress (P3)


def test_split_message_terminates_when_limit_is_smaller_than_a_tag_pair() -> None:
    """limit 10-13 is smaller than <pre>'s open+close (11 chars) combined.

    Without a floor, the reopen path's per-iteration progress could hit zero and spin
    forever. `chunks` returning at all (rather than the test hanging) is the point.
    """
    rendered = "<pre>" + "m" * 500 + "</pre>"
    for limit in (10, 11, 12, 13):
        chunks = split_message(rendered, limit=limit)
        assert chunks
        for chunk in chunks:
            assert chunk.count("<pre>") == chunk.count("</pre>")
            assert "<pre></pre>" not in chunk
        rejoined = "".join(chunks).replace("</pre><pre>", "")
        assert rejoined == rendered


# --------------------------------------------- unclosed <blockquote> bug (C9)


def test_split_message_never_splits_a_blockquote_unclosed() -> None:
    """Before C9, `<blockquote>`/`<blockquote expandable>` were not in
    `_UNSPLITTABLE_SPAN_RE`'s allowlist, so a long one split unclosed and
    Telegram rejected the whole message with "can't parse entities". There is
    no production caller of `blockquote()` yet, so this was latent -- fixed
    here rather than left as a trap for whoever calls it first.
    """
    long_quote = blockquote("א" * 9000, expandable=True)
    text = "לפני. " + long_quote + " אחרי."
    for limit in (64, 100, 512, 3900):
        chunks = split_message(text, limit=limit)
        assert len(chunks) > 1
        for chunk in chunks:
            opens = len(re.findall(r"<blockquote(?: expandable)?>", chunk))
            assert opens == chunk.count("</blockquote>")
            assert "<blockquote></blockquote>" not in chunk
            assert "<blockquote expandable></blockquote>" not in chunk


# ============================================================== owner_text (C9)
# House rules for Hebrew owner output, enforced at egress: R1 (no dash as
# punctuation), R7 (every owner line starts with Hebrew, in bidi terms). See
# owner_text()'s own docstring and the module comment above it for the
# mechanism this pins.


def _strip_bidi(text: str) -> str:
    return text.replace(_FSI, "").replace(_PDI, "").replace(_RLM, "")


# ---------------------------------------------------- golden codepoint tests


def test_owner_text_golden_the_real_production_message() -> None:
    """The exact shape of the message Assaf received, round-tripped through
    owner_text alone -- no builder, no structural rewrite, just the egress
    normaliser. Isolates present, no em-dash survives, every digit unchanged.
    """
    message = (
        "נבדקו 25 הודעות ב־17 שרשורים. התיבה חלקית — "
        "ייתכן שיש הודעות נוספות מעבר לעמוד הראשון.\n"
        "• Vercel — כניסה חדשה לחשבון בשעה 10:54. כדאי לוודא שזו הייתה הכניסה שלך.\n"
        "• GitHub/Vercel — עדכון על PR #31 בנושא FAQPage JSON-LD בעמוד "
        "/blog/soken-koli."
    )
    expected = (
        "נבדקו ⁨25⁩ הודעות ב־⁨17⁩ שרשורים. התיבה חלקית, "
        "ייתכן שיש הודעות נוספות מעבר לעמוד הראשון.\n"
        "• ‏⁨Vercel⁩, כניסה חדשה לחשבון בשעה ⁨10:54⁩. "
        "כדאי לוודא שזו הייתה הכניסה שלך.\n"
        "• ‏⁨GitHub/Vercel⁩, עדכון על ⁨PR #31⁩ בנושא "
        "⁨FAQPage JSON-LD⁩ בעמוד ⁨/blog/soken-koli⁩."
    )
    out = owner_text(message)
    assert out == expected
    assert "–" not in out and "—" not in out and "--" not in out
    assert re.findall(r"\d+", _strip_bidi(out)) == re.findall(r"\d+", message)
    assert owner_text(out) == out  # idempotent


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "עדכון על PR #31 בעמוד /blog/soken-koli.",
            "עדכון על ⁨PR #31⁩ בעמוד ⁨/blog/soken-koli⁩.",
        ),
        ("שעות פעילות 09:00-17:00 בזום.", "שעות פעילות ⁨09:00-17:00⁩ בזום."),
        ("המודל gpt-5.6 עודכן.", "המודל ⁨gpt-5.6⁩ עודכן."),
        # A label -> value dash becomes a colon only when a BUILDER chose that
        # shape explicitly; the automatic fallback for ungoverned model prose
        # (R1's own stated fallback) is a comma.
        ("סטטוס — פעיל", "סטטוס, פעיל"),
        # A line-leading dash is deleted outright, not converted.
        ("— פריט ברשימה", "פריט ברשימה"),
        # A dash with no surrounding spaces is data (part of one LTR token),
        # left untouched and isolated whole.
        ("המזהה soken-koli תקין.", "המזהה ⁨soken-koli⁩ תקין."),
        # P1 review fix: a dash right after an opaque span (here, a backtick
        # code span) must still become a comma, not a line-leading deletion
        # that glues the two sides together. `^` in `_LINE_LEADING_DASH_RE`
        # used to treat the gap's own position 0 as a fresh line.
        (
            "הרצתי `git status` — הכול נקי.",
            "הרצתי `git status`, הכול נקי.",
        ),
    ],
)
def test_owner_text_golden_lines(raw: str, expected: str) -> None:
    assert owner_text(raw) == expected


def test_owner_text_golden_html_dash_right_after_a_tag_becomes_a_comma() -> None:
    """P1 review fix, html=True shape: the gap right after `</b>` is not a
    real line start either -- `<b>ליד</b> — דנה` used to lose both spaces
    and become `<b>ליד</b>דנה`.
    """
    assert owner_text("<b>ליד</b> — דנה", html=True) == "<b>ליד</b>, דנה"


def test_owner_text_leading_rlm_when_first_strong_char_is_latin() -> None:
    """R7: Telegram derives paragraph direction from the first strong character
    of the line -- a Hebrew line that starts (after a bullet) with a Latin
    token needs an explicit RLM, or the whole line flips to an LTR base and
    the bullet/punctuation land on the wrong side.
    """
    out = owner_text("• Vercel: כניסה חדשה.")
    assert out.startswith("• " + _RLM + _FSI + "Vercel" + _PDI)


def test_owner_text_no_rlm_when_line_already_starts_hebrew() -> None:
    out = owner_text("כניסה חדשה ל-Vercel.")
    assert _RLM not in out


def test_owner_text_pure_english_gets_dash_rule_only() -> None:
    """No Hebrew in the line -> no base direction to protect, so isolating
    would add controls for no reason; only the dash rule applies.
    """
    out = owner_text("Status -- active. See gpt-5.6 for details.")
    assert _FSI not in out and _PDI not in out
    assert out == "Status, active. See gpt-5.6 for details."


# --------------------------------------------------------- structural properties
# Adversarial fixtures: Latin-only text, a Hebrew sentence with an ASCII-quoted
# query, an email address, a long id, an em-dash inside a provider-shaped
# snippet, a time range, and a ~6000-char body (forces multi-chunk downstream).

_ADVERSARIAL_CORPUS = [
    "נבדקו 25 הודעות ב־17 שרשורים. התיבה חלקית — ייתכן שיש הודעות נוספות.",
    "• Vercel — כניסה חדשה לחשבון בשעה 10:54.",
    "• GitHub/Vercel — עדכון על PR #31 בעמוד /blog/soken-koli.",
    "Subject only, no Hebrew at all -- still needs the dash fixed.",
    'שאילתה עם "מרכאות" ומילים באנגלית inside quotes -- like this.',
    "המייל a.b+tag@example.co.il התקבל.",
    "המזהה lead_ab12cd34ef56gh78 עודכן.",
    "מזהה ארוך: " + "x" * 41 + " נשמר.",
    "פער — בין שתי עובדות בלי בילדר.",
    "טווח שעות 09:00-17:00 ו-18:00-20:00 פנויים.",
    "עדכון: " + ("תוכן ארוך. " * 600),
]


@pytest.mark.parametrize("raw", _ADVERSARIAL_CORPUS)
def test_owner_text_is_idempotent(raw: str) -> None:
    """Double application at two egress points (e.g. a builder that already
    called owner_text, then the outbound_reply adoption site) must be harmless.
    """
    once = owner_text(raw)
    assert owner_text(once) == once
    once_html = owner_text(raw, html=True)
    assert owner_text(once_html, html=True) == once_html


@pytest.mark.parametrize("raw", _ADVERSARIAL_CORPUS)
def test_owner_text_no_banned_dash_form_survives(raw: str) -> None:
    out = owner_text(raw)
    assert "–" not in out
    assert "—" not in out
    assert " -- " not in out


@pytest.mark.parametrize("raw", _ADVERSARIAL_CORPUS)
def test_owner_text_preserves_every_digit_run(raw: str) -> None:
    """R5, data is immutable: owner_text may insert bidi controls and change
    dash punctuation, never a digit. Comparing digit runs after stripping bidi
    controls enforces that mechanically instead of by eyeballing.
    """
    out = owner_text(raw)
    assert re.findall(r"\d+", _strip_bidi(out)) == re.findall(r"\d+", raw)


@pytest.mark.parametrize("raw", _ADVERSARIAL_CORPUS)
def test_owner_text_no_isolated_run_contains_hebrew(raw: str) -> None:
    """Catches over-wrapping: an isolate must never swallow a Hebrew character."""
    out = owner_text(raw)
    for match in re.finditer(_FSI + r"([^" + _PDI + r"]*)" + _PDI, out):
        assert not re.search(r"[֐-׿]", match.group(1)), match.group(1)


@pytest.mark.parametrize("raw", _ADVERSARIAL_CORPUS)
def test_owner_text_isolate_depth_never_negative_and_ends_at_zero(raw: str) -> None:
    out = owner_text(raw)
    depth = 0
    for char in out:
        if char == _FSI:
            depth += 1
        elif char == _PDI:
            depth -= 1
        assert depth >= 0, out
    assert depth == 0, out


def test_owner_text_html_mode_never_isolates_inside_a_tag_or_entity() -> None:
    html = (
        "<b>Vercel</b> עדכן את <code>lead_ab12cd34</code> ולינק "
        '<a href="https://example.com/soken-koli?x=1">כאן</a> &amp; זהו.'
    )
    out = owner_text(html, html=True)
    for tag in re.finditer(r"<[^<>]*>", out):
        assert _FSI not in tag.group() and _PDI not in tag.group()
    for entity in re.finditer(r"&[a-zA-Z0-9#]+;", out):
        assert _FSI not in entity.group() and _PDI not in entity.group()
    for href in re.findall(r'href="([^"]*)"', out):
        assert _FSI not in href and _PDI not in href
    # <code> content is opaque to owner_text -- a builder must isolate it
    # itself (see proposal_cards._kv_lines: code(isolate(v)), never the
    # reverse), so an id already inside a code span is left exactly as-is.
    assert "<code>lead_ab12cd34</code>" in out


def test_owner_text_html_mode_is_opaque_to_pre_and_code_content() -> None:
    html = "<pre>git diff -- a.py b.py</pre> ותוכן <code>a -- b</code> חדש."
    out = owner_text(html, html=True)
    assert "<pre>git diff -- a.py b.py</pre>" in out
    assert "<code>a -- b</code>" in out


def test_owner_text_does_not_re_isolate_a_field_a_builder_already_wrapped() -> None:
    """A builder that already called isolate() on a known field must not be
    double-wrapped by the automatic egress pass -- this is what makes it safe
    to add owner_text() unconditionally at every adoption site.
    """
    pre_isolated = f"מזהה: {code(isolate('op_308abc9002f37d0'))} עודכן."
    out = owner_text(pre_isolated, html=True)
    assert out.count(_FSI) == 1
    assert out.count(_PDI) == 1


# ------------------------------------------------- no em-dash reaches egress


def test_outbound_reply_strips_dashes_from_model_prose() -> None:
    """app.api.inbound_common.outbound_reply is the single highest-value
    adoption site: the last hop for model-composed Hebrew before Telegram.
    """
    from app.api.inbound_common import outbound_reply
    from app.domain.events import Channel as _Channel

    item = {"id": "evt.1", "from": "111", "chat_id": "111"}
    message = outbound_reply(
        item,
        text="בדיקה — עם מקף וגם PR #7 בתוך המשפט.",
        channel=_Channel.TELEGRAM,
    )
    assert "–" not in message.text
    assert "—" not in message.text
    assert _FSI in message.text  # "PR #7" isolated


# --------------------------------------------------------------- dotted_date


def test_dotted_date_formats_and_isolates() -> None:
    assert dotted_date("2026-09-16") == _FSI + "16.09.2026" + _PDI


def test_dotted_date_passes_through_unrecognised_input() -> None:
    assert dotted_date("not-a-date") == "not-a-date"


# ------------------------------------------- splitter isolate invariant (C9)


@pytest.mark.parametrize("limit", [64, 100, 512, 3900])
def test_split_message_isolate_invariant_over_corpus(limit: int) -> None:
    """The four-part invariant from the C9 design, pinned exactly:
    (1) isolate depth never negative and ends at 0 in every returned chunk --
        an isolate pair is never separated across a chunk boundary;
    (2) no isolate lands inside a tag or entity, and no href value carries one;
    (3) every `<pre>`/`<b>`/`<code>`/`<blockquote>` opened in a chunk is closed
        in that same chunk;
    (4) concatenating the chunks and deleting bidi controls and tags
        reproduces the original visible content (whitespace at a cut boundary
        aside -- that trimming is `split_message`'s own pre-existing, bidi-
        unrelated behaviour) -- proving the isolate/tag handling added only
        controls and never changed data.
    """
    bodies = [render_owner_markdown(owner_text(raw)) for raw in _ADVERSARIAL_CORPUS]
    bodies.append(_FSI + ("A" * 4000) + _PDI)  # one isolated run alone > every limit
    bodies.append(bold("כותרת") + "\n" + owner_text("תוכן ארוך. " * 500, html=True))
    bodies.append(
        '<a href="https://example.com/soken-koli?x=1">'
        + owner_text("קישור")
        + "</a> חדש."
    )
    # P3 review fix: `code(isolate(v))` (proposal_cards._kv_lines's mono shape)
    # nests a full FSI...PDI run inside a `<code>` span; when that span alone
    # is bigger than `limit`, the reopen path used to cut inside the nested
    # isolate, shipping an unterminated FSI in one chunk and a stray PDI in
    # the next.
    bodies.append("לפני. " + code(isolate("op_" + "a" * 200)) + " אחרי.")
    bodies.append("X " + bold(isolate("B" * 300)) + " Y")
    for body in bodies:
        chunks = split_message(body, limit=limit)

        def _visible_no_whitespace(html: str) -> str:
            no_tags = re.sub(r"<[^<>]*>", "", html)
            return re.sub(r"\s+", "", _strip_bidi(no_tags))

        assert _visible_no_whitespace("".join(chunks)) == _visible_no_whitespace(body)  # (4)

        for chunk in chunks:
            depth = 0
            for char in chunk:
                if char == _FSI:
                    depth += 1
                elif char == _PDI:
                    depth -= 1
                assert depth >= 0, chunk  # (1)
            assert depth == 0, chunk  # (1)

            stripped = re.sub(r"<[^<>]*>|&[a-zA-Z0-9#]+;", "", chunk)
            assert stripped.count(_FSI) == chunk.count(_FSI)  # (2)
            assert stripped.count(_PDI) == chunk.count(_PDI)  # (2)
            for href in re.findall(r'href="([^"]*)"', chunk):
                assert _FSI not in href and _PDI not in href  # (2)

            for tag_name in ("pre", "b", "code", "blockquote"):
                opens = len(re.findall(rf"<{tag_name}(?: expandable)?>", chunk))
                assert opens == chunk.count(f"</{tag_name}>"), (tag_name, chunk)  # (3)
