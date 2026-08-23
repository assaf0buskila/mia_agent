"""Structured sales decision + route lookup. No graph, no send, no assert_allowed."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from app.domain.policies.execution_policy import ActionPolicy, ExecutionMode

_EVIDENCE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9._:-]+$")
_MAX_EVIDENCE_IDS = 8
_MAX_EVIDENCE_ID_LEN = 128
_MAX_UNCERTAINTY_REASONS = 8
_MAX_UNCERTAINTY_REASON_LEN = 64
_MAX_ACTION_LEN = 64
_MAX_CUSTOMER_MESSAGE_LEN = 4000
_EMPTY_ACTIONS = frozenset({"", "unknown"})
DETERMINISTIC_NBA_CONFIDENCE = 1.0


class DecisionRoute(StrEnum):
    EXECUTE = "execute"
    HUMAN_REVIEW = "human_review"
    APPROVAL = "approval"
    HUMAN_HANDOFF = "human_handoff"
    ASK_CLARIFICATION = "ask_clarification"


class AgentDecision(BaseModel):
    action: str
    customer_message: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)
    uncertainty_reasons: list[str] = Field(default_factory=list)
    requires_human: bool = False
    approval_required: bool = False
    next_state: str | None = None

    @field_validator("action")
    @classmethod
    def _validate_action(cls, value: str) -> str:
        if len(value) > _MAX_ACTION_LEN:
            raise ValueError("action too long")
        return value

    @field_validator("customer_message", mode="before")
    @classmethod
    def _normalize_customer_message(cls, value: str | None) -> str | None:
        if value == "":
            return None
        return value

    @field_validator("customer_message")
    @classmethod
    def _validate_customer_message(cls, value: str | None) -> str | None:
        if value is not None and len(value) > _MAX_CUSTOMER_MESSAGE_LEN:
            raise ValueError("customer_message too long")
        return value

    @field_validator("evidence_ids")
    @classmethod
    def _validate_evidence_ids(cls, value: list[str]) -> list[str]:
        if len(value) > _MAX_EVIDENCE_IDS:
            raise ValueError("evidence_ids max 8")
        for item in value:
            if len(item) > _MAX_EVIDENCE_ID_LEN:
                raise ValueError("evidence_id too long")
            if _EVIDENCE_ID_PATTERN.fullmatch(item) is None:
                raise ValueError("invalid evidence_id")
        return value

    @field_validator("uncertainty_reasons")
    @classmethod
    def _validate_uncertainty_reasons(cls, value: list[str]) -> list[str]:
        if len(value) > _MAX_UNCERTAINTY_REASONS:
            raise ValueError("uncertainty_reasons max 8")
        for item in value:
            if len(item) > _MAX_UNCERTAINTY_REASON_LEN:
                raise ValueError("uncertainty_reason too long")
        return value


def route_decision(decision: AgentDecision, policy: ActionPolicy) -> DecisionRoute:
    """First-match route lookup. Does not call assert_allowed; EXECUTE still needs it later."""
    if policy.execution_mode == ExecutionMode.HUMAN_ONLY:
        return DecisionRoute.HUMAN_HANDOFF
    if decision.requires_human:
        return DecisionRoute.HUMAN_REVIEW
    if decision.confidence < policy.minimum_confidence:
        return DecisionRoute.HUMAN_REVIEW
    if policy.approval_required or decision.approval_required:
        return DecisionRoute.APPROVAL
    if decision.uncertainty_reasons and (
        decision.action in _EMPTY_ACTIONS or decision.next_state == "needs_clarification"
    ):
        return DecisionRoute.ASK_CLARIFICATION
    return DecisionRoute.EXECUTE


risk_gate = route_decision


def decision_from_sales(
    *,
    action: str,
    reply: str,
    run_id: str = "",
    owner_required: bool = False,
    lint_reasons: tuple[str, ...] = (),
) -> AgentDecision:
    """Wrap deterministic NBA + reply into a structured decision (confidence pinned 1.0)."""
    evidence_ids = [run_id] if run_id else []
    uncertainty_reasons = [
        reason[:_MAX_UNCERTAINTY_REASON_LEN]
        for reason in lint_reasons[:_MAX_UNCERTAINTY_REASONS]
    ]
    requires_human = action == "handoff" or owner_required
    approval_required = action == "handoff"
    customer_message = reply if reply else None
    return AgentDecision(
        action=action,
        customer_message=customer_message,
        confidence=DETERMINISTIC_NBA_CONFIDENCE,
        evidence_ids=evidence_ids,
        uncertainty_reasons=uncertainty_reasons,
        requires_human=requires_human,
        approval_required=approval_required,
        next_state=action,
    )
