import importlib
import inspect

from app.core.capabilities import CAPABILITIES, CapabilityId, require_alive
from app.core.risk import PolicyDecision, RiskAction, RiskLevel, decide
from app.domain.policies.execution_policy import ExecutionMode, policy_for


def test_every_capability_id_has_policy() -> None:
    for cap in CAPABILITIES:
        policy = policy_for(cap.id)
        assert policy.capability == cap.id.value


def test_identity_is_deterministic() -> None:
    policy = policy_for(CapabilityId.IDENTITY)
    assert policy.execution_mode == ExecutionMode.DETERMINISTIC
    assert policy.minimum_confidence == 1.0


def test_sales_state_and_langgraph_are_deterministic() -> None:
    assert policy_for(CapabilityId.SALES_STATE).execution_mode == ExecutionMode.DETERMINISTIC
    assert policy_for(CapabilityId.LANGGRAPH).execution_mode == ExecutionMode.DETERMINISTIC


def test_sales_reply_is_ai_automatic_fail_closed() -> None:
    policy = policy_for(CapabilityId.SALES_REPLY)
    assert policy.execution_mode == ExecutionMode.AI_AUTOMATIC
    assert policy.fail_closed is True


def test_owner_reply_is_ai_automatic_fail_closed() -> None:
    policy = policy_for(CapabilityId.OWNER_REPLY)
    assert policy.execution_mode == ExecutionMode.AI_AUTOMATIC
    assert policy.fail_closed is True
    assert policy.risk == RiskLevel.R1_LOW_WRITE


def test_approvals_is_human_only_with_approval() -> None:
    policy = policy_for(CapabilityId.APPROVALS)
    assert policy.execution_mode == ExecutionMode.HUMAN_ONLY
    assert policy.approval_required is True


def test_meta_ads_is_deterministic() -> None:
    policy = policy_for(CapabilityId.META_ADS)
    assert policy.execution_mode == ExecutionMode.DETERMINISTIC
    assert policy.risk == RiskLevel.R0_READ


def test_aws_runtime_is_human_only() -> None:
    policy = policy_for(CapabilityId.AWS_RUNTIME)
    assert policy.execution_mode == ExecutionMode.HUMAN_ONLY


def test_unknown_capabilities_fail_closed_human_only_r5() -> None:
    for unknown in ("meta_write", "delete_lead"):
        policy = policy_for(unknown)
        assert policy.execution_mode == ExecutionMode.HUMAN_ONLY
        assert policy.risk == RiskLevel.R5_DESTRUCTIVE
        assert policy.fail_closed is True
        assert policy.approval_required is True
        assert policy.minimum_confidence == 1.0


def test_all_policies_fail_closed() -> None:
    for cap in CAPABILITIES:
        assert policy_for(cap.id).fail_closed is True


def test_deterministic_policies_use_full_confidence() -> None:
    for cap in CAPABILITIES:
        policy = policy_for(cap.id)
        if policy.execution_mode == ExecutionMode.DETERMINISTIC:
            assert policy.minimum_confidence == 1.0


def test_no_registry_row_uses_ai_with_review() -> None:
    for cap in CAPABILITIES:
        assert policy_for(cap.id).execution_mode != ExecutionMode.AI_WITH_REVIEW


def test_risk_policy_unchanged_r4_approval_r5_deny() -> None:
    assert decide(RiskAction(name="x", risk=RiskLevel.R4_FINANCIAL_MARKETING)) == (
        PolicyDecision.APPROVAL
    )
    assert decide(RiskAction(name="x", risk=RiskLevel.R5_DESTRUCTIVE)) == PolicyDecision.DENY


def test_execution_policy_module_has_no_forbidden_imports() -> None:
    source = inspect.getsource(importlib.import_module("app.domain.policies.execution_policy"))
    forbidden = ("app.graph", "MessagePort", "select_next_action")
    for token in forbidden:
        assert token not in source


def test_fde_execution_policy_capability_alive() -> None:
    require_alive(CapabilityId.FDE_EXECUTION_POLICY)


def test_registry_covers_all_capability_ids() -> None:
    registry_keys = {
        policy_for(cap).capability for cap in CapabilityId
    }
    assert registry_keys == {cap.value for cap in CapabilityId}
