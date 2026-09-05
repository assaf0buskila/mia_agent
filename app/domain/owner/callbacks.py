"""Resolve an inline-button decision from the owner's Telegram console.

This is the completion path the console never had. Before, a gated action logged
"נרשם כמשימה. לא ביצעתי אותה." and there was no way to say yes, so every yellow-tier
action was a dead end. The rule itself does not change: the approval still has to exist,
still binds to one approval id, and an unknown or already-decided id is refused.

Telegram can replay a callback against a message whose buttons are gone, so every path
here is idempotent: deciding an already-decided approval reports the existing state rather
than applying a second time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.db.store import LeadStore
from app.domain.approvals import (
    ACTION_CALENDAR_CREATE,
    ACTION_CALENDAR_RESCHEDULE,
    ACTION_COMPOSIO_WRITE,
    ACTION_GMAIL_SEND,
    ACTION_LINKEDIN_COMPOSIO_WRITE,
    ACTION_PROPOSAL_HANDOFF,
    ACTION_WEBSITE_EDIT,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    RESOURCE_CALENDAR,
    RESOURCE_COMPOSIO_TOOL,
    RESOURCE_GMAIL,
    RESOURCE_LEAD,
    RESOURCE_LINKEDIN_TOOL,
    RESOURCE_WEBSITE,
    RISK_R3,
    WEBSITE_RESOURCE_ID,
    is_approval_expired,
    validate_pending_approval_binding,
    website_resource_hash_matches,
)
from app.integrations.telegram_format import bold, code, join_sections

MAX_TOKEN_LEN = 60

_APPROVED_HEAD = "✅ אושר"
_REJECTED_HEAD = "✖️ בוטל"
_UNKNOWN = "לא מצאתי את הבקשה הזאת. יכול להיות שהיא כבר טופלה."
_ALREADY = "כבר הוכרע קודם. לא שיניתי כלום."


@dataclass(frozen=True)
class OwnerCallbackResolution:
    text: str
    gmail_draft_id_to_send: str | None = None
    calendar_resource_id_to_execute: str | None = None
    linkedin_resource_id_to_execute: str | None = None
    composio_resource_id_to_execute: str | None = None


def approval_token(approval_id: str) -> str:
    """Opaque ASCII token for `callback_data`, which is capped at 64 bytes."""
    return approval_id[:MAX_TOKEN_LEN]


def _callback_binding_is_valid(row) -> bool:
    now = datetime.now(UTC)
    if row.action == ACTION_PROPOSAL_HANDOFF:
        return (
            validate_pending_approval_binding(
                row,
                now=now,
                action=ACTION_PROPOSAL_HANDOFF,
                risk=RISK_R3,
                resource_type=RESOURCE_LEAD,
                resource_id=row.lead_id or "",
            )
            is None
        )
    if row.action == ACTION_GMAIL_SEND:
        return (
            validate_pending_approval_binding(
                row,
                now=now,
                action=ACTION_GMAIL_SEND,
                risk=RISK_R3,
                resource_type=RESOURCE_GMAIL,
                resource_id=row.resource_id,
            )
            is None
        )
    if row.action in (ACTION_CALENDAR_CREATE, ACTION_CALENDAR_RESCHEDULE):
        # Calendar changes carry a parameter-bound hash, validated by their
        # executor immediately before the provider call.
        from app.domain.owner.calendar_writes import _row_change

        return (
            row.resource_type == RESOURCE_CALENDAR
            and not is_approval_expired(row, now=now)
            and _row_change(row) is not None
        )
    if row.action == ACTION_WEBSITE_EDIT:
        return (
            validate_pending_approval_binding(
                row,
                now=now,
                action=ACTION_WEBSITE_EDIT,
                risk=RISK_R3,
                resource_type=RESOURCE_WEBSITE,
                resource_id=WEBSITE_RESOURCE_ID,
                hash_matches=website_resource_hash_matches,
            )
            is None
        )
    if row.action == ACTION_LINKEDIN_COMPOSIO_WRITE:
        from app.domain.owner.linkedin_writes import linkedin_row_valid

        return (
            row.resource_type == RESOURCE_LINKEDIN_TOOL
            and not is_approval_expired(row, now=now)
            and linkedin_row_valid(row) is not None
        )
    if row.action == ACTION_COMPOSIO_WRITE:
        from app.domain.owner.composio_writes import composio_row_valid

        return (
            row.resource_type == RESOURCE_COMPOSIO_TOOL
            and not is_approval_expired(row, now=now)
            and composio_row_valid(row) is not None
        )
    return False


def _apply_callback_decision(store: LeadStore, row, *, decision: str) -> bool:
    if row.action == ACTION_PROPOSAL_HANDOFF:
        return store.decide_approval(
            lead_id=row.lead_id or "",
            action=ACTION_PROPOSAL_HANDOFF,
            decision=decision,
        )
    if row.action == ACTION_GMAIL_SEND:
        return store.decide_gmail_approval(resource_id=row.resource_id, decision=decision)
    if row.action in (ACTION_CALENDAR_CREATE, ACTION_CALENDAR_RESCHEDULE):
        return store.decide_calendar_approval(
            resource_id=row.resource_id, action=row.action, decision=decision
        )
    if row.action == ACTION_WEBSITE_EDIT:
        return store.decide_website_approval(resource_id=row.resource_id, decision=decision)
    if row.action == ACTION_LINKEDIN_COMPOSIO_WRITE:
        return store.decide_linkedin_approval(resource_id=row.resource_id, decision=decision)
    if row.action == ACTION_COMPOSIO_WRITE:
        return store.decide_composio_approval(resource_id=row.resource_id, decision=decision)
    return False


def resolve_owner_callback_result(
    store: LeadStore, *, decision: str, token: str
) -> OwnerCallbackResolution:
    """Apply one callback decision and expose a valid approved Gmail draft structurally."""
    if decision not in ("approve", "reject"):
        return OwnerCallbackResolution(_UNKNOWN)
    row = store.get_approval_by_approval_id(token)
    if row is None:
        return OwnerCallbackResolution(_UNKNOWN)
    head = _APPROVED_HEAD if decision == "approve" else _REJECTED_HEAD
    if row.decision != DECISION_PENDING:
        if row.action == ACTION_GMAIL_SEND and row.decision == DECISION_APPROVED:
            if not _callback_binding_is_valid(row):
                return OwnerCallbackResolution(
                    join_sections(
                        bold("האישור אינו תקף"),
                        _UNKNOWN,
                        f"מזהה: {code(token)}",
                    )
                )
            return OwnerCallbackResolution(
                join_sections(bold(_APPROVED_HEAD), _ALREADY, f"מזהה: {code(token)}"),
                gmail_draft_id_to_send=row.resource_id,
            )
        if (
            row.action in (ACTION_CALENDAR_CREATE, ACTION_CALENDAR_RESCHEDULE)
            and row.decision == DECISION_APPROVED
        ):
            if not _callback_binding_is_valid(row):
                return OwnerCallbackResolution(
                    join_sections(bold("האישור אינו תקף"), _UNKNOWN, f"מזהה: {code(token)}")
                )
            return OwnerCallbackResolution(
                join_sections(bold(_APPROVED_HEAD), _ALREADY, f"מזהה: {code(token)}"),
                calendar_resource_id_to_execute=row.resource_id,
            )
        if row.action == ACTION_LINKEDIN_COMPOSIO_WRITE and row.decision == DECISION_APPROVED:
            if not _callback_binding_is_valid(row):
                return OwnerCallbackResolution(
                    join_sections(bold("האישור אינו תקף"), _UNKNOWN, f"מזהה: {code(token)}")
                )
            return OwnerCallbackResolution(
                join_sections(bold(_APPROVED_HEAD), _ALREADY, f"מזהה: {code(token)}"),
                linkedin_resource_id_to_execute=row.resource_id,
            )
        if row.action == ACTION_COMPOSIO_WRITE and row.decision == DECISION_APPROVED:
            if not _callback_binding_is_valid(row):
                return OwnerCallbackResolution(
                    join_sections(bold("האישור אינו תקף"), _UNKNOWN, f"מזהה: {code(token)}")
                )
            return OwnerCallbackResolution(
                join_sections(bold(_APPROVED_HEAD), _ALREADY, f"מזהה: {code(token)}"),
                composio_resource_id_to_execute=row.resource_id,
            )
        return OwnerCallbackResolution(
            join_sections(
                bold(_APPROVED_HEAD if row.decision == DECISION_APPROVED else _REJECTED_HEAD),
                _ALREADY,
                f"מזהה: {code(token)}",
            )
        )
    if not _callback_binding_is_valid(row):
        return OwnerCallbackResolution(
            join_sections(bold("האישור אינו תקף"), _UNKNOWN, f"מזהה: {code(token)}")
        )
    target_decision = DECISION_APPROVED if decision == "approve" else DECISION_REJECTED
    if not _apply_callback_decision(store, row, decision=target_decision):
        return OwnerCallbackResolution(
            join_sections(bold("לא הצלחתי להחיל את ההחלטה"), f"מזהה: {code(token)}")
        )
    return OwnerCallbackResolution(
        join_sections(bold(head), f"מזהה: {code(token)}"),
        gmail_draft_id_to_send=(
            row.resource_id
            if row.action == ACTION_GMAIL_SEND and target_decision == DECISION_APPROVED
            else None
        ),
        calendar_resource_id_to_execute=(
            row.resource_id
            if row.action in (ACTION_CALENDAR_CREATE, ACTION_CALENDAR_RESCHEDULE)
            and target_decision == DECISION_APPROVED
            else None
        ),
        linkedin_resource_id_to_execute=(
            row.resource_id
            if row.action == ACTION_LINKEDIN_COMPOSIO_WRITE and target_decision == DECISION_APPROVED
            else None
        ),
        composio_resource_id_to_execute=(
            row.resource_id
            if row.action == ACTION_COMPOSIO_WRITE and target_decision == DECISION_APPROVED
            else None
        ),
    )


def resolve_owner_callback(store: LeadStore, *, decision: str, token: str) -> str:
    """Compatibility text wrapper for direct callback render callers."""
    return resolve_owner_callback_result(store, decision=decision, token=token).text
