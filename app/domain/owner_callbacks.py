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

from app.db.store import LeadStore
from app.domain.approvals import (
    ACTION_PROPOSAL_HANDOFF,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
)
from app.integrations.telegram_format import bold, code, join_sections

MAX_TOKEN_LEN = 60

_APPROVED_HEAD = "✅ אושר"
_REJECTED_HEAD = "✖️ בוטל"
_UNKNOWN = "לא מצאתי את הבקשה הזאת. יכול להיות שהיא כבר טופלה."
_ALREADY = "כבר הוכרע קודם. לא שיניתי כלום."


def approval_token(approval_id: str) -> str:
    """Opaque ASCII token for `callback_data`, which is capped at 64 bytes."""
    return approval_id[:MAX_TOKEN_LEN]


def resolve_owner_callback(store: LeadStore, *, decision: str, token: str) -> str:
    """Apply an approve/reject press. Returns the HTML body for the edited message."""
    if decision not in ("approve", "reject"):
        return _UNKNOWN
    row = store.get_approval_by_approval_id(token)
    if row is None:
        return _UNKNOWN
    head = _APPROVED_HEAD if decision == "approve" else _REJECTED_HEAD
    if row.decision != DECISION_PENDING:
        return join_sections(
            bold(_APPROVED_HEAD if row.decision == DECISION_APPROVED else _REJECTED_HEAD),
            _ALREADY,
            f"מזהה: {code(token)}",
        )
    applied = store.decide_approval(
        lead_id=row.lead_id or "",
        action=row.action or ACTION_PROPOSAL_HANDOFF,
        decision=DECISION_APPROVED if decision == "approve" else DECISION_REJECTED,
    )
    if not applied:
        return join_sections(bold("לא הצלחתי להחיל את ההחלטה"), f"מזהה: {code(token)}")
    return join_sections(bold(head), f"מזהה: {code(token)}")
