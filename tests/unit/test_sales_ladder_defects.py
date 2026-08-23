"""Regressions for two defects found by probing the live website sales flow.

Both were reachable in production and both are asserted here at the state-machine level
(not just through the API) so a future refactor cannot quietly reintroduce them.
"""

from __future__ import annotations

import pytest
from app.domain.extract import extract_sales_signals
from app.domain.sales import (
    FitLevel,
    NextAction,
    PainLevel,
    SalesState,
    select_next_action,
)
from app.graph.replies import reply_for


def _offered_state() -> SalesState:
    """A lead who has been offered the WhatsApp handoff and has real discovery behind it."""
    return SalesState(
        lead_id="lead_test0001",
        fit=FitLevel.POSSIBLE,
        workflow_known=True,
        manual_step_known=True,
        impact_confirmed=True,
        pain_level=PainLevel.P3,
        whatsapp_handoff_offered=True,
    )


# ---------------------------------------------------------------- defect 1


@pytest.mark.parametrize(
    "reply", ["כן בבקשה", "כן", "בטח", "אוקיי", "תעבירי", "yes", "sure", "go ahead"]
)
def test_accepting_the_whatsapp_offer_hands_off(reply: str) -> None:
    """Mia offered to pass them to Assaf. They said yes. She must follow through.

    Before the fix the ladder fell back to an unmet discovery rung, so an acceptance was
    answered with a reflection question and the offer was silently abandoned.
    """
    state = extract_sales_signals(_offered_state(), reply)
    assert state.owner_required is True
    assert select_next_action(state, channel="website") is NextAction.HANDOFF


@pytest.mark.parametrize("reply", ["לא עכשיו", "לא צריך", "not now", "later"])
def test_declining_the_whatsapp_offer_does_not_hand_off(reply: str) -> None:
    state = extract_sales_signals(_offered_state(), reply)
    assert state.owner_required is False
    assert select_next_action(state, channel="website") is not NextAction.HANDOFF


def test_affirmative_before_any_offer_never_hands_off() -> None:
    """A bare 'כן' during discovery is an answer, not a handoff request."""
    state = SalesState(lead_id="lead_test0002", workflow_known=True)
    assert state.whatsapp_handoff_offered is False
    updated = extract_sales_signals(state, "כן")
    assert updated.owner_required is False
    assert select_next_action(updated, channel="website") is not NextAction.HANDOFF


def test_the_ladder_does_not_step_back_into_discovery_after_acceptance() -> None:
    """The specific regression: offer -> accept must not return REFLECT."""
    state = _offered_state()
    assert state.reflected is False  # a discovery rung is still formally unmet
    accepted = extract_sales_signals(state, "כן בבקשה")
    assert select_next_action(accepted, channel="website") is not NextAction.REFLECT


# ------------------------------------------------- price questions (NOT a defect)


@pytest.mark.parametrize(
    "question",
    ["כמה זה עולה?", "מה המחיר?", "how much does it cost?", "what is the price?"],
)
def test_a_price_question_hands_off_by_design(question: str) -> None:
    """Investigated as a suspected defect; it is deliberate.

    A price question sets `owner_required`, and `select_next_action` checks that before
    `active_objection`, so HANDLE_OBJECTION never fires and the PRICE_QUESTION reply copy
    is unreachable on this path. That is correct: there is no public price list and Mia
    must never quote a number. Pinned so nobody "fixes" it the way I first tried to.
    """
    state = extract_sales_signals(SalesState(lead_id="lead_test0003"), question)
    assert state.owner_required is True
    assert select_next_action(state, channel="website") is NextAction.HANDOFF
    assert "?" not in reply_for("website", NextAction.HANDOFF, state)


@pytest.mark.parametrize(
    "request_text",
    ["תשלחי הצעה", "הצעת מחיר", "negotiate the price", "special discount", "want a contract"],
)
def test_commercial_negotiation_also_requires_the_owner(request_text: str) -> None:
    state = extract_sales_signals(SalesState(lead_id="lead_test0004"), request_text)
    assert state.owner_required is True
    assert select_next_action(state, channel="website") is NextAction.HANDOFF


@pytest.mark.parametrize(
    "reply",
    ["sure, but that's too expensive", "כן אבל זה יקר לי", "ok but I have someone already"],
)
def test_a_qualified_yes_is_not_an_acceptance(reply: str) -> None:
    """"sure, but that's too expensive" is a conversation, not consent to hand off.

    The acceptance tokens are deliberately broad ("sure", "ok", "כן"). Two guards keep a
    qualified answer from reading as consent: a detected objection, and the conjunction
    itself — because the objection token lists are not exhaustive for every phrasing.
    """
    state = extract_sales_signals(_offered_state(), reply)
    assert state.owner_required is False
    assert select_next_action(state, channel="website") is not NextAction.HANDOFF


@pytest.mark.parametrize(
    "reply", ["ok that's right", "כן נכון", "yes exactly", "בדיוק כן"]
)
def test_confirming_a_statement_is_not_consenting_to_the_handoff(reply: str) -> None:
    """"ok that's right" confirms a reflection. It is not a yes to being passed to Assaf.

    This one derailed the clinic funnel one rung before the meeting offer, because the
    bare "ok" matched. The discriminator is the confirmation idiom, not word count —
    "כן נוח לי" is the same length and IS an acceptance.
    """
    state = extract_sales_signals(_offered_state(), reply)
    assert state.owner_required is False
    assert select_next_action(state, channel="website") is not NextAction.HANDOFF


def test_a_short_plain_yes_is_still_an_acceptance() -> None:
    """The guards must not over-block: the eval's own acceptance phrasing still works."""
    state = extract_sales_signals(_offered_state(), "כן נוח לי")
    assert state.owner_required is True
    assert select_next_action(state, channel="website") is NextAction.HANDOFF
