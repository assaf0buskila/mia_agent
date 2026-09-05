"""Shadow mode: skip prospect outbound send; persist proposed reply for audit."""

from app.core.config import AutomationMode
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.conversation_scope import AutomationScope
from app.domain.events import Channel
from app.domain.policies.execution_policy import POLICY_VERSION
from app.domain.sales import NextAction

MAX_PROPOSED_REPLY = 4000


def should_skip_prospect_send(
    mode: AutomationMode,
    actor_role: str,
    *,
    channel: str = "",
    automation_scope: str = "",
    whatsapp_handoff_send: bool = False,
    whatsapp_require_business_scope: bool = False,
) -> bool:
    if actor_role != "prospect":
        return False
    # Production (business-scope on): WhatsApp stays silent until Assaf flips
    # MIA_WHATSAPP_HANDOFF_SEND after official Cloud API inbound works.
    # Tests that disable the scope gate still use WhatsApp as a sales transport.
    if (
        channel == Channel.WHATSAPP.value
        and whatsapp_require_business_scope
        and not whatsapp_handoff_send
    ):
        return True
    if mode != AutomationMode.SHADOW:
        return False
    if (
        whatsapp_handoff_send
        and channel == Channel.WHATSAPP.value
        and automation_scope == AutomationScope.MIA_BUSINESS.value
    ):
        return False
    return True


def _valid_next_action(value: str) -> bool:
    try:
        NextAction(value)
    except ValueError:
        return False
    return True


def _valid_channel(value: str) -> bool:
    try:
        Channel(value)
    except ValueError:
        return False
    return True


def persist_shadow_decision(
    store,
    *,
    run_id: str,
    lead_id: str | None,
    channel: str,
    next_action: str,
    proposed_reply: str,
) -> None:
    if not run_id:
        return
    if not _valid_next_action(next_action):
        return
    if not _valid_channel(channel):
        return
    try:
        assert_allowed(
            RiskAction(name="shadow_decision_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=False,
        )
    except PolicyDenied:
        return
    store.save_shadow_decision(
        run_id=run_id,
        lead_id=lead_id,
        channel=channel,
        next_action=next_action,
        proposed_reply=proposed_reply[:MAX_PROPOSED_REPLY],
        policy_version=POLICY_VERSION,
    )
