import importlib
import inspect

from app.core.capabilities import CapabilityId, require_alive
from app.core.risk import PolicyDecision, RiskAction, RiskLevel, decide
from app.domain.policies.failure_policy import failure_policy_for
from app.domain.tools import ALLOWLISTED_TOOLS


def test_every_allowlisted_tool_has_policy_fail_closed() -> None:
    for tool in sorted(ALLOWLISTED_TOOLS):
        policy = failure_policy_for(tool)
        assert policy.node == tool
        assert policy.fail_closed is True


def test_sales_reply_retries_one_fallback_canned() -> None:
    policy = failure_policy_for("sales_reply")
    assert policy.maximum_retries == 1
    assert policy.fallback == "canned"
    assert policy.fail_closed is True


def test_voice_transcribe_retries_one_fallback_empty() -> None:
    policy = failure_policy_for("voice_transcribe")
    assert policy.maximum_retries == 1
    assert policy.fallback == "empty"
    assert policy.fail_closed is True


def test_meta_ads_insights_fallback_omit_retries_zero() -> None:
    policy = failure_policy_for("meta_ads_insights")
    assert policy.fallback == "omit"
    assert policy.maximum_retries == 0
    assert policy.fail_closed is True


def test_calendar_create_fallback_deny() -> None:
    policy = failure_policy_for("calendar_create")
    assert policy.fallback == "deny"
    assert policy.fail_closed is True


def test_unknown_browser_crawl_fail_closed_omit_no_retries() -> None:
    policy = failure_policy_for("browser_crawl")
    assert policy.fail_closed is True
    assert policy.maximum_retries == 0
    assert policy.timeout_ms == 0
    assert policy.fallback == "omit"
    assert policy.notify_owner is False


def test_meta_write_fail_closed_deny() -> None:
    policy = failure_policy_for("meta_write")
    assert policy.fail_closed is True
    assert policy.fallback == "deny"
    assert policy.maximum_retries == 0
    assert policy.timeout_ms == 0


def test_failure_policy_module_has_no_forbidden_imports() -> None:
    source = inspect.getsource(importlib.import_module("app.domain.policies.failure_policy"))
    forbidden = ("app.graph", "MessagePort", "select_next_action")
    for token in forbidden:
        assert token not in source


def test_fde_failure_policy_capability_alive() -> None:
    require_alive(CapabilityId.FDE_FAILURE_POLICY)


def test_risk_policy_unchanged_r4_approval_r5_deny() -> None:
    assert decide(RiskAction(name="x", risk=RiskLevel.R4_FINANCIAL_MARKETING)) == (
        PolicyDecision.APPROVAL
    )
    assert decide(RiskAction(name="x", risk=RiskLevel.R5_DESTRUCTIVE)) == PolicyDecision.DENY
