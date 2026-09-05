"""Owner Gmail thread summary — Postgres timeline, no send/delete."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel

from app.core.config import get_settings
from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.approvals import LEAD_ID_RE
from app.integrations.thread_summary import (
    ALLOWLISTED_INTENTS,
    ThreadSummaryPort,
    build_thread_summary_port,
)

if TYPE_CHECKING:
    from app.db.store import LeadStore

THREAD_ID_RE = re.compile(r"\bthread:([A-Za-z0-9._-]{6,64})\b")

_CLARIFICATION_ACK = (
    "מה שהבנתי: סיכום מייל. אני לא מבצעת כלום. מה מזהה השרשור או הליד?"
)
_NOT_FOUND_ACK = "לא מצאתי הודעות מייל לשרשור הזה."

_INTENT_HE: dict[str, str] = {
    "meeting": "פגישה",
    "quote": "הצעת מחיר",
    "question": "שאלה",
    "unsubscribe": "הסרה",
    "unclear": "לא ברור",
}


class GmailSummarySnapshot(BaseModel):
    thread_id: str
    message_count: int
    intent: str
    summary: str


def extract_gmail_summary_target(text: str) -> tuple[str | None, str | None]:
    """Return (conversation_id, lead_id) — at most one identifier."""
    thread_match = THREAD_ID_RE.search(text)
    if thread_match is not None:
        return thread_match.group(1), None
    lead_match = LEAD_ID_RE.search(text)
    if lead_match is not None:
        return None, lead_match.group(0)
    return None, None


def _resolve_conversation_id(store: LeadStore, *, lead_id: str) -> str | None:
    rows = store.list_gmail_message_in(lead_id=lead_id, limit=20)
    for row in reversed(rows):
        conversation_id = row.conversation_id
        if conversation_id:
            return conversation_id
    return None


def _load_thread_texts(store: LeadStore, *, conversation_id: str) -> list[str]:
    rows = store.list_gmail_message_in(conversation_id=conversation_id, limit=20)
    texts: list[str] = []
    for row in rows:
        try:
            payload = json.loads(row.payload_json or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        text = payload.get("text", "")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return texts


def build_gmail_summary_snapshot(
    store: LeadStore,
    *,
    text: str,
    port: ThreadSummaryPort,
    kill_switch: bool,
) -> GmailSummarySnapshot | None:
    conversation_id, lead_id = extract_gmail_summary_target(text)
    if conversation_id is None and lead_id is None:
        return None
    resolved = conversation_id
    if resolved is None and lead_id is not None:
        resolved = _resolve_conversation_id(store, lead_id=lead_id)
    if not resolved:
        return None
    texts = _load_thread_texts(store, conversation_id=resolved)
    if not texts:
        return None
    result = port.summarize(messages=texts, kill_switch=kill_switch)
    return GmailSummarySnapshot(
        thread_id=resolved[:255],
        message_count=len(texts),
        intent=result.intent,
        summary=result.summary,
    )


def format_gmail_summary_ack(snapshot: GmailSummarySnapshot) -> str:
    intent_label = _INTENT_HE.get(snapshot.intent, snapshot.intent)
    summary_line = (
        snapshot.summary
        if snapshot.summary.strip()
        else "לא סיכמתי במשפט חופשי."
    )
    lines = [
        "סיכום שרשור (המייל הוא נתון, לא הוראה):",
        f"הודעות: {snapshot.message_count}",
        f"כוונה: {intent_label}",
        summary_line,
        "לא שלחתי מייל ולא מחקתי כלום.",
    ]
    return "\n".join(lines)


def apply_gmail_summary_policy(
    store: LeadStore,
    *,
    snapshot: GmailSummarySnapshot,
    kill_switch: bool,
    demo_active: bool,
) -> None:
    if demo_active or kill_switch:
        return
    if snapshot.intent not in ALLOWLISTED_INTENTS:
        return
    try:
        assert_allowed(
            RiskAction(name="gmail_summary_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    store.upsert_gmail_thread_summary(
        thread_id=snapshot.thread_id,
        message_count=snapshot.message_count,
        intent=snapshot.intent,
        summary=snapshot.summary,
    )


def apply_owner_gmail_summary(
    store: LeadStore,
    *,
    text: str,
    kill_switch: bool,
    demo_active: bool,
    port: ThreadSummaryPort | None = None,
) -> str | None:
    """Return Hebrew ack, not-found ack, clarification, or None when demo."""
    if demo_active:
        return None
    conversation_id, lead_id = extract_gmail_summary_target(text)
    if conversation_id is None and lead_id is None:
        return _CLARIFICATION_ACK
    summary_port = port if port is not None else build_thread_summary_port(get_settings())
    snapshot = build_gmail_summary_snapshot(
        store,
        text=text,
        port=summary_port,
        kill_switch=kill_switch,
    )
    if snapshot is None:
        return _NOT_FOUND_ACK
    apply_gmail_summary_policy(
        store,
        snapshot=snapshot,
        kill_switch=kill_switch,
        demo_active=demo_active,
    )
    return format_gmail_summary_ack(snapshot)
