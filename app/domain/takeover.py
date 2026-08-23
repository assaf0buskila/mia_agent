"""Human takeover: owner parks a conversation; Mia stops prospect outbound."""

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.approvals import LEAD_ID_RE
from app.domain.conversation_scope import TakeoverState

_HUMAN_TAKEOVER_ACK = (
    "תפיסה אנושית אושרה. לא אשלח הודעות לליד הזה."
)
_HUMAN_TAKEOVER_RESUME_ACK = (
    "תפיסה אנושית שוחררה. אחזור לשלוח לליד הזה לפי המדיניות."
)
_UNKNOWN_LEAD_ACK = (
    "מה שהבנתי: תפיסה אנושית. לא מצאתי את הליד. אני לא מבצעת כלום."
)
_UNKNOWN_LEAD_RESUME_ACK = (
    "מה שהבנתי: שחרור תפיסה. לא מצאתי את הליד. אני לא מבצעת כלום."
)


def extract_takeover_lead_id(text: str) -> str | None:
    match = LEAD_ID_RE.search(text)
    return match.group(0) if match else None


def _persist_human_takeover(
    store, *, lead_id: str, kill_switch: bool, enabled: bool
) -> None:
    try:
        assert_allowed(
            RiskAction(name="human_takeover_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    if enabled:
        store.set_takeover_state(lead_id, TakeoverState.HUMAN_ACTIVE.value)
        store.cancel_pending_follow_up(lead_id)
    else:
        store.set_takeover_state(lead_id, TakeoverState.MIA_ACTIVE.value)


def apply_owner_human_takeover(
    store,
    *,
    text: str,
    kill_switch: bool,
) -> str | None:
    """Return Hebrew takeover ack, unknown-lead ack, or None when no lead_id in text."""
    lead_id = extract_takeover_lead_id(text)
    if lead_id is None:
        return None
    if store.get_lead(lead_id) is None:
        return _UNKNOWN_LEAD_ACK
    _persist_human_takeover(
        store, lead_id=lead_id, kill_switch=kill_switch, enabled=True
    )
    return _HUMAN_TAKEOVER_ACK


def apply_owner_human_resume(
    store,
    *,
    text: str,
    kill_switch: bool,
) -> str | None:
    """Return Hebrew resume ack, unknown-lead ack, or None when no lead_id in text."""
    lead_id = extract_takeover_lead_id(text)
    if lead_id is None:
        return None
    if store.get_lead(lead_id) is None:
        return _UNKNOWN_LEAD_RESUME_ACK
    _persist_human_takeover(
        store, lead_id=lead_id, kill_switch=kill_switch, enabled=False
    )
    return _HUMAN_TAKEOVER_RESUME_ACK
