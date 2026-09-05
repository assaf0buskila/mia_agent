"""Routing for natural Hebrew/English owner phrasing (ADR-031 / owner_agent_v3).

This is the product contract Assaf actually cares about: he no longer has to use a
canned command phrase. A real question about his inbox, calendar, a lead, or his day
must reach the agent instead of being swallowed by the old-style deterministic
keyword classifier -- and a genuine bare greeting must still resolve to the short,
static hello without ever reaching the model.

A unit test cannot prove the model *chooses* the right tool for a given phrase (that
is verified against real CloudWatch traces). What is deterministic and pinned here is
routing: whether `classify_owner_task` intercepts the phrase into a
deterministic-only intent (`app.domain.owner.brain.DETERMINISTIC_TASK_TYPES`, gated by
`agent_allowed_for`) instead of letting the agent answer it.
"""

from __future__ import annotations

import pytest
from app.domain.owner.brain import agent_allowed_for
from app.domain.owner.tasks import (
    OwnerTaskType,
    ack_for_owner_task,
    classify_owner_task,
    promote_unclassified_text_to_status,
)

# --------------------------------------------------------------------- helpers


def _routed(text: str):
    """The exact promotion pipeline `app/api/inbound.py` runs before dispatch."""
    return promote_unclassified_text_to_status(
        classify_owner_task(text), inbound_source=None, text=text
    )


def _assert_reaches_agent(text: str) -> None:
    """The real contract: does this phrasing reach the agent, or does the

    deterministic classifier answer it on its own? `OWNER_STATUS`, `GMAIL_SUMMARY`
    and `GMAIL_DRAFT` are named explicitly because they are the concrete failure
    modes this slice replaced (a real question mistaken for a greeting or an
    already-ingested-thread summary); `agent_allowed_for` is the actual mechanism
    that gate is built on, so it is the authoritative assertion.
    """
    decision = _routed(text)
    assert agent_allowed_for(decision.task_type) is True, (
        f"{text!r} was intercepted by the deterministic classifier as "
        f"{decision.task_type!r} and never reaches the agent"
    )
    assert decision.task_type != OwnerTaskType.OWNER_STATUS
    assert decision.task_type != OwnerTaskType.GMAIL_SUMMARY
    assert decision.task_type != OwnerTaskType.GMAIL_DRAFT


# ------------------------------------------------------------------------ inbox


@pytest.mark.parametrize(
    "text",
    [
        "מה יש לי במייל?",
        "תבדקי רגע מה נכנס",
        "היה משהו חשוב היום במייל?",
        "what's in my inbox?",
        "can you check my mail?",
        "anything important come in today?",
    ],
)
def test_inbox_phrasing_reaches_the_agent(text: str) -> None:
    _assert_reaches_agent(text)


# ----------------------------------------------------------------------- sender


@pytest.mark.parametrize(
    "text",
    [
        "דניאל שלח לי משהו?",
        "תחפשי רגע את המיילים מרועי",
        "did Daniel email me?",
        "find the latest thing from Roy",
    ],
)
def test_sender_phrasing_reaches_the_agent(text: str) -> None:
    _assert_reaches_agent(text)


# --------------------------------------------------------------------- read one


@pytest.mark.parametrize(
    "text",
    [
        "מה הוא כתב שם?",
        "תפתחי את האחרון",
        "what did he say?",
        "read the latest one",
    ],
)
def test_read_one_phrasing_reaches_the_agent(text: str) -> None:
    _assert_reaches_agent(text)


# ------------------------------------------------------------------ lead by name


