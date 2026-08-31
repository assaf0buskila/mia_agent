"""Brief Assaf on Telegram when a website visitor clicks through to WhatsApp.

Mia does not answer on WhatsApp until official Cloud API inbound exists.
The customer opens Assaf's chat; this module is how Assaf knows who they are
and what first line is worth sending. No LLM: the paste line is deterministic.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import NamedTuple

from app.core.config import Settings
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.lead_label import lead_display
from app.domain.memory import ROLE_MIA, ConversationTurn, counterpart_turns
from app.domain.owner_notification_delivery import (
    KIND_WEBSITE_HANDOFF_DELIVERY,
    KIND_WEBSITE_WHATSAPP_LEGACY,
    WEBSITE_HANDOFF_DELIVERY_KINDS,
)
from app.domain.sales import PainLevel, SalesState
from app.integrations.telegram_format import blockquote, bold, esc

KIND_WEBSITE_WHATSAPP = KIND_WEBSITE_WHATSAPP_LEGACY
NOTIFICATION_DELIVERED = "delivered"
NOTIFICATION_FAILED = "failed"
NOTIFICATION_DUPLICATE_OR_AMBIGUOUS = "duplicate_or_ambiguous"
_BRIEF_MAX = 1800
_MAX_TURNS = 10
_PROSPECT_WINDOW = 4

_GENERIC_LINE = "היי, ראיתי שהגעתם מהאתר. בואו נמשיך מפה."
_MANUAL_LINE = "היי, דיברתם באתר על שלב שעדיין נעשה ידנית. בואו נמשיך מפה."
_INVENTORY_SHEETS_LINE = "היי, דיברתם על הזנת מלאי לשיטס. בואו נמשיך מפה."
_INVENTORY_LINE = "היי, דיברתם על העבודה עם המלאי. בואו נמשיך מפה."
_SHEETS_LINE = "היי, דיברתם על הזנה ידנית לשיטס. בואו נמשיך מפה."
_WEBSITE_INTENT_LINE = "היי, דיברתם באתר על בניית אתר. בואו נמשיך מפה."
_BUSINESS_INTENT_LINE = "היי, דיברתם באתר שרציתם לפתוח עסק. בואו נמשיך מפה."
_INTENT_LINE = "היי, דיברתם באתר על מה שרציתם לבנות. בואו נמשיך מפה."
_PAIN_LINE = "היי, דיברתם באתר על משהו שתוקע את העסק. בואו נמשיך מפה."

# Allowlisted topic labels only. Never interpolate prospect text into the paste line.
_TOPIC_NEEDLES: tuple[tuple[str, str], ...] = (
    ("מלאי", "מלאי"),
    ("inventory", "מלאי"),
    ("שיטס", "שיטס"),
    ("sheets", "שיטס"),
    ("spreadsheet", "שיטס"),
    ("אקסל", "שיטס"),
    ("excel", "שיטס"),
    ("לבנות אתר", "אתר"),
    ("בניית אתר", "אתר"),
    ("want a website", "אתר"),
    ("build a website", "אתר"),
    ("לפתוח עסק", "עסק"),
    ("open a business", "עסק"),
)

_FORBIDDEN_PASTE = re.compile(
    r"mia1_|roi|₪|\$|€|%|\d{7,}",
    re.IGNORECASE,
)
_FAKE_URGENCY = ("רק היום", "הזדמנות אחרונה", "limited time", "act now")
_MIA_WILL_REPLY = ("מיה תענה", "מיה תחזיר", "מיה תכתוב")


class WebsiteWhatsAppBriefResult(NamedTuple):
    brief: str | None
    notification_status: str
    local_commit_failed: bool = False


def _deliver_owner_brief(*, brief: str, settings: Settings, recipient_ids: tuple[str, ...]):
    """Lazy import avoids the services package's finalization import cycle."""
    from app.services.notifications import deliver_owner_telegram

    return deliver_owner_telegram(
        text=brief,
        settings=settings,
        parse_mode="HTML",
        recipient_ids=recipient_ids,
    )


def _fact_lines(sales: SalesState) -> list[str]:
    lines: list[str] = []
    if sales.workflow_known:
        lines.append("יודעים מה העסק עושה")
    if sales.manual_step_known:
        lines.append("יש שלב ידני ברור")
    if sales.data_source_known:
        lines.append("ידוע מאיפה הנתונים מגיעים")
    if sales.impact_confirmed:
        lines.append(f"כאב P{int(sales.pain_level)}")
    if sales.hypothesis_offered:
        lines.append("הוצעה השערה")
    if sales.explicit_buying_intent:
        lines.append("יש כוונת קנייה מפורשת")
    if not lines:
        lines.append("עדיין בתחילת discovery")
    return lines


