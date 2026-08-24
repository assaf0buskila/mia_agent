"""Per-capability execution mode registry. Lookup only; does not gate writes."""

from enum import StrEnum

from pydantic import BaseModel, Field

from app.core.capabilities import CapabilityId
from app.core.risk import RiskLevel

POLICY_VERSION = "fde_v1"


class ExecutionMode(StrEnum):
    DETERMINISTIC = "deterministic"
    AI_AUTOMATIC = "ai_automatic"
    AI_WITH_REVIEW = "ai_with_review"
    HUMAN_ONLY = "human_only"


class ActionPolicy(BaseModel):
    capability: str
    execution_mode: ExecutionMode
    minimum_confidence: float = Field(ge=0.0, le=1.0)
    approval_required: bool = False
    fail_closed: bool = True
    maximum_retries: int = Field(default=2, ge=0, le=5)
    risk: RiskLevel


def _det(
    cap: CapabilityId,
    *,
    risk: RiskLevel,
    approval_required: bool = False,
    minimum_confidence: float = 1.0,
) -> ActionPolicy:
    return ActionPolicy(
        capability=cap.value,
        execution_mode=ExecutionMode.DETERMINISTIC,
        minimum_confidence=minimum_confidence,
        approval_required=approval_required,
        fail_closed=True,
        risk=risk,
    )


def _ai_auto(cap: CapabilityId, *, risk: RiskLevel) -> ActionPolicy:
    return ActionPolicy(
        capability=cap.value,
        execution_mode=ExecutionMode.AI_AUTOMATIC,
        minimum_confidence=0.0,
        approval_required=False,
        fail_closed=True,
        risk=risk,
    )


def _human(
    cap: CapabilityId,
    *,
    risk: RiskLevel,
    approval_required: bool = True,
) -> ActionPolicy:
    return ActionPolicy(
        capability=cap.value,
        execution_mode=ExecutionMode.HUMAN_ONLY,
        minimum_confidence=1.0,
        approval_required=approval_required,
        fail_closed=True,
        risk=risk,
    )


