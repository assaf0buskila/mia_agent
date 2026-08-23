import pytest
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.extract import extract_sales_signals
from app.domain.sales import (
    FitLevel,
    NextAction,
    ObjectionKind,
    PainLevel,
    SalesState,
    select_next_action,
)
from app.graph.replies import (
    OBJECTION_REPLIES_EN,
    REFRAME_REPLIES,
    REFRAME_REPLIES_EN,
    reply_for,
)
from app.main import app
from fastapi.testclient import TestClient


def test_extract_price_en_sets_objection_and_handle_action() -> None:
    state = SalesState(lead_id="lead_obj_en")
    updated = extract_sales_signals(state, "That's too expensive for us right now.")
    assert updated.active_objection == ObjectionKind.PRICE
    assert select_next_action(updated) == NextAction.HANDLE_OBJECTION


def test_extract_price_he_sets_objection() -> None:
    state = SalesState(lead_id="lead_obj_he")
    updated = extract_sales_signals(state, "זה יקר מדי בשבילנו.")
    assert updated.active_objection == ObjectionKind.PRICE
    assert select_next_action(updated) == NextAction.HANDLE_OBJECTION


def test_ain_zman_alone_is_pain_not_no_time_objection() -> None:
    state = SalesState(lead_id="lead_pain_he")
    updated = extract_sales_signals(state, "אין זמן לענות לכולם.")
    assert updated.active_objection is None
    assert updated.pain_level >= 2


def test_eikar_does_not_set_price_objection() -> None:
    state = SalesState(lead_id="lead_eikar")
    updated = extract_sales_signals(state, "העיקר זה לשמור על איכות.")
    assert updated.active_objection is None


def test_stop_clears_stale_objection_so_nba_is_stop() -> None:
    state = SalesState(
        lead_id="lead_stale_stop",
        workflow_known=True,
        pain_level=PainLevel.P3,
        impact_confirmed=True,
        reflected=True,
        hypothesis_offered=True,
        buying_reality_known=True,
        fit=FitLevel.GOOD,
        active_objection=ObjectionKind.PRICE,
    )
    updated = extract_sales_signals(state, "not interested")
    assert updated.active_objection is None
    assert updated.willingness_to_meet is False
    assert select_next_action(updated) == NextAction.STOP


def test_hebrew_partner_token_does_not_match_inside_plural() -> None:
    updated = extract_sales_signals(
        SalesState(lead_id="lead_partners_plural"),
        "השותפים בקליניקה עמוסים.",
    )
    assert updated.active_objection is None


def test_stop_tokens_skip_objection_and_set_willingness_false() -> None:
    state = SalesState(
        lead_id="lead_stop",
        workflow_known=True,
        pain_level=PainLevel.P3,
        impact_confirmed=True,
        reflected=True,
        hypothesis_offered=True,
        buying_reality_known=True,
        fit=FitLevel.GOOD,
    )
    updated = extract_sales_signals(state, "not interested, too expensive")
    assert updated.active_objection is None
    assert updated.willingness_to_meet is False
    assert select_next_action(updated) == NextAction.STOP


def test_poor_fit_disqualifies_even_with_price_objection_in_message() -> None:
    state = SalesState(lead_id="lead_poor_price", workflow_known=True)
    updated = extract_sales_signals(state, "I'm a student and it's too expensive.")
    assert updated.fit == FitLevel.POOR
    assert updated.active_objection is None
    assert select_next_action(updated) == NextAction.DISQUALIFY


def test_poor_fit_state_beats_active_objection_in_nba() -> None:
    state = SalesState(
        lead_id="lead_poor_obj",
        workflow_known=True,
        fit=FitLevel.POOR,
        active_objection=ObjectionKind.PRICE,
    )
    assert select_next_action(state) == NextAction.DISQUALIFY


def test_next_message_without_objection_clears_active_objection() -> None:
    state = SalesState(
        lead_id="lead_clear",
        workflow_known=True,
        active_objection=ObjectionKind.PRICE,
    )
    updated = extract_sales_signals(state, "We run a clinic and miss calls all day.")
    assert updated.active_objection is None
    assert updated.workflow_known is True


def test_objection_and_pain_can_coexist_message_sets_both() -> None:
    state = SalesState(lead_id="lead_both")
    updated = extract_sales_signals(
        state,
        "We miss calls all day and it's too expensive.",
    )
    assert updated.active_objection == ObjectionKind.PRICE
    assert updated.workflow_known is True
    assert select_next_action(updated) == NextAction.HANDLE_OBJECTION


