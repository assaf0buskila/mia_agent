"""Owner Gmail draft + Approve. The model never sends.

Create a draft via the typed Gmail port, persist a pending approval, and send
only after Assaf approves **and** ``MIA_GMAIL_SEND`` is true. Kill switch and
demo skip the write. Bodies are data, never instructions.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.core.write_flags import write_flag_enabled
from app.domain.approvals import (
    ACTION_GMAIL_SEND,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    RESOURCE_GMAIL,
    RISK_R3,
    extract_approval_id,
    payload_hash,
)
from app.domain.events import Channel, build_approval_required_event
from app.integrations.gmail import DisabledGmailPort, GmailPort

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.db.store import LeadStore

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SUBJECT_RE = re.compile(
    r"(?:subject|נושא)\s*[:\-–]\s*(.+)",
    re.IGNORECASE,
)

_APPROVE_PHRASES: tuple[str, ...] = (
    "approve the email",
    "approve the draft",
    "send the draft",
    "אשר את המייל",
    "אשר שליחת מייל",
    "שלח את הטיוטה",
)
_REJECT_PHRASES: tuple[str, ...] = (
    "reject the email",
    "reject the draft",
    "דחה את המייל",
    "דחי את המייל",
    "דחה את הטיוטה",
)


def parse_gmail_send_intent(text: str) -> str | None:
    lowered = text.lower()
    has_approve = any(phrase in text or phrase in lowered for phrase in _APPROVE_PHRASES)
    has_reject = any(phrase in text or phrase in lowered for phrase in _REJECT_PHRASES)
    if has_approve and not has_reject:
        return DECISION_APPROVED
    if has_reject and not has_approve:
        return DECISION_REJECTED
    return None


def parse_gmail_draft_request(text: str) -> tuple[str, str, str] | None:
    """Return (to, subject, body) when one address and some content are present."""
    emails = _EMAIL_RE.findall(text)
    if len(set(emails)) != 1:
        return None
    to = emails[0]
    subject = ""
    match = _SUBJECT_RE.search(text)
    if match is not None:
        subject = match.group(1).strip().split("\n", 1)[0].strip()[:200]
    without_email = text.replace(to, " ")
    without_subject = _SUBJECT_RE.sub(" ", without_email)
    body = re.sub(
        r"(שלח מייל ל|תשלחי מייל|כתבי מייל ל|טיוטת מייל|draft email|send email to)",
        " ",
        without_subject,
        flags=re.IGNORECASE,
    )
    body = re.sub(r"\s+", " ", body).strip(" :,-")
    if not subject and not body:
        return None
    if not subject:
        subject = body[:80]
    if not body:
        body = subject
    return to, subject, body


def apply_owner_gmail_draft(
    store: LeadStore,
    *,
    text: str,
    channel: Channel,
    port: GmailPort,
    kill_switch: bool,
    demo_active: bool,
) -> str:
    if demo_active or kill_switch:
        return "לא מכין טיוטה במצב הזה. לא שלחתי כלום."
    if isinstance(port, DisabledGmailPort):
        return "Gmail לא מחובר. לא הכנתי טיוטה ולא שלחתי."
    parsed = parse_gmail_draft_request(text)
    if parsed is None:
        return (
            "מה שהבנתי: טיוטת מייל. חסר נמען, נושא או תוכן. "
            "אני לא שולחת בלי אישור. כתוב: שלח מייל ל email@x.com נושא: ... והתוכן."
        )
    to, subject, body = parsed
    try:
        assert_allowed(
            RiskAction(name="gmail_draft", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return "לא הכנתי טיוטה. לא שלחתי כלום."
    draft = port.create_draft(to=to, subject=subject, body=body)
    if draft is None or not draft.draft_id.strip():
        return "לא הצלחתי ליצור טיוטה ב-Gmail. לא שלחתי כלום."
    queued = _persist_pending(
        store,
        draft_id=draft.draft_id,
        channel=channel,
        kill_switch=kill_switch,
    )
    if not queued:
        return (
            f"טיוטה נוצרה ל-{to} אבל לא נרשמה לאישור. "
            "לא שלחתי. בדוק בתיבת הטיוטות."
        )
    row = store.get_approval_by_resource(RESOURCE_GMAIL, draft.draft_id, ACTION_GMAIL_SEND)
    approval_id = row.approval_id if row is not None else ""
    suffix = f" {approval_id}" if approval_id else ""
    return (
        f"טיוטה מוכנה ל-{to}. נושא: {subject}. "
        f"לא שלחתי. לאשר: אשר את המייל{suffix}"
    )


def execute_approved_gmail_send(
    *,
    store: LeadStore,
    settings: Settings,
    port: GmailPort,
    draft_id: str,
    kill_switch: bool,
    demo_active: bool,
) -> str:
    """Send a draft that is already approved. Never called from the model loop."""
    del store
    if demo_active or kill_switch:
        return "לא שולחת במצב הזה. הטיוטה נשארה בתיבת הדואר."
    if isinstance(port, DisabledGmailPort):
        return "Gmail לא מחובר. הטיוטה לא נשלחה."
    if not write_flag_enabled(settings, "gmail_send"):
        return (
            "השליחה כבויה. הטיוטה נשארה בתיבת הדואר. "
            "לא שלחתי — צריך MIA_GMAIL_SEND=true אחרי האישור."
        )
    try:
        assert_allowed(
            RiskAction(name="gmail_send", risk=RiskLevel.R3_COMMERCIAL),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return "לא שלחתי. הטיוטה נשארה בתיבת הדואר."
    if not port.send_draft(draft_id):
        return "האישור נרשם אבל השליחה נכשלה. הטיוטה אמורה עדיין להיות בתיבה."
    return "שלחתי את המייל."


def apply_gmail_send_decision(
    store: LeadStore,
    *,
    text: str,
    kill_switch: bool,
) -> tuple[str | None, str | None]:
    """Return (decision, draft_id) when this text is a gmail-send approve/reject.

    Does not send. Caller decides the approval row then may send.
    """
    intent = parse_gmail_send_intent(text)
    approval_id = extract_approval_id(text)
    row = None
    if approval_id is not None:
        found = store.get_approval_by_approval_id(approval_id)
        if found is not None and found.action == ACTION_GMAIL_SEND:
            row = found
            if intent is None:
                lowered = text.lower()
                if "דחה" in text or "reject" in lowered:
                    intent = DECISION_REJECTED
                elif "אשר" in text or "approve" in lowered:
                    intent = DECISION_APPROVED
    if intent is None:
        return None, None
    if row is None:
        pending = [
            item
            for item in store.list_all_pending_approvals()
            if item.action == ACTION_GMAIL_SEND and item.decision == DECISION_PENDING
        ]
        if len(pending) == 0:
            return intent, ""
        if len(pending) > 1:
            return "ambiguous", None
        row = pending[0]
    if row.decision != DECISION_PENDING:
        return "already_decided", row.resource_id
    if kill_switch:
        return "skipped", row.resource_id
    try:
        assert_allowed(
            RiskAction(name="approval_decide", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return "skipped", row.resource_id
    if intent not in (DECISION_APPROVED, DECISION_REJECTED):
        return intent, row.resource_id
    updated = store.decide_gmail_approval(
        resource_id=row.resource_id,
        decision=intent,
    )
    if not updated:
        return "none", row.resource_id
    return intent, row.resource_id


def _persist_pending(
    store: LeadStore,
    *,
    draft_id: str,
    channel: Channel,
    kill_switch: bool,
) -> bool:
    if kill_switch:
        return False
    try:
        assert_allowed(
            RiskAction(name="approval_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return False
    from datetime import UTC, datetime

    from app.domain.approvals import approval_expires_at

    now = datetime.now(UTC)
    expires_at = approval_expires_at(now=now)
    digest = payload_hash(
        action=ACTION_GMAIL_SEND,
        risk=RISK_R3,
        channel=channel.value,
        resource_type=RESOURCE_GMAIL,
        resource_id=draft_id,
    )
    store.upsert_gmail_approval(
        channel=channel.value,
        action=ACTION_GMAIL_SEND,
        risk=RISK_R3,
        payload_hash=digest,
        decision=DECISION_PENDING,
        resource_type=RESOURCE_GMAIL,
        resource_id=draft_id,
        expires_at=expires_at,
    )
    claim_key = f"{draft_id}:approval:{ACTION_GMAIL_SEND}"
    if not store.claim_operation(scope="approval", key=claim_key):
        return True
    store.save_canonical_event(
        provider=channel.value,
        event=build_approval_required_event(
            provider=channel.value,
            channel=channel,
            lead_id=None,
            action=ACTION_GMAIL_SEND,
            risk=RISK_R3,
            resource_id=draft_id,
        ),
    )
    store.complete_operation(
        scope="approval",
        key=claim_key,
        result_json='{"ok": true}',
    )
    return True