@pytest.mark.parametrize(
    "text",
    [
        "מה קורה עם יוסי כהן?",
        "תמצאי לי את דניאל מהלידים",
        "find Yossi Cohen",
        "what happened with Daniel's lead?",
        "find לי את Daniel מהלידים",
    ],
)
def test_lead_by_name_phrasing_reaches_the_agent(text: str) -> None:
    """Whichever task_type the deterministic classifier lands on, the agent must

    still get the chance to answer -- `agent_allowed_for` is the real gate.

    Note: 3 of these 5 phrasings ("...מהלידים" / "...lead?") keyword-match the
    SALES intent (the word "לידים"/"lead" is in that keyword set) rather than
    staying NOTE. That still reaches the agent (SALES is not in
    DETERMINISTIC_TASK_TYPES) so the owner-visible answer is unaffected, but it
    also silently logs a spurious "sales follow-up" OwnerTaskRow as a side effect
    of what was only a lookup question -- a real classifier imprecision, reported
    separately rather than fixed here (tests/ is the only path owned by this task).
    """
    _assert_reaches_agent(text)


# ------------------------------------------------------------------------ calendar


@pytest.mark.parametrize(
    "text",
    [
        "מה יש לי מחר?",
        "יש משהו עם רון השבוע?",
        "what am I doing tomorrow?",
        "do I have anything with Ron this week?",
    ],
)
def test_calendar_phrasing_reaches_the_agent(text: str) -> None:
    _assert_reaches_agent(text)


# --------------------------------------------------------------------------- today


@pytest.mark.parametrize(
    "text",
    [
        "תבדקי מה עבר עליי היום",
        "יש משהו שאני צריך לדעת מהיום?",
        "anything I should know from today?",
    ],
)
def test_today_phrasing_reaches_the_agent(text: str) -> None:
    _assert_reaches_agent(text)


def test_what_happened_today_hebrew_keyword_matches_daily_brief_by_design() -> None:
    """'מה קרה היום?' already keyword-matches DAILY_BRIEF -- existing, intended

    behaviour (it is a `_READ_COMBINE_TYPES` read, listed by name in the product
    dedicated-match table). It still reaches the agent because DAILY_BRIEF is not
    in DETERMINISTIC_TASK_TYPES, so forcing it to NOTE would be testing for the
    wrong thing; what matters is that it is not swallowed by OWNER_STATUS.
    """
    decision = _routed("מה קרה היום?")
    assert decision.task_type == OwnerTaskType.DAILY_BRIEF
    assert agent_allowed_for(decision.task_type) is True


def test_what_happened_today_english_keyword_matches_daily_brief_by_design() -> None:
    decision = _routed("what happened today?")
    assert decision.task_type == OwnerTaskType.DAILY_BRIEF
    assert agent_allowed_for(decision.task_type) is True


# -------------------------------------------------------------------- mixed language


@pytest.mark.parametrize(
    "text",
    [
        "תבדקי inbox מהיום",
        "find לי את Daniel מהלידים",
        "מה יש לי calendar מחר",
    ],
)
def test_mixed_language_phrasing_reaches_the_agent(text: str) -> None:
    _assert_reaches_agent(text)


# ------------------------------------------------------------- greeting strictness


@pytest.mark.parametrize("text", ["היי", "שלום", "hey"])
def test_a_bare_greeting_alone_stays_a_hard_owner_status_greeting(text: str) -> None:
    decision = _routed(text)
    assert decision.task_type == OwnerTaskType.OWNER_STATUS
    assert agent_allowed_for(decision.task_type) is False
    assert ack_for_owner_task(decision) == "היי אסף, אני כאן."


@pytest.mark.parametrize(
    "text",
    [
        "היי מה יש לי היום",
        "שלום תבדקי מייל",
        "hey check my calendar",
    ],
)
def test_a_greeting_word_plus_a_real_ask_does_not_become_a_greeting(text: str) -> None:
    """A greeting word is not enough on its own -- the moment a real ask rides

    along with it, this must not collapse into the static OWNER_STATUS hello, and
    must still reach the agent.
    """
    decision = _routed(text)
    assert decision.task_type != OwnerTaskType.OWNER_STATUS
    assert agent_allowed_for(decision.task_type) is True