def test_website_post_price_objection_returns_handle_objection() -> None:
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        reply = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "that's too expensive"},
        )
        assert reply.status_code == 200
        body = reply.json()
        assert body["next_action"] == "handle_objection"
        assert body["message"] == OBJECTION_REPLIES_EN[ObjectionKind.PRICE]
        assert body["message"] != REFRAME_REPLIES_EN[ObjectionKind.PRICE]


def test_cold_price_objection_reply_for_is_first_move() -> None:
    state = SalesState(lead_id="lead_cold_price")
    updated = extract_sales_signals(state, "that's too expensive")
    reply = reply_for("website", NextAction.HANDLE_OBJECTION, updated)
    assert "יקר" in reply
    assert "החיכוך שכבר תיארת" not in reply


def test_reframe_context_price_objection_uses_reframe_copy() -> None:
    state = SalesState(
        lead_id="lead_reframe_price",
        workflow_known=True,
        manual_step_known=True,
        impact_confirmed=True,
        reflected=True,
        pain_level=PainLevel.P3,
    )
    updated = extract_sales_signals(state, "that's too expensive")
    assert updated.active_objection == ObjectionKind.PRICE
    assert select_next_action(updated) == NextAction.HANDLE_OBJECTION
    reply = reply_for("website", NextAction.HANDLE_OBJECTION, updated)
    assert reply == REFRAME_REPLIES[ObjectionKind.PRICE]


def test_website_funnel_price_objection_after_reflect_uses_reframe() -> None:
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        msg1 = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "We run a clinic and miss calls all day."},
        )
        assert msg1.status_code == 200
        assert msg1.json()["next_action"] == "deepen_pain"
        msg2 = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "we call everyone back by hand from a list"},
        )
        assert msg2.status_code == 200
        assert msg2.json()["next_action"] == "reflect"
        msg3 = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "about two hours every day"},
        )
        assert msg3.status_code == 200
        assert msg3.json()["next_action"] == "offer_whatsapp"
        msg4 = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "sure, but that's too expensive"},
        )
        assert msg4.status_code == 200
        body = msg4.json()
        assert body["next_action"] == "handle_objection"
        # Reframe copy, because there is real described friction to measure against.
        assert body["message"] == REFRAME_REPLIES_EN[ObjectionKind.PRICE]


def test_qualification_snapshot_includes_active_objection() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_obj_qual"
        )
        db.commit()
        sales = store.get_sales(lead_id)
        sales = extract_sales_signals(sales, "that's too expensive")
        store.save_sales(sales)
        db.commit()
        loaded = store.get_sales(lead_id)
        assert loaded.active_objection == ObjectionKind.PRICE
    finally:
        db.close()


def test_clinic_pain_sets_possible_not_good() -> None:
    state = SalesState(lead_id="lead_clinic")
    updated = extract_sales_signals(state, "We run a clinic and miss calls all day.")
    assert updated.fit == FitLevel.POSSIBLE
    assert updated.fit != FitLevel.GOOD
    assert updated.buying_reality_known is False


def test_hebrew_clinic_sets_possible() -> None:
    state = SalesState(lead_id="lead_clinic_he")
    updated = extract_sales_signals(state, "יש לנו קליניקה ולא תמיד עונה.")
    assert updated.fit == FitLevel.POSSIBLE


def test_student_stays_poor_even_with_business_token() -> None:
    state = SalesState(lead_id="lead_student_clinic")
    updated = extract_sales_signals(state, "I'm a student with a clinic project.")
    assert updated.fit == FitLevel.POOR


def test_buying_reality_alone_does_not_invent_good_from_unknown() -> None:
    state = SalesState(lead_id="lead_buying_only", pain_level=PainLevel.P3)
    updated = extract_sales_signals(state, "I decide this quarter")
    assert updated.buying_reality_known is True
    assert updated.fit == FitLevel.UNKNOWN
    assert updated.fit != FitLevel.GOOD


def _funnel_ready_except_buying(lead_id: str = "lead_buying") -> SalesState:
    return SalesState(
        lead_id=lead_id,
        workflow_known=True,
        pain_level=PainLevel.P3,
        impact_confirmed=True,
        reflected=True,
        hypothesis_offered=True,
        buying_reality_known=False,
        fit=FitLevel.POSSIBLE,
        willingness_to_meet=True,
    )


@pytest.mark.parametrize(
    "message",
    [
        "I decide this quarter",
        "I'm the owner here",
        "אני מחליט על זה",
        "אני הבעלים, עד סוף הרבעון",
    ],
)
def test_extract_buying_reality_phrases(message: str) -> None:
    state = _funnel_ready_except_buying()
    updated = extract_sales_signals(state, message)
    assert updated.buying_reality_known is True
    assert updated.fit == FitLevel.GOOD
    assert select_next_action(updated) == NextAction.OFFER_MEETING


