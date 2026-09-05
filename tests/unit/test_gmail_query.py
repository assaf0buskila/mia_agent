"""normalize_gmail_query: the deterministic, pure-function Gmail search normalizer.

Gmail AND-matches every bare term against message text, so an owner phrase sent
verbatim ("תבדקי את המייל של דניאל") fails on the function word "של", which never
appears literally in the mail. This is the highest-value pure-unit surface in the
Telegram owner slice: no I/O, no model, no settings -- just tokenize, strip known
filler, optionally wrap a lone name into a sender-or-text search, and optionally
append at most one relative-time operator.
"""

from __future__ import annotations

import pytest
from app.domain.gmail.query import normalize_gmail_query

# --------------------------------------------------------------------- invariants


def _assert_no_invented_content(raw: str, query: str) -> None:
    """Every token normalize_gmail_query emits must trace back to the raw input.

    The only content the function is allowed to add is structural: `from:`,
    `OR`, parentheses, and a `newer_than:Nd` operator it selected itself from a
    fixed, known set. Anything else in the output must be a case-insensitive
    substring of the raw text -- the function must never invent a name, a
    company, or a date that was not actually said.
    """
    raw_low = raw.lower()
    cleaned = query.replace("(", " ").replace(")", " ")
    for word in cleaned.split():
        low = word.lower()
        if low == "or":
            continue
        if low.startswith("from:"):
            low = low[len("from:") :]
        if not low or low.startswith("newer_than:"):
            continue
        assert low in raw_low, f"invented token {word!r} not present in raw {raw!r}"


def _assert_at_most_one_time_operator(query: str) -> None:
    assert query.count("newer_than:") <= 1


def _assert_never_empty(query: str) -> None:
    assert query.strip() != ""


# --------------------------------------------------------------- operator passthrough


@pytest.mark.parametrize(
    "raw",
    [
        "from:daniel@example.com",
        "subject:invoice",
        "from:daniel subject:proposal",
        "is:unread",
        "label:leads",
        "after:2024/01/01",
        "newer_than:3d",
        "filename:pdf",
    ],
)
def test_operator_passthrough_leaves_input_untouched(raw: str) -> None:
    result = normalize_gmail_query(raw)
    assert result.query == raw
    assert result.changed is False
    assert result.reason == "operators_present"


def test_a_precise_query_with_an_operator_is_never_widened() -> None:
    """A precise from: query must never be broadened just because it might return less."""
    raw = "from:daniel@bigclient.com subject:contract"
    result = normalize_gmail_query(raw)
    assert result.query == raw


# -------------------------------------------------------- Hebrew clitic stopwords


def test_what_is_in_the_inbox_returns_raw_text_not_a_fabricated_sender_filter() -> None:
    """Real bug this pins: 'בתיבה' (in-the-inbox) is a stopword ('תיבה') plus the

    clitic 'ב'. A whole-token-only stopword match would let it slip through as a
    real search term and get wrapped into a fabricated `from:בתיבה` sender filter
    -- a confidently wrong search for a sender literally named "inbox". Every
    token in "מה יש בתיבה" is recognised as filler (directly or via a clitic), so
    the function must fall back to the raw string with reason "empty" instead.
    """
    raw = "מה יש בתיבה"
    result = normalize_gmail_query(raw)
    assert result.query == raw
    assert result.reason == "empty"
    assert result.changed is False
    assert "from:בתיבה" not in result.query
    assert "בתיבה" not in result.query.replace(raw, "")  # nothing new was added


@pytest.mark.parametrize(
    "raw",
    [
        "מה יש בתיבה",
        "מה יש בתיבה?",
        "תבדקי רגע מה נכנס",
        "תבדקי רגע בבקשה",
        "תראי מה יש",
    ],
)
def test_all_filler_hebrew_phrasings_fall_back_to_raw_with_empty_reason(raw: str) -> None:
    result = normalize_gmail_query(raw)
    assert result.reason == "empty"
    assert result.query == raw
    assert "from:" not in result.query


# ------------------------------------------------------------------- a stated name


def test_a_stated_name_survives_and_is_wrapped_as_sender_or_text() -> None:
    result = normalize_gmail_query("תבדקי את המייל של דניאל")
    assert "דניאל" in result.query
    assert result.reason == "sender_or_text"
    assert result.changed is True


def test_an_english_stated_name_survives_and_is_wrapped() -> None:
    result = normalize_gmail_query("did Daniel email me?")
    assert "Daniel" in result.query
    assert result.reason == "sender_or_text"


def test_find_the_latest_thing_from_a_named_sender() -> None:
    result = normalize_gmail_query("find the latest thing from Roy")
    assert "Roy" in result.query
    assert "?" not in result.query


# --------------------------------------------------------------------- relative time


def test_today_produces_exactly_one_newer_than_operator_and_drops_the_trigger_word() -> None:
    result = normalize_gmail_query("תבדקי מיילים היום")
    assert result.query == "newer_than:1d"
    assert result.reason == "relative_time"
    # The trigger word itself must not survive as a literal AND-matched term --
    # otherwise Gmail AND-matches "today" against message text and guarantees zero
    # results even though the operator is doing the real work.
    assert "היום" not in result.query


