"""Brief Assaf on Telegram when a website visitor clicks through to WhatsApp.

Mia does not answer on WhatsApp until official Cloud API inbound exists.
The customer opens Assaf's chat; this module is how Assaf knows who they are
and what first line is worth sending. No LLM: the paste line is deterministic.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from app.core.config import Settings
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.hot_handoff import notify_owners
from app.domain.memory import ROLE_MIA, ConversationTurn, counterpart_turns
from app.domain.owner_lead_card import (
    card_who,
    format_owner_lead_card,
    last_message_short,
)
from app.domain.sales import PainLevel, SalesState, select_next_action
from app.integrations.sheets import build_sheets_port, maybe_mirror_lead_snapshot
from app.integrations.telegram_format import blockquote, esc

KIND_WEBSITE_WHATSAPP = "website_whatsapp_handoff"
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
    stage: str = "",
) -> str:
    """Owner-facing briefing, HTML. No prices, no invented facts, no customer phone.

    Structured card first (lead id, stage, what they said, next action, WhatsApp
    offered). Paste line and transcript follow as detail, not the opening wall.
    """
    paste = _recommended_first_line(sales=sales, turns=turns)
    after_parts = [
        esc("מיה לא תענה שם. תטפל אתה."),
        "",
        f"{esc('השורה שלך:')}",
        esc(paste),
    ]
    transcript = _transcript_lines(turns)
    if transcript:
        after_parts.extend(
            ["", esc("השיחה:"), blockquote("\n".join(transcript), expandable=True)]
        )
    return format_owner_lead_card(
        title="ליד מהאתר → וואטסאפ",
        lead_id=lead_id,
        stage=stage,
        last_said=last_message_short(turns),
        next_action=select_next_action(sales, channel="website").value,
        whatsapp_offered=True,
        who=card_who(lead_id, sales),
        extra_pairs=[("מה ידוע", " · ".join(_fact_lines(sales)))],
        after="\n".join(after_parts),
    )[:_BRIEF_MAX]


def format_website_human_handoff_brief(
    *,
    lead_id: str,
    sales: SalesState,
    turns: list[ConversationTurn],
    stage: str = "",
) -> str:
    """Owner ping when the website graph hands off. HTML. Includes the conversation.

    This is not a WhatsApp click. Do not tell Assaf the visitor is already in his
    WhatsApp inbox — they are not, until they tap the widget CTA.
    """
    after_parts = [esc("מיה עצרה באתר. תטפל אתה.")]
    transcript = _transcript_lines(turns)
    if transcript:
        after_parts.extend(
            ["", esc("השיחה:"), blockquote("\n".join(transcript), expandable=True)]
        )
    return format_owner_lead_card(
        title="ליד מהאתר — צריך אותך",
        lead_id=lead_id,
        stage=stage,
        last_said=last_message_short(turns),
        next_action=select_next_action(sales, channel="website").value,
        whatsapp_offered=sales.whatsapp_handoff_offered,
        who=card_who(lead_id, sales),
        extra_pairs=[("מה ידוע", " · ".join(_fact_lines(sales)))],
        after="\n".join(after_parts),
    )[:_BRIEF_MAX]


def apply_website_whatsapp_handoff_brief(
    store,
    *,
    lead_id: str,
    session_id: str,
    settings: Settings,
) -> str | None:
    """Persist once per lead and best-effort Telegram. Never raises to the website."""
    if settings.kill_switch or settings.demo_mode:
        return None
    try:
        assert_allowed(
            RiskAction(name="website_whatsapp_brief", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=settings.kill_switch,
        )
    except PolicyDenied:
        return None
    sales = store.get_sales(lead_id)
    turns = store.list_conversation_turns(session_id)
    stage = ""
    getter = getattr(store, "get_lead_stage", None)
    if callable(getter):
        try:
            stage = getter(lead_id) or ""
        except KeyError:
            stage = ""
    brief = format_website_whatsapp_brief(
        lead_id=lead_id, sales=sales, turns=turns, stage=stage
    )
    # The claim, not a prior read, decides whether we send. A read-then-send left two
    # concurrent /handoff clicks both seeing "not yet notified" and both pushing the
    # brief. Lead-scoped on purpose: one WhatsApp handoff brief per lead, not per
    # conversation, so conversation_id stays empty here.
    now_iso = datetime.now(UTC).replace(microsecond=0).isoformat()
    claimed = store.try_insert_owner_notification(
        kind=KIND_WEBSITE_WHATSAPP,
        lead_id=lead_id,
        scheduled_at=now_iso,
    )
    if not claimed:
        return brief
    notify_owners(brief=brief, inbound_id=session_id, settings=settings)
    try:
        maybe_mirror_lead_snapshot(
            sheets=build_sheets_port(settings),
            store=store,
            lead_id=lead_id,
            channel="website",
            next_action="offer_whatsapp",
            conversation_id=session_id,
            kill_switch=settings.kill_switch,
        )
    except (KeyError, AttributeError, RuntimeError, TypeError):
        pass
    return brief
