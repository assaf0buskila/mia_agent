import hashlib
import hmac

import pytest
from app.core.errors import MergeRejected, PolicyDenied, WebhookRejected
from app.core.redact import redact
from app.core.risk import RiskAction, RiskLevel, assert_allowed, decide
from app.core.webhooks import verify_webhook
from app.domain.events import Channel
from app.domain.extract import extract_sales_signals
from app.domain.identity import ChannelIdentity, IdentityIndex
from app.domain.sales import (
    FitLevel,
    NextAction,
    ObjectionKind,
    PainLevel,
    SalesState,
    compute_missing_fields,
    mark_action_delivered,
    select_next_action,
)
from app.graph.replies import reply_for


def test_redact_strips_pii_and_secrets() -> None:
    cleaned = redact({"email": "a@b.com", "phone": "050", "text": "hi", "nested": {"api_key": "x"}})
    assert cleaned["email"] == "[redacted]"
    assert cleaned["phone"] == "[redacted]"
    assert cleaned["text"] == "hi"
    assert cleaned["nested"]["api_key"] == "[redacted]"


def test_risk_policy_gates_meta_writes_and_denies_destructive() -> None:
    assert decide(RiskAction(name="read_insights", risk=RiskLevel.R0_READ)).value == "auto"
    assert (
        decide(
            RiskAction(name="ig_reply", risk=RiskLevel.R2_CUSTOMER_MESSAGE, in_approved_scope=True)
        ).value
        == "auto"
    )
    assert (
        decide(
            RiskAction(name="ig_reply", risk=RiskLevel.R2_CUSTOMER_MESSAGE, in_approved_scope=False)
        ).value
        == "approval"
    )
    meta = decide(RiskAction(name="meta_budget", risk=RiskLevel.R4_FINANCIAL_MARKETING))
    assert meta.value == "approval"
    with pytest.raises(PolicyDenied):
        assert_allowed(RiskAction(name="delete", risk=RiskLevel.R5_DESTRUCTIVE))
    with pytest.raises(PolicyDenied):
        decide(RiskAction(name="read", risk=RiskLevel.R0_READ), kill_switch=True)


def test_webhook_hmac_and_replay_window() -> None:
    secret = "test-secret"
    body = b'{"id":"1"}'
    ts = 1_700_000_000
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    verify_webhook(secret=secret, body=body, signature_hex=signature, timestamp=ts, now=ts)
    with pytest.raises(WebhookRejected):
        verify_webhook(secret=secret, body=body, signature_hex="deadbeef", timestamp=ts, now=ts)
    with pytest.raises(WebhookRejected):
        verify_webhook(
            secret=secret,
            body=body,
            signature_hex=signature,
            timestamp=ts,
            now=ts + 10_000,
        )


def test_identity_reuses_exact_channel_id_and_rejects_weak_merge() -> None:
    index = IdentityIndex()
    first = index.observe(
        ChannelIdentity(channel=Channel.WHATSAPP, external_id="+97250", verified=True)
    )
    again = index.observe(
        ChannelIdentity(channel=Channel.WHATSAPP, external_id="+97250", verified=True)
    )
    other = index.observe(
        ChannelIdentity(channel=Channel.INSTAGRAM, external_id="ig_9", verified=False)
    )
    assert first.customer_id == again.customer_id
    assert first.customer_id != other.customer_id
    with pytest.raises(MergeRejected):
        index.merge(first.customer_id, other.customer_id, verified=False)
    merged = index.merge(first.customer_id, other.customer_id, verified=True)
    assert merged.customer_id == first.customer_id
    assert len(merged.identities) == 2


def test_sales_state_is_workflow_first_and_does_not_pitch_early() -> None:
    cold = SalesState(lead_id="lead_1")
    assert select_next_action(cold) == NextAction.UNDERSTAND_WORKFLOW

    poor = SalesState(lead_id="lead_2", workflow_known=True, fit=FitLevel.POOR)
    assert select_next_action(poor) == NextAction.DISQUALIFY

    ready = SalesState(
        lead_id="lead_3",
        workflow_known=True,
        pain_level=PainLevel.P3,
        impact_confirmed=True,
        reflected=True,
        hypothesis_offered=True,
        buying_reality_known=True,
        fit=FitLevel.GOOD,
        willingness_to_meet=True,
    )
    assert select_next_action(ready) == NextAction.OFFER_MEETING
    assert select_next_action(ready, channel="website") == NextAction.OFFER_MEETING