def test_extract_proposal_sets_owner_required_and_handoff() -> None:
    state = SalesState(lead_id="lead_proposal", workflow_known=True)
    updated = extract_sales_signals(state, "Please send me a proposal")
    assert updated.owner_required is True
    assert select_next_action(updated) == NextAction.HANDOFF
    assert "אסף" in reply_for("website", NextAction.HANDOFF, updated)


def test_extract_hebrew_proposal_handoff() -> None:
    state = SalesState(lead_id="lead_proposal_he", workflow_known=True)
    updated = extract_sales_signals(state, "תשלחי הצעת מחיר")
    assert updated.owner_required is True
    assert select_next_action(updated) == NextAction.HANDOFF


def test_price_objection_does_not_set_owner_required() -> None:
    state = SalesState(lead_id="lead_price_not_owner", workflow_known=True)
    updated = extract_sales_signals(state, "that's too expensive")
    assert updated.owner_required is False
    assert updated.active_objection == ObjectionKind.PRICE


def test_ain_zman_pain_does_not_set_owner_required() -> None:
    state = SalesState(lead_id="lead_ain_zman", workflow_known=True)
    updated = extract_sales_signals(state, "אין זמן לענות לכולם.")
    assert updated.owner_required is False
    assert updated.active_objection is None


def test_ad_sof_hayom_is_not_buying_reality() -> None:
    state = _funnel_ready_except_buying("lead_ad_sof")
    updated = extract_sales_signals(
        state, "עד סוף היום אני לא תמיד עונה לטלפון."
    )
    assert updated.buying_reality_known is False
    assert select_next_action(updated) == NextAction.QUALIFY


def test_cold_not_interested_stops_instead_of_discovery() -> None:
    updated = extract_sales_signals(
        SalesState(lead_id="lead_cold_stop"),
        "not interested",
    )
    assert updated.willingness_to_meet is False
    assert select_next_action(updated) == NextAction.STOP


def test_opt_out_beats_stale_owner_required() -> None:
    state = SalesState(
        lead_id="lead_stop_after_proposal",
        workflow_known=True,
        owner_required=True,
    )
    updated = extract_sales_signals(state, "not interested")
    assert updated.willingness_to_meet is False
    assert select_next_action(updated) == NextAction.STOP


def test_owner_required_beats_price_objection_in_nba() -> None:
    state = SalesState(lead_id="lead_both_owner_price", workflow_known=True)
    updated = extract_sales_signals(
        state, "send me a proposal, that's too expensive"
    )
    assert updated.owner_required is True
    assert updated.active_objection == ObjectionKind.PRICE
    assert select_next_action(updated) == NextAction.HANDOFF


def test_facebook_page_does_not_set_willingness_to_meet() -> None:
    state = SalesState(lead_id="lead_facebook")
    updated = extract_sales_signals(state, "I saw your facebook page")
    assert updated.willingness_to_meet is None


def test_book_meeting_sets_willingness_with_word_boundary() -> None:
    state = SalesState(lead_id="lead_book")
    updated = extract_sales_signals(state, "let's book a meeting")
    assert updated.willingness_to_meet is True


def test_ecommerce_abandoned_leads_extract() -> None:
    state = SalesState(lead_id="lead_ecom")
    updated = extract_sales_signals(
        state, "We have an online store and abandoned leads every day."
    )
    assert updated.fit == FitLevel.POSSIBLE
    assert updated.pain_level >= PainLevel.P2
    assert updated.impact_confirmed is True


def test_real_estate_followup_extract() -> None:
    state = SalesState(lead_id="lead_re")
    updated = extract_sales_signals(
        state, "I'm in real estate and I forget to follow up with leads all day."
    )
    assert updated.fit == FitLevel.POSSIBLE
    assert updated.pain_level >= PainLevel.P2


def test_hebrew_real_estate_fit_token() -> None:
    state = SalesState(lead_id="lead_he_re")
    updated = extract_sales_signals(state, "יש לנו נדלן")
    assert updated.fit == FitLevel.POSSIBLE


@pytest.mark.parametrize(
    "message",
    [
        "how much does it cost",
        "כמה זה עולה",
        "I want to complain",
        "יש לי תלונה",
        "I want to speak to a human",
        "רוצה נציג",
        "do you guarantee results",
    ],
)
def test_money_complaint_human_and_promise_hand_off(message: str) -> None:
    state = SalesState(lead_id="lead_shop_handoff", workflow_known=True)
    updated = extract_sales_signals(state, message)
    assert updated.owner_required is True
    assert select_next_action(updated) == NextAction.HANDOFF
    assert "?" not in reply_for("website", NextAction.HANDOFF, updated)