def _transcript_lines(turns: list[ConversationTurn]) -> list[str]:
    clipped = turns[-_MAX_TURNS:]
    lines: list[str] = []
    for turn in clipped:
        label = "מיה" if turn.role == ROLE_MIA else "לקוח"
        lines.append(f"{label}: {turn.text}")
    return lines


def _prospect_blob(turns: list[ConversationTurn]) -> str:
    recent = counterpart_turns(turns)[-_PROSPECT_WINDOW:]
    return " ".join(turn.text for turn in recent).lower()


def _topics(blob: str) -> frozenset[str]:
    found: set[str] = set()
    for needle, label in _TOPIC_NEEDLES:
        if needle in blob:
            found.add(label)
    return frozenset(found)


def _line_allowed(line: str) -> bool:
    if _FORBIDDEN_PASTE.search(line):
        return False
    folded = line.lower()
    if any(phrase in folded for phrase in _FAKE_URGENCY):
        return False
    if any(phrase in line for phrase in _MIA_WILL_REPLY):
        return False
    return True


def _manual_line(topics: frozenset[str]) -> str:
    if "מלאי" in topics and "שיטס" in topics:
        return _INVENTORY_SHEETS_LINE
    if "מלאי" in topics:
        return _INVENTORY_LINE
    if "שיטס" in topics:
        return _SHEETS_LINE
    return _MANUAL_LINE


def _intent_line(topics: frozenset[str]) -> str:
    if "אתר" in topics:
        return _WEBSITE_INTENT_LINE
    if "עסק" in topics:
        return _BUSINESS_INTENT_LINE
    return _INTENT_LINE


def _recommended_first_line(
    *, sales: SalesState, turns: list[ConversationTurn]
) -> str:
    """One paste-ready Hebrew line. Built from flags + allowlisted topics only."""
    topics = _topics(_prospect_blob(turns))
    if sales.manual_step_known:
        line = _manual_line(topics)
    elif sales.explicit_buying_intent:
        line = _intent_line(topics)
    elif sales.impact_confirmed or sales.pain_level >= PainLevel.P2:
        line = _PAIN_LINE
    else:
        line = _GENERIC_LINE
    if not _line_allowed(line):
        return _GENERIC_LINE
    return line


def format_website_whatsapp_brief(
    *,
    lead_id: str,
    sales: SalesState,
    turns: list[ConversationTurn],
) -> str:
    """Owner-facing briefing, HTML. No prices, no invented facts, no customer phone.

    Short on purpose. The previous version opened with two lines of preamble, then an
    opaque lead id, then facts one per line, then the whole transcript inline — so the one
    thing Assaf needs (who is this, and what do I send) was buried under everything else.

    Now: who it is on line one, the facts on a single line, the paste line, and the
    transcript collapsed into an expandable quote that costs one tap to open.
    """
    paste = _recommended_first_line(sales=sales, turns=turns)
    headline = (sales.headline or "").strip()
    who = lead_display(lead_id, headline, sales.display_name)
    blocks = [
        f"{bold('ליד מהאתר → וואטסאפ')}",
        esc(who),
        esc("מיה לא תענה שם. תטפל אתה."),
        "",
        f"{bold('מה ידוע')}: " + esc(" · ".join(_fact_lines(sales))),
        "",
        "השורה שלך:",
        esc(paste),
    ]
    transcript = _transcript_lines(turns)
    if transcript:
        # Collapsed by default: the transcript is evidence, not the message.
        blocks.extend(
            ["", "השיחה:", blockquote("\n".join(transcript), expandable=True)]
        )
    return "\n".join(blocks)[:_BRIEF_MAX]


def format_website_human_handoff_brief(
    *,
    lead_id: str,
    sales: SalesState,
    turns: list[ConversationTurn],
) -> str:
    """Owner ping when the website graph hands off. HTML. Includes the conversation.

    This is not a WhatsApp click. Do not tell Assaf the visitor is already in his
    WhatsApp inbox — they are not, until they tap the widget CTA.
    """
    headline = (sales.headline or "").strip()
    who = lead_display(lead_id, headline, sales.display_name)
    blocks = [
        f"{bold('ליד מהאתר — צריך אותך')}",
        esc(who),
        esc("מיה עצרה באתר. תטפל אתה."),
        "",
        f"{bold('מה ידוע')}: " + esc(" · ".join(_fact_lines(sales))),
    ]
    transcript = _transcript_lines(turns)
    if transcript:
        blocks.extend(
            ["", "השיחה:", blockquote("\n".join(transcript), expandable=True)]
        )
    return "\n".join(blocks)[:_BRIEF_MAX]


