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
