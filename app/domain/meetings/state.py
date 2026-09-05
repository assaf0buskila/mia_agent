"""Meeting row persistence (§12.2): offered on offer_meeting; booked after explicit confirmation."""

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.events import Channel

STATUS_OFFERED = "offered"
STATUS_BOOKED = "booked"
STATUS_CANCELLATION_REQUESTED = "cancellation_requested"
MEETING_TYPE_INTRO_CALL = "intro_call"
ALLOWLISTED_MEETING_TYPES = frozenset({MEETING_TYPE_INTRO_CALL})
ALLOWLISTED_STATUSES = frozenset(
    {STATUS_OFFERED, STATUS_BOOKED, STATUS_CANCELLATION_REQUESTED}
)
_STATUS_RANK = {
    STATUS_OFFERED: 0,
    STATUS_BOOKED: 1,
    STATUS_CANCELLATION_REQUESTED: 2,
}
_NEXT_ACTION = "offer_meeting"


def apply_meeting_policy(
    store,
    *,
    lead_id: str,
    channel: Channel,
    action: str,
    kill_switch: bool,
) -> None:
    """Persist an offer without downgrading booked or cancellation-requested rows."""
    action_key = str(action).lower().strip()
    if action_key != _NEXT_ACTION:
        return
    if kill_switch:
        return
    try:
        assert_allowed(
            RiskAction(name="meeting_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    existing = store.get_meeting(lead_id)
    if existing is not None and existing.status in {
        STATUS_BOOKED,
        STATUS_CANCELLATION_REQUESTED,
    }:
        return
    store.upsert_meeting_offered(lead_id=lead_id, source=channel.value)