def apply_website_whatsapp_handoff_brief(
    store,
    *,
    lead_id: str,
    session_id: str,
    settings: Settings,
) -> WebsiteWhatsAppBriefResult:
    """Persist the owner card and deliver once per owner. Never raises to the website."""
    if settings.kill_switch or settings.demo_mode:
        return WebsiteWhatsAppBriefResult(None, NOTIFICATION_FAILED)
    try:
        assert_allowed(
            RiskAction(name="website_whatsapp_brief", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=settings.kill_switch,
        )
    except PolicyDenied:
        return WebsiteWhatsAppBriefResult(None, NOTIFICATION_FAILED)
    sales = store.get_sales(lead_id)
    turns = store.list_conversation_turns(session_id)
    brief = format_website_whatsapp_brief(
        lead_id=lead_id, sales=sales, turns=turns
    )
    # The owner inbox records the local business event. Delivery idempotency is a
    # separate per-recipient ledger: accepted/ambiguous recipients remain claimed,
    # while an explicit Telegram rejection releases only that recipient for retry.
    # Delivery is conversation-scoped. A returning visitor starts a new website session
    # and must produce a new owner alert, while graph handoff and WhatsApp click inside
    # the same session share this key and still deduplicate against each other.
    notification_key = session_id
    now_iso = datetime.now(UTC).replace(microsecond=0).isoformat()
    store.upsert_owner_notification(
        kind=KIND_WEBSITE_WHATSAPP, lead_id=lead_id, scheduled_at=now_iso
    )
    # A pre-recipient deployment could already have sent this lead-scoped brief.
    # Its legacy claim has no recipient/outcome detail, so suppress the whole new
    # fan-out conservatively instead of risking a duplicate owner ping.
    if any(
        store.has_owner_notification_claim(
            kind=kind, lead_id=lead_id, conversation_id=notification_key
        )
        for kind in WEBSITE_HANDOFF_DELIVERY_KINDS
    ):
        return WebsiteWhatsAppBriefResult(
            brief, NOTIFICATION_DUPLICATE_OR_AMBIGUOUS
        )
    token = settings.telegram_bot_token.strip()
    recipients = tuple(sorted(settings.telegram_owner_user_id_set()))
    if not token or not recipients or not brief.strip():
        if store.confirmed_owner_notification_recipients(
            kind=KIND_WEBSITE_HANDOFF_DELIVERY,
            lead_id=lead_id,
            notification_key=notification_key,
        ):
            return WebsiteWhatsAppBriefResult(brief, NOTIFICATION_DELIVERED)
        return WebsiteWhatsAppBriefResult(brief, NOTIFICATION_FAILED)
    claimed_recipients = tuple(
        recipient_id
        for recipient_id in recipients
        if store.try_claim_owner_notification_recipient_compatible(
            kind=KIND_WEBSITE_HANDOFF_DELIVERY,
            compatible_kinds=WEBSITE_HANDOFF_DELIVERY_KINDS,
            lead_id=lead_id,
            notification_key=notification_key,
            recipient_id=recipient_id,
            claimed_at=now_iso,
        )
    )
    if not claimed_recipients:
        if store.confirmed_owner_notification_recipients(
            kind=KIND_WEBSITE_HANDOFF_DELIVERY,
            lead_id=lead_id,
            notification_key=notification_key,
        ):
            return WebsiteWhatsAppBriefResult(brief, NOTIFICATION_DELIVERED)
        return WebsiteWhatsAppBriefResult(
            brief, NOTIFICATION_DUPLICATE_OR_AMBIGUOUS
        )
    # The claim must survive independently of FastAPI's commit-after-yield cleanup.
    # No external Telegram request is allowed unless this local transaction commits.
    if not store.commit_owner_notification_delivery_state():
        return WebsiteWhatsAppBriefResult(brief, NOTIFICATION_FAILED, True)
    delivery = _deliver_owner_brief(
        brief=brief, settings=settings, recipient_ids=claimed_recipients
    )
    outcomes_persisted = store.record_owner_notification_recipient_delivery_outcomes_durably(
        kind=KIND_WEBSITE_HANDOFF_DELIVERY,
        lead_id=lead_id,
        notification_key=notification_key,
        delivered_recipient_ids=delivery.delivered,
        rejected_recipient_ids=delivery.rejected,
    )
    if delivery.delivered or store.confirmed_owner_notification_recipients(
        kind=KIND_WEBSITE_HANDOFF_DELIVERY,
        lead_id=lead_id,
        notification_key=notification_key,
    ):
        status = NOTIFICATION_DELIVERED
    elif delivery.ambiguous or not outcomes_persisted:
        status = NOTIFICATION_DUPLICATE_OR_AMBIGUOUS
    elif delivery.rejected:
        status = NOTIFICATION_FAILED
    else:
        # The adapter had enough configuration to attempt but provided no explicit
        # outcome. Retaining the durable claim is safer and more truthful than saying
        # the delivery failed and inviting a duplicate retry.
        status = NOTIFICATION_DUPLICATE_OR_AMBIGUOUS
    return WebsiteWhatsAppBriefResult(brief, status)
