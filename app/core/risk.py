from enum import StrEnum

from pydantic import BaseModel

from app.core.errors import PolicyDenied


class RiskLevel(StrEnum):
    R0_READ = "R0"
    R1_LOW_WRITE = "R1"
    R2_CUSTOMER_MESSAGE = "R2"
    R3_COMMERCIAL = "R3"
    R4_FINANCIAL_MARKETING = "R4"
    R5_DESTRUCTIVE = "R5"


class PolicyDecision(StrEnum):
    AUTO = "auto"
    APPROVAL = "approval"
    DENY = "deny"


class RiskAction(BaseModel):
    name: str
    risk: RiskLevel
    in_approved_scope: bool = False


# Hard-coded Bible §33. Not env-overridable: R4 stays approval, R5 stays deny.
_DEFAULT_POLICY: dict[RiskLevel, PolicyDecision] = {
    RiskLevel.R0_READ: PolicyDecision.AUTO,
    RiskLevel.R1_LOW_WRITE: PolicyDecision.AUTO,
    RiskLevel.R2_CUSTOMER_MESSAGE: PolicyDecision.AUTO,
    RiskLevel.R3_COMMERCIAL: PolicyDecision.APPROVAL,
    RiskLevel.R4_FINANCIAL_MARKETING: PolicyDecision.APPROVAL,
    RiskLevel.R5_DESTRUCTIVE: PolicyDecision.DENY,
}


def decide(action: RiskAction, *, kill_switch: bool = False) -> PolicyDecision:
    if kill_switch:
        raise PolicyDenied("kill switch is on")
    if action.risk == RiskLevel.R2_CUSTOMER_MESSAGE and not action.in_approved_scope:
        return PolicyDecision.APPROVAL
    return _DEFAULT_POLICY[action.risk]


def assert_allowed(action: RiskAction, *, kill_switch: bool = False) -> PolicyDecision:
    decision = decide(action, kill_switch=kill_switch)
    if decision == PolicyDecision.DENY:
        raise PolicyDenied(f"{action.name} is denied")
    return decision