def test_website_offers_whatsapp_after_friction_not_greeting() -> None:
    greeting = SalesState(lead_id="lead_wa_hi")
    assert select_next_action(greeting, channel="website") == NextAction.UNDERSTAND_WORKFLOW

    p1 = SalesState(
        lead_id="lead_wa_p1",
        workflow_known=True,
        pain_level=PainLevel.P1,
        fit=FitLevel.POSSIBLE,
    )
    assert select_next_action(p1, channel="website") == NextAction.DEEPEN_PAIN

    # Friction is known but the prospect has barely spoken. Keep asking.
    early = SalesState(
        lead_id="lead_wa_early",
        workflow_known=True,
        manual_step_known=True,
        pain_level=PainLevel.P2,
        fit=FitLevel.POSSIBLE,
        discovery_turns=1,
    )
    assert select_next_action(early, channel="website") == NextAction.QUANTIFY

    engaged = early.model_copy(
        update={"lead_id": "lead_wa_engaged", "discovery_turns": 3}
    )
    assert select_next_action(engaged) == NextAction.QUANTIFY
    assert select_next_action(engaged, channel="website") == NextAction.OFFER_WHATSAPP
    marked = mark_action_delivered(engaged, NextAction.OFFER_WHATSAPP)
    assert marked.whatsapp_handoff_offered is True
    assert select_next_action(marked, channel="website") == NextAction.QUANTIFY

    # Stated intent needs no ladder — there is no workflow to dig into yet.
    prelaunch = SalesState(
        lead_id="lead_wa_prelaunch",
        workflow_known=True,
        explicit_buying_intent=True,
        pain_level=PainLevel.P2,
        fit=FitLevel.POSSIBLE,
        discovery_turns=1,
    )
    assert select_next_action(prelaunch, channel="website") == NextAction.OFFER_WHATSAPP


def _impact_confirmed_unreflected(lead_id: str = "lead_reflect") -> SalesState:
    return SalesState(
        lead_id=lead_id,
        workflow_known=True,
        manual_step_known=True,
        pain_level=PainLevel.P3,
        impact_confirmed=True,
        reflected=False,
        hypothesis_offered=False,
    )


def test_mark_action_delivered_reflect_then_hypothesis() -> None:
    state = _impact_confirmed_unreflected()
    assert select_next_action(state) == NextAction.REFLECT
    marked = mark_action_delivered(state, NextAction.REFLECT)
    assert marked.reflected is True
    assert select_next_action(marked) == NextAction.OFFER_HYPOTHESIS


def test_mark_action_delivered_hypothesis_then_qualify() -> None:
    state = _impact_confirmed_unreflected().model_copy(
        update={"reflected": True, "hypothesis_offered": False}
    )
    assert select_next_action(state) == NextAction.OFFER_HYPOTHESIS
    marked = mark_action_delivered(state, NextAction.OFFER_HYPOTHESIS)
    assert marked.hypothesis_offered is True
    assert marked.buying_reality_known is False
    assert select_next_action(marked) == NextAction.QUALIFY


def test_mark_action_delivered_qualify_does_not_set_buying_reality() -> None:
    state = SalesState(
        lead_id="lead_qualify_mark",
        workflow_known=True,
        pain_level=PainLevel.P3,
        impact_confirmed=True,
        reflected=True,
        hypothesis_offered=True,
        buying_reality_known=False,
    )
    marked = mark_action_delivered(state, NextAction.QUALIFY)
    assert marked.buying_reality_known is False


def test_willing_to_meet_without_workflow_skips_understand_workflow() -> None:
    state = SalesState(lead_id="x", willingness_to_meet=True)
    assert select_next_action(state) == NextAction.QUALIFY


def test_cold_book_meeting_extract_then_qualify_not_understand_workflow() -> None:
    state = SalesState(lead_id="cold_book")
    updated = extract_sales_signals(state, "let's book a meeting")
    assert updated.willingness_to_meet is True
    assert updated.fit == FitLevel.POSSIBLE
    assert updated.workflow_known is False
    assert select_next_action(updated) == NextAction.QUALIFY