_REGISTRY: dict[str, ActionPolicy] = {
    CapabilityId.HTTP_API.value: _det(CapabilityId.HTTP_API, risk=RiskLevel.R0_READ),
    CapabilityId.CONFIG.value: _det(CapabilityId.CONFIG, risk=RiskLevel.R0_READ),
    CapabilityId.CAPABILITY_REGISTRY.value: _det(
        CapabilityId.CAPABILITY_REGISTRY, risk=RiskLevel.R0_READ
    ),
    CapabilityId.OBSERVABILITY.value: _det(
        CapabilityId.OBSERVABILITY, risk=RiskLevel.R0_READ
    ),
    CapabilityId.RISK_POLICY.value: _det(CapabilityId.RISK_POLICY, risk=RiskLevel.R0_READ),
    CapabilityId.CANONICAL_EVENTS.value: _det(
        CapabilityId.CANONICAL_EVENTS, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.IDENTITY.value: _det(CapabilityId.IDENTITY, risk=RiskLevel.R1_LOW_WRITE),
    CapabilityId.SALES_STATE.value: _det(CapabilityId.SALES_STATE, risk=RiskLevel.R0_READ),
    CapabilityId.SALES_REPLY.value: _ai_auto(
        CapabilityId.SALES_REPLY, risk=RiskLevel.R2_CUSTOMER_MESSAGE
    ),
    CapabilityId.OWNER_REPLY.value: _ai_auto(
        CapabilityId.OWNER_REPLY, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.HUMANITY_LINTER.value: _det(
        CapabilityId.HUMANITY_LINTER, risk=RiskLevel.R0_READ
    ),
    CapabilityId.LANGGRAPH.value: _det(CapabilityId.LANGGRAPH, risk=RiskLevel.R0_READ),
    CapabilityId.WEBSITE.value: _det(CapabilityId.WEBSITE, risk=RiskLevel.R1_LOW_WRITE),
    CapabilityId.WHATSAPP.value: _det(CapabilityId.WHATSAPP, risk=RiskLevel.R1_LOW_WRITE),
    CapabilityId.TELEGRAM.value: _det(CapabilityId.TELEGRAM, risk=RiskLevel.R1_LOW_WRITE),
    CapabilityId.VOICE_STT.value: _ai_auto(CapabilityId.VOICE_STT, risk=RiskLevel.R0_READ),
    CapabilityId.INSTAGRAM.value: _det(CapabilityId.INSTAGRAM, risk=RiskLevel.R1_LOW_WRITE),
    CapabilityId.MANYCHAT.value: _det(CapabilityId.MANYCHAT, risk=RiskLevel.R1_LOW_WRITE),
    CapabilityId.GMAIL.value: _det(CapabilityId.GMAIL, risk=RiskLevel.R0_READ),
    CapabilityId.GMAIL_SUMMARY.value: _ai_auto(
        CapabilityId.GMAIL_SUMMARY, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.GMAIL_INBOX.value: _det(CapabilityId.GMAIL_INBOX, risk=RiskLevel.R0_READ),
    CapabilityId.CALENDAR.value: _det(
        CapabilityId.CALENDAR, risk=RiskLevel.R2_CUSTOMER_MESSAGE
    ),
    CapabilityId.SHEETS_MIRROR.value: _det(
        CapabilityId.SHEETS_MIRROR, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.META_ADS.value: _det(CapabilityId.META_ADS, risk=RiskLevel.R0_READ),
    CapabilityId.CONTENT_PERFORMANCE.value: _det(
        CapabilityId.CONTENT_PERFORMANCE, risk=RiskLevel.R0_READ
    ),
    CapabilityId.CONTENT_IDEAS.value: _det(
        CapabilityId.CONTENT_IDEAS, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.CAMPAIGN_ANALYSIS.value: _det(
        CapabilityId.CAMPAIGN_ANALYSIS, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.CAMPAIGN_PACING.value: _det(
        CapabilityId.CAMPAIGN_PACING, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.CAMPAIGN_PRELAUNCH.value: _det(
        CapabilityId.CAMPAIGN_PRELAUNCH, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.RESEARCH.value: _det(CapabilityId.RESEARCH, risk=RiskLevel.R0_READ),
    CapabilityId.LINKEDIN.value: _det(CapabilityId.LINKEDIN, risk=RiskLevel.R0_READ),
    CapabilityId.OWNER_LEARNING.value: _det(
        CapabilityId.OWNER_LEARNING, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.OWNER_BRIEF.value: _det(
        CapabilityId.OWNER_BRIEF, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.OWNER_WEEKLY.value: _det(
        CapabilityId.OWNER_WEEKLY, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.GRAPH_LAB.value: _det(CapabilityId.GRAPH_LAB, risk=RiskLevel.R0_READ),
    CapabilityId.DEMO_MODE.value: _det(CapabilityId.DEMO_MODE, risk=RiskLevel.R0_READ),
    CapabilityId.FOLLOW_UP.value: _det(CapabilityId.FOLLOW_UP, risk=RiskLevel.R1_LOW_WRITE),
    CapabilityId.DUE_SCAN.value: _det(CapabilityId.DUE_SCAN, risk=RiskLevel.R1_LOW_WRITE),
    CapabilityId.RECONCILIATION.value: _det(
        CapabilityId.RECONCILIATION, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.CONVERSATION_KILL.value: _det(
        CapabilityId.CONVERSATION_KILL, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.MEETING_BRIEF.value: _det(
        CapabilityId.MEETING_BRIEF, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.MEETING_DEBRIEF.value: _det(
        CapabilityId.MEETING_DEBRIEF, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.LEAD_REVIEW.value: _det(
        CapabilityId.LEAD_REVIEW, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.OWNER_CALENDAR.value: _det(
        CapabilityId.OWNER_CALENDAR, risk=RiskLevel.R0_READ
    ),
    CapabilityId.OWNER_NOTIFY.value: _det(
        CapabilityId.OWNER_NOTIFY, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.MEETINGS.value: _det(CapabilityId.MEETINGS, risk=RiskLevel.R1_LOW_WRITE),
    CapabilityId.APPROVALS.value: _human(
        CapabilityId.APPROVALS,
        risk=RiskLevel.R3_COMMERCIAL,
        approval_required=True,
    ),
    CapabilityId.DEALS.value: _det(CapabilityId.DEALS, risk=RiskLevel.R1_LOW_WRITE),
    CapabilityId.AI_RUNS.value: _det(CapabilityId.AI_RUNS, risk=RiskLevel.R1_LOW_WRITE),
    CapabilityId.TOOL_RUNS.value: _det(CapabilityId.TOOL_RUNS, risk=RiskLevel.R1_LOW_WRITE),
    CapabilityId.AWS_RUNTIME.value: _human(
        CapabilityId.AWS_RUNTIME,
        risk=RiskLevel.R5_DESTRUCTIVE,
        approval_required=True,
    ),
    CapabilityId.FDE_EXECUTION_POLICY.value: _det(
        CapabilityId.FDE_EXECUTION_POLICY, risk=RiskLevel.R0_READ
    ),
    CapabilityId.FDE_SHADOW.value: _det(
        CapabilityId.FDE_SHADOW, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.FDE_FEEDBACK.value: _det(
        CapabilityId.FDE_FEEDBACK, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.FDE_VALUE.value: _det(
        CapabilityId.FDE_VALUE, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.FDE_FAILURE_POLICY.value: _det(
        CapabilityId.FDE_FAILURE_POLICY, risk=RiskLevel.R0_READ
    ),
    CapabilityId.FDE_HUMAN_TAKEOVER.value: _det(
        CapabilityId.FDE_HUMAN_TAKEOVER, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.FDE_IDEMPOTENCY.value: _det(
        CapabilityId.FDE_IDEMPOTENCY, risk=RiskLevel.R1_LOW_WRITE
    ),
    CapabilityId.PRELOADED_TOOLS.value: _det(
        CapabilityId.PRELOADED_TOOLS, risk=RiskLevel.R0_READ
    ),
    CapabilityId.MODEL_TASK_CLASSES.value: _det(
        CapabilityId.MODEL_TASK_CLASSES, risk=RiskLevel.R0_READ
    ),
    CapabilityId.FRESHNESS_POLICY.value: _det(
        CapabilityId.FRESHNESS_POLICY, risk=RiskLevel.R0_READ
    ),
}


def policy_for(capability: str | CapabilityId) -> ActionPolicy:
    key = capability.value if isinstance(capability, CapabilityId) else capability
    known = _REGISTRY.get(key)
    if known is not None:
        return known.model_copy(deep=True)
    return ActionPolicy(
        capability=key,
        execution_mode=ExecutionMode.HUMAN_ONLY,
        minimum_confidence=1.0,
        approval_required=True,
        fail_closed=True,
        risk=RiskLevel.R5_DESTRUCTIVE,
    )
