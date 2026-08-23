from app.core.config import AutomationMode
from app.core.risk import PolicyDecision, RiskAction, RiskLevel, assert_allowed
from app.domain.shadow import should_skip_prospect_send
from app.integrations.base import MessagePort, OutboundMessage


async def send_inbound_reply(
    *,
    port: MessagePort,
    message: OutboundMessage,
    kill_switch: bool,
    automation_mode: AutomationMode,
    actor_role: str = "prospect",
    lead_id: str | None = None,
    store=None,
    automation_scope: str = "",
    whatsapp_handoff_send: bool = False,
    auto_reply_instagram: bool = False,
    whatsapp_require_business_scope: bool = False,
) -> bool:
    """Send a customer reply after R2 policy check. Returns True if the port sent."""
    if should_skip_prospect_send(
        automation_mode,
        actor_role,
        channel=message.channel,
        automation_scope=automation_scope,
        whatsapp_handoff_send=whatsapp_handoff_send,
        auto_reply_instagram=auto_reply_instagram,
        whatsapp_require_business_scope=whatsapp_require_business_scope,
    ):
        return False
    if (
        actor_role == "prospect"
        and store is not None
        and lead_id is not None
        and store.is_human_takeover(lead_id)
    ):
        return False
    action = RiskAction(
        name=f"{message.channel}_reply",
        risk=RiskLevel.R2_CUSTOMER_MESSAGE,
        in_approved_scope=True,
    )
    decision = assert_allowed(action, kill_switch=kill_switch)
    if decision != PolicyDecision.AUTO:
        return False
    try:
        await port.send(message)
    except RuntimeError:
        return False
    return True