def test_today_english_produces_exactly_one_newer_than_operator() -> None:
    result = normalize_gmail_query("anything important come in today?")
    assert result.query.count("newer_than:") == 1
    assert "today" not in result.query.lower()


def test_relative_time_combined_with_a_real_term_keeps_both() -> None:
    result = normalize_gmail_query("תבדקי מדניאל היום")
    assert "newer_than:1d" in result.query
    assert "דניאל" in result.query
    assert result.query.count("newer_than:") == 1


def test_yesterday_and_this_week_map_to_distinct_windows() -> None:
    yesterday = normalize_gmail_query("אתמול קיבלתי משהו")
    assert yesterday.query == "newer_than:2d"
    this_week = normalize_gmail_query("this week")
    assert this_week.query == "newer_than:7d"


def test_a_sentence_that_is_otherwise_pure_filler_still_resolves_via_its_time_word() -> None:
    """'היה משהו חשוב היום במייל' (was there something important today in mail) is

    filler apart from one real signal: 'today'. Every other token strips as a
    stopword, but 'היום' is not itself filler -- it is the relative-time trigger,
    so the right outcome is a real newer_than:1d query, not a raw-text fallback.
    """
    result = normalize_gmail_query("היה משהו חשוב היום במייל")
    assert result.query == "newer_than:1d"
    assert result.reason == "relative_time"


# ----------------------------------------------------------- punctuation / verbatim


@pytest.mark.parametrize(
    "raw",
    [
        "did Daniel email me?",
        "find the latest thing from Roy?",
        "תבדקי רגע את המייל של דניאל?",
    ],
)
def test_trailing_punctuation_never_survives(raw: str) -> None:
    result = normalize_gmail_query(raw)
    assert "?" not in result.query


def test_quoted_phrases_survive_verbatim() -> None:
    result = normalize_gmail_query('תבדקי "פגישה דחופה" מדניאל')
    assert '"פגישה דחופה"' in result.query


def test_email_addresses_survive_verbatim() -> None:
    result = normalize_gmail_query("check email from daniel@example.com")
    assert "daniel@example.com" in result.query


def test_digits_and_dates_survive_verbatim() -> None:
    result = normalize_gmail_query("check email from March 5 2024")
    assert "5" in result.query.split()
    assert "2024" in result.query.split()


# ---------------------------------------------------------------- never empty


@pytest.mark.parametrize(
    "raw",
    [
        "מה יש בתיבה",
        "תבדקי רגע בבקשה",
        "please check",
    ],
)
def test_never_returns_an_empty_query_for_non_blank_input(raw: str) -> None:
    """A raw phrase that carries no usable content always falls back to itself --

    it never comes back as an empty string, which would search for nothing.
    """
    result = normalize_gmail_query(raw)
    assert result.query != ""


def test_blank_input_reports_empty_reason_without_crashing() -> None:
    result = normalize_gmail_query("")
    assert result.reason == "empty"
    assert result.changed is False


# ------------------------------------------------------------------ passthrough


def test_an_already_clean_multiword_query_passes_through_unchanged() -> None:
    result = normalize_gmail_query("quarterly proposal")
    assert result.query == "quarterly proposal"
    assert result.changed is False
    assert result.reason == "passthrough"


# --------------------------------------------------------- realistic sweep (invariants)


REALISTIC_PHRASINGS = [
    "מה יש לי במייל?",
    "תבדקי רגע מה נכנס",
    "היה משהו חשוב היום במייל?",
    "what's in my inbox?",
    "can you check my mail?",
    "anything important come in today?",
    "דניאל שלח לי משהו?",
    "תחפשי רגע את המיילים מרועי",
    "did Daniel email me?",
    "find the latest thing from Roy",
    "מה הוא כתב שם?",
    "תפתחי את האחרון",
    "what did he say?",
    "read the latest one",
    "תבדקי inbox מהיום",
    "find לי את Daniel מהלידים",
    "מה יש לי calendar מחר",
    "תבדקי את המייל של דניאל",
    "מה קיבלתי השבוע מרועי כהן?",
    "any email from the client this week?",
]


@pytest.mark.parametrize("raw", REALISTIC_PHRASINGS)
def test_realistic_phrasing_sweep_keeps_the_safety_invariants(raw: str) -> None:
    result = normalize_gmail_query(raw, now=None)
    _assert_never_empty(result.query)
    _assert_at_most_one_time_operator(result.query)
    _assert_no_invented_content(raw, result.query)


def test_reason_is_always_one_of_the_documented_values() -> None:
    allowed = {
        "passthrough",
        "operators_present",
        "stopwords_stripped",
        "sender_or_text",
        "relative_time",
        "empty",
    }
    for raw in [*REALISTIC_PHRASINGS, "", "from:x@y.com", "quarterly proposal"]:
        result = normalize_gmail_query(raw)
        assert result.reason in allowed
