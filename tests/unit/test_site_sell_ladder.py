"""Mia must stop interrogating and start selling.

A real prospect described a gel-nail business, answered six discovery questions in a
row, was never told what AssafWeb would build for her, was never asked for a phone
number, and finally typed "נכשלת" — you failed. Every substantive turn mapped to the
same action and the same intent, so turn 1 and turn 20 were identical.
"""

from __future__ import annotations

from app.domain.sales import NextAction
from app.surfaces.site_policy import (
    ASK_CONTACT_AFTER_TURNS,
    classify_site_intent,
    decide_site_turn,
    is_frustrated,
)
from app.surfaces.site_reply import ANSWER_LADDER, answer_intent

REAL_CONVERSATION = [
    "היי יש לי עסק של לק ג'ל אני צריכה עזרה בניהול לקוחות בוואטסאפ",
    "שיבוץ לקוחות ביומן שלי ותמחור מחירים",
    "מגיעה אלי מאינסטגרם לוואטסאפ ואז אני מתאמת איתה",
    "מבינה את סוג הלק בגדול",
    "זהו",
]


def _decide(text: str, *, turns: int, need_seen: bool = True, frustrated: bool = False):
    return decide_site_turn(
        thought=text,
        language="he",
        has_contact=False,
        already_confirmed=False,
        selling_stopped=False,
        already_pinged=False,
        visitor_turns=turns,
        frustrated=frustrated,
        need_seen=need_seen,
    )


def test_the_bug_she_no_longer_asks_forever() -> None:
    """Replay of the conversation that failed. She must reach the offer."""
    actions = [
        _decide(text, turns=i).action
        for i, text in enumerate(REAL_CONVERSATION, start=1)
    ]
    assert "ask_contact" in actions, f"never offered to connect them: {actions}"
    assert actions.index("ask_contact") <= 4, f"took too long: {actions}"


def test_the_ladder_changes_move_every_turn() -> None:
    """Turn 1 and turn 3 must not be the same move."""
    intents = [answer_intent(visitor_turns=t, frustrated=False) for t in (1, 2, 3)]
    assert len(set(intents)) == 3, intents
    # Turn 2 is the one that was missing: name what we would take off their hands.
    assert intents[1] is NextAction.OFFER_HYPOTHESIS


def test_frustration_stops_the_questions_immediately() -> None:
    assert is_frustrated("נכשלת")
    assert is_frustrated("את לא מבינה")
    assert is_frustrated("you failed")
    assert not is_frustrated("יש לי עסק של לק ג'ל")
    # Not another question — the offer.
    assert answer_intent(visitor_turns=2, frustrated=True) is NextAction.OFFER_HYPOTHESIS
    assert _decide("נכשלת", turns=2, frustrated=True).action == "ask_contact"


def test_a_visitor_with_no_business_need_is_never_asked_for_a_phone() -> None:
    """A student on a school project is not a lead."""
    decision = _decide(
        "I'm a student with a school project", turns=6, need_seen=False
    )
    assert decision.action == "answer"
    assert decision.ask_contact is False


def test_describing_your_own_pricing_work_is_not_a_price_question() -> None:
    """'Pricing eats my day' is a pain to sell into, not 'what do you charge'."""
    assert classify_site_intent("שיבוץ לקוחות ביומן שלי ותמחור מחירים") != "price"
    assert classify_site_intent("תמחור לוקח לי המון זמן") != "price"
    # Real price questions still route to the no-invented-price guardrail.
    for ask in ("כמה עולה?", "מה המחיר שלכם?", "what's the price", "how much?"):
        assert classify_site_intent(ask) == "price", ask


def test_contact_is_not_asked_before_the_ladder_is_earned() -> None:
    early = _decide("אני מתאמת תורים בוואטסאפ", turns=1)
    assert early.action == "answer"
    late = _decide("אני מתאמת תורים בוואטסאפ", turns=ASK_CONTACT_AFTER_TURNS)
    assert late.action == "ask_contact"


def test_the_ladder_never_runs_off_its_end() -> None:
    for turns in (0, 1, 5, 99):
        assert answer_intent(visitor_turns=turns, frustrated=False) in ANSWER_LADDER
