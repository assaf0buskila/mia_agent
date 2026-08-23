import importlib
import inspect

import pytest
from app.core.capabilities import CapabilityId
from app.domain.policies.decision import (
    DETERMINISTIC_NBA_CONFIDENCE,
    AgentDecision,
    DecisionRoute,
    decision_from_sales,
    risk_gate,
    route_decision,
)
from app.domain.policies.execution_policy import policy_for
from pydantic import ValidationError


def test_offer_meeting_routes_execute() -> None:
    decision = decision_from_sales(action="offer_meeting", reply="בוא נקבע פגישה")
    policy = policy_for(CapabilityId.SALES_STATE)
    assert route_decision(decision, policy) == DecisionRoute.EXECUTE
    assert decision.confidence == DETERMINISTIC_NBA_CONFIDENCE
    assert decision.requires_human is False


def test_handoff_owner_required_routes_human_review_on_sales_state() -> None:
    decision = decision_from_sales(action="handoff", reply="", owner_required=True)
    policy = policy_for(CapabilityId.SALES_STATE)
    assert route_decision(decision, policy) == DecisionRoute.HUMAN_REVIEW


def test_handoff_on_approvals_policy_routes_human_handoff() -> None:
    decision = decision_from_sales(action="handoff", reply="", owner_required=True)
    policy = policy_for(CapabilityId.APPROVALS)
    assert route_decision(decision, policy) == DecisionRoute.HUMAN_HANDOFF


def test_low_confidence_identity_routes_human_review() -> None:
    decision = AgentDecision(action="identify", confidence=0.4)
    policy = policy_for(CapabilityId.IDENTITY)
    assert route_decision(decision, policy) == DecisionRoute.HUMAN_REVIEW


def test_low_confidence_sales_reply_routes_execute() -> None:
    decision = AgentDecision(action="reply", confidence=0.4)
    policy = policy_for(CapabilityId.SALES_REPLY)
    assert route_decision(decision, policy) == DecisionRoute.EXECUTE


def test_decision_approval_required_routes_approval() -> None:
    decision = AgentDecision(
        action="proposal",
        confidence=1.0,
        approval_required=True,
        requires_human=False,
    )
    policy = policy_for(CapabilityId.SALES_STATE)
    assert route_decision(decision, policy) == DecisionRoute.APPROVAL


def test_needs_clarification_routes_ask_clarification() -> None:
    decision = AgentDecision(
        action="unknown",
        confidence=1.0,
        next_state="needs_clarification",
        uncertainty_reasons=["ambiguous_intent"],
    )
    policy = policy_for(CapabilityId.SALES_STATE)
    assert route_decision(decision, policy) == DecisionRoute.ASK_CLARIFICATION


def test_risk_gate_is_route_decision() -> None:
    assert risk_gate is route_decision


def test_decision_module_has_no_forbidden_imports() -> None:
    source = inspect.getsource(importlib.import_module("app.domain.policies.decision"))
    forbidden = ("app.graph", "MessagePort", "select_next_action")
    for token in forbidden:
        assert token not in source


def test_decision_from_sales_pins_deterministic_confidence() -> None:
    decision = decision_from_sales(action="offer_meeting", reply="test")
    assert decision.confidence == DETERMINISTIC_NBA_CONFIDENCE


def test_empty_customer_message_becomes_none() -> None:
    decision = decision_from_sales(action="offer_meeting", reply="")
    assert decision.customer_message is None


def test_evidence_ids_rejects_oversized_junk_blob() -> None:
    junk = "x" * 200
    with pytest.raises(ValidationError):
        AgentDecision(action="offer_meeting", confidence=1.0, evidence_ids=[junk])
