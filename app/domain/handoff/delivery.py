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


MAX_LEAD_ID_CHARS = 40  # matches OwnerNotificationRecipientClaimRow.lead_id String(40)


def website_ping_scope(session_id: str) -> tuple[str, str]:
    """(lead_id, notification_key) identifying one website conversation's owner ping.

    lead_id is a primary-key column shared with every real owner-flow lead id and
    capped at 40 characters. "site:" (5) plus a canonical UUID4 session id (36, with
    dashes) is 41 - one character over, so every website Telegram ping deterministically
    failed to claim its delivery row with a DataError, on every attempt, forever. The
    dashes carry no entropy, so dropping them (32 hex chars) keeps the key unique per
    session while fitting the column with room to spare.
    """
    compact_session_id = session_id.replace("-", "")
    lead_id = f"{WEBSITE_PING_SCOPE_PREFIX}:{compact_session_id}"
    assert len(lead_id) <= MAX_LEAD_ID_CHARS, (
        f"website ping lead_id is {len(lead_id)} chars, over the {MAX_LEAD_ID_CHARS}-char "
        "column - this used to fail silently as a DataError deep in CRM delivery"
    )
    return lead_id, f"site-ping:{session_id}"
