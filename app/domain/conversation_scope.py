"""WhatsApp automation eligibility. Deterministic; never from message wording."""

from __future__ import annotations

import re
from enum import StrEnum

from app.domain.approvals import LEAD_ID_RE
from app.domain.events import Channel

SCOPE_SOURCE_WEBSITE_HANDOFF = "website_handoff"
SCOPE_SOURCE_OWNER = "owner"
_PHONE_RE = re.compile(r"\d{8,15}")

MIA_INTRO_HE = "היי, זאת מיה, העוזרת של AssafWeb 🙂 ממשיכים מכאן."


class AutomationScope(StrEnum):
    OWNER = "owner"
    MIA_BUSINESS = "mia_business"
    HUMAN_BUSINESS = "human_business"
    PERSONAL = "personal"
    DO_NOT_AUTOMATE = "do_not_automate"
    UNKNOWN = "unknown"


class TakeoverState(StrEnum):
    MIA_ACTIVE = "mia_active"
    HUMAN_TAKEOVER_REQUIRED = "human_takeover_required"
    HUMAN_ACTIVE = "human_active"
    MIA_PAUSED = "mia_paused"


DENY_SCOPES = frozenset(
    {
        AutomationScope.PERSONAL,
        AutomationScope.DO_NOT_AUTOMATE,
        AutomationScope.HUMAN_BUSINESS,
        AutomationScope.UNKNOWN,
    }
)

TAKEOVER_BLOCKS_SEND = frozenset(
    {
        TakeoverState.HUMAN_TAKEOVER_REQUIRED,
        TakeoverState.HUMAN_ACTIVE,
        TakeoverState.MIA_PAUSED,
    }
)


def takeover_blocks_send(state: str) -> bool:
    return state in {item.value for item in TAKEOVER_BLOCKS_SEND}


def human_takeover_flag(state: str) -> bool:
    return takeover_blocks_send(state)


def extract_digits_id(text: str) -> str | None:
    match = _PHONE_RE.search(text.replace(" ", "").replace("-", ""))
    return match.group(0) if match else None


def extract_lead_id(text: str) -> str | None:
    match = LEAD_ID_RE.search(text)
    return match.group(0) if match else None


def whatsapp_sales_allowed(
    *,
    scope: str,
    require_business_scope: bool,
    fresh_handoff: bool,
) -> bool:
    if scope in {
        AutomationScope.PERSONAL.value,
        AutomationScope.DO_NOT_AUTOMATE.value,
        AutomationScope.HUMAN_BUSINESS.value,
    }:
        return False
    if fresh_handoff or scope == AutomationScope.MIA_BUSINESS.value:
        return True
    if not require_business_scope:
        return True
    return False


def existing_whatsapp_scope(store, external_id: str) -> str:
    row = store.get_conversation_control(Channel.WHATSAPP.value, external_id)
    if row is None:
        return AutomationScope.UNKNOWN.value
    return row.automation_scope


def whatsapp_stt_allowed(
    *,
    from_id: str,
    owner_ids: set[str],
    scope: str,
    require_business_scope: bool,
) -> bool:
    if from_id in owner_ids:
        return True
    return whatsapp_sales_allowed(
        scope=scope,
        require_business_scope=require_business_scope,
        fresh_handoff=False,
    )


def prepare_whatsapp_inbound(
    store,
    *,
    external_id: str,
    text: str,
    require_business_scope: bool,
) -> tuple[bool, str | None, bool, str]:
    """Return (allowed, handoff_lead_id, fresh_handoff, message_text)."""
    from app.domain.handoff import extract_handoff_token, inbound_text_without_token

    existing_scope = existing_whatsapp_scope(store, external_id)
    if existing_scope in {
        AutomationScope.PERSONAL.value,
        AutomationScope.DO_NOT_AUTOMATE.value,
        AutomationScope.HUMAN_BUSINESS.value,
    }:
        return False, None, False, ""
    fresh_handoff = False
    handoff_lead_id: str | None = None
    message_text = text
    extracted = extract_handoff_token(text)
    if extracted is not None:
        raw_token, _remaining = extracted
        message_text = inbound_text_without_token(text)
        handoff_lead_id = store.consume_handoff_token(
            raw_token, whatsapp_external_id=external_id
        )
        if handoff_lead_id is not None:
            fresh_handoff = True
            store.upsert_conversation_control(
                channel=Channel.WHATSAPP.value,
                external_id=external_id,
                automation_scope=AutomationScope.MIA_BUSINESS.value,
                source=SCOPE_SOURCE_WEBSITE_HANDOFF,
                lead_id=handoff_lead_id,
                mia_introduced=False,
            )
    scope = existing_whatsapp_scope(store, external_id)
    allowed = whatsapp_sales_allowed(
        scope=scope,
        require_business_scope=require_business_scope,
        fresh_handoff=fresh_handoff,
    )
    if not allowed:
        return False, None, False, ""
    return True, handoff_lead_id, fresh_handoff, message_text


def prepend_mia_intro(reply: str, *, already_introduced: bool) -> str:
    if already_introduced or not reply.strip():
        return reply
    if MIA_INTRO_HE in reply:
        return reply
    return f"{MIA_INTRO_HE}\n{reply}"


def apply_owner_scope_mark(store, *, text: str, kill_switch: bool) -> str | None:
    """Owner marks a WhatsApp contact personal or do-not-automate. No customer path."""
    from app.core.errors import PolicyDenied
    from app.core.risk import RiskAction, RiskLevel, assert_allowed

    lowered = text.lower()
    if any(
        phrase in lowered
        for phrase in (
            "never automate",
            "do not automate",
            "don't automate",
            "אל תאוטומטי",
        )
    ):
        scope = AutomationScope.DO_NOT_AUTOMATE
        ack = "סומן: בלי אוטומציה."
    elif any(
        phrase in lowered
        for phrase in ("mark this contact personal", "this is personal", "סמן אישי", "זה אישי")
    ):
        scope = AutomationScope.PERSONAL
        ack = "סומן: שיחה אישית. מיה לא מתערבת."
    else:
        return None
    external_id = extract_digits_id(text)
    lead_id = extract_lead_id(text)
    if external_id is None and lead_id is not None:
        external_id = store.whatsapp_external_id_for_lead(lead_id)
    if not external_id:
        return "מה שהבנתי: סימון שיחה. חסר מספר או lead_id. אני לא מבצעת כלום."
    try:
        assert_allowed(
            RiskAction(name="conversation_scope_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return ack
    store.upsert_conversation_control(
        channel=Channel.WHATSAPP.value,
        external_id=external_id,
        automation_scope=scope.value,
        source=SCOPE_SOURCE_OWNER,
        lead_id=lead_id or "",
    )
    return ack
