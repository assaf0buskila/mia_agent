"""Conversation-level kill switch (§34.2): persist opt-out on graph stop, recover otherwise."""

from datetime import UTC, datetime

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.policies.freshness import overlay_stale, stamp_freshness
from app.domain.tools import ToolOutcome


def opt_out_status_outcome(*, present: bool, now: datetime) -> ToolOutcome:
    base_status = "ok" if present else "empty"
    stamp = stamp_freshness(
        "opt_out_status",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="opt_out_status",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=1 if present else 0,
        freshness=stamp.status,
    )


def apply_conversation_kill_policy(
    store, *, lead_id: str, action: str
) -> ToolOutcome | None:
    """Persist or clear leads.conversation_killed. Never sends; swallows PolicyDenied only."""
    try:
        assert_allowed(
            RiskAction(name="conversation_kill_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=False,
        )
    except PolicyDenied:
        return None

    was = store.is_conversation_killed(lead_id)
    action_key = str(action).lower().strip()
    new = action_key == "stop"
    if was == new:
        return None

    store.set_conversation_killed(lead_id, new)
    return opt_out_status_outcome(present=True, now=datetime.now(UTC))