def _clinic_p3_state(lead_id: str = "lead_clinic_p3") -> SalesState:
    return SalesState(
        lead_id=lead_id,
        workflow_known=True,
        pain_level=PainLevel.P3,
        impact_confirmed=True,
        fit=FitLevel.POSSIBLE,
    )


def test_clinic_all_day_stays_p3_without_metric() -> None:
    state = SalesState(lead_id="lead_clinic_cold")
    updated = extract_sales_signals(
        state, "We run a clinic and miss calls all day."
    )
    assert updated.pain_level == PainLevel.P3
    assert updated.metric_known is False
    assert updated.missing_fields == ["decision_maker", "timeline", "metric"]


def test_extract_does_not_clear_legacy_buying_reality() -> None:
    state = SalesState(lead_id="lead_legacy_buy", buying_reality_known=True)
    updated = extract_sales_signals(state, "ok")
    assert updated.buying_reality_known is True
    assert updated.authority_known is False
    assert updated.timeline_known is False


def test_i_decide_sets_authority_not_timeline() -> None:
    updated = extract_sales_signals(SalesState(lead_id="lead_decide"), "I decide")
    assert updated.authority_known is True
    assert updated.timeline_known is False
    assert updated.buying_reality_known is True


def test_this_quarter_sets_timeline_not_authority() -> None:
    updated = extract_sales_signals(
        SalesState(lead_id="lead_quarter"), "this quarter"
    )
    assert updated.timeline_known is True
    assert updated.authority_known is False
    assert updated.buying_reality_known is True


def test_p4_does_not_jump_from_p0_on_money_only() -> None:
    updated = extract_sales_signals(SalesState(lead_id="lead_money"), "money")
    assert updated.pain_level < PainLevel.P4
    assert updated.metric_known is False


def test_p4_after_p3_from_cost_message() -> None:
    state = _clinic_p3_state()
    updated = extract_sales_signals(state, "this costs me money")
    assert updated.pain_level == PainLevel.P4
    assert updated.metric_known is True


def test_lo_dahuf_does_not_set_p5() -> None:
    state = _clinic_p3_state()
    updated = extract_sales_signals(state, "לא דחוף")
    assert updated.pain_level == PainLevel.P3
    assert updated.active_objection == ObjectionKind.NOT_URGENT


def test_compute_missing_fields_order() -> None:
    assert compute_missing_fields(SalesState(lead_id="x")) == [
        "decision_maker",
        "timeline",
        "metric",
    ]
    state = SalesState(
        lead_id="x",
        authority_known=True,
        timeline_known=True,
        metric_known=True,
    )
    assert compute_missing_fields(state) == []


def test_qualify_reply_one_question_decision_maker() -> None:
    sales = SalesState(lead_id="lead_qualify_cold")
    reply = reply_for("website", NextAction.QUALIFY, sales)
    assert "מי צריך להיות מעורב" in reply
    assert "ומתי" not in reply


def test_quantify_reply_asks_effort_not_cost() -> None:
    """QUANTIFY asks time or frequency. Money and value belong to QUALIFY metric."""
    reply = reply_for("website", NextAction.QUANTIFY)
    assert "כמה זמן" in reply
    assert "עולה" not in reply
    assert "ומתי" not in reply
    retry = reply_for("website", NextAction.QUANTIFY, repeat_ask=True)
    assert retry != reply
    assert "עולה" not in retry


def test_qualify_reply_metric_after_authority_and_timeline() -> None:
    sales = SalesState(
        lead_id="lead_qualify_metric",
        authority_known=True,
        timeline_known=True,
        missing_fields=["metric"],
    )
    reply = reply_for("website", NextAction.QUALIFY, sales)
    assert "מה זה עולה לכם" in reply
    assert "ומתי" not in reply


def test_qualify_reply_fallback_when_all_known() -> None:
    sales = SalesState(
        lead_id="lead_qualify_full",
        authority_known=True,
        timeline_known=True,
        metric_known=True,
        missing_fields=[],
    )
    reply = reply_for("website", NextAction.QUALIFY, sales)
    assert "מי צריך להיות מעורב" in reply
