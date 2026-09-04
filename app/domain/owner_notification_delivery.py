"""Compatibility keys for website-lead owner handoff delivery.

Inbox kinds describe why Assaf was notified. Delivery claims answer the narrower
question "has this website lead already produced an owner handoff ping?" New hot
handoffs and WhatsApp clicks share one recipient key; old rows remain evidence.
"""

KIND_HOT_LEAD_LEGACY = "hot_lead"
KIND_WEBSITE_WHATSAPP_LEGACY = "website_whatsapp_handoff"
KIND_WEBSITE_HANDOFF_DELIVERY = "website_owner_handoff"

WEBSITE_HANDOFF_DELIVERY_KINDS = (
    KIND_WEBSITE_HANDOFF_DELIVERY,
    KIND_HOT_LEAD_LEGACY,
    KIND_WEBSITE_WHATSAPP_LEGACY,
)

# The website never mints a lead (ADR-049), so a website conversation cannot key its
# claim on a lead id. The conversation itself is the scope: two /end calls on one
# session are one logical handoff, and a returning visitor's new session is a new one.
WEBSITE_PING_SCOPE_PREFIX = "site"


def website_ping_scope(session_id: str) -> tuple[str, str]:
    """(lead_id, notification_key) identifying one website conversation's owner ping."""
    return f"{WEBSITE_PING_SCOPE_PREFIX}:{session_id}", f"site-ping:{session_id}"
