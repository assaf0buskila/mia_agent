"""R3 commercial + R4 campaign approval persistence (§33): pending row, no send, no Meta."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.events import Channel, build_approval_required_event
from app.domain.sales import SalesState

ACTION_PROPOSAL_HANDOFF = "proposal_handoff"
ACTION_CAMPAIGN_WRITE = "campaign_write"
ACTION_WEBSITE_EDIT = "website_edit"
DECISION_PENDING = "pending"
DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"
RISK_R3 = "R3"
RISK_R4 = "R4"
RESOURCE_LEAD = "lead"
RESOURCE_CAMPAIGN = "campaign"
RESOURCE_WEBSITE = "website"
WEBSITE_RESOURCE_ID = "assafweb-home"
APPROVAL_TTL = timedelta(hours=24)
_NEXT_ACTION = "handoff"
_CAMPAIGN_ID_VALID = re.compile(r"^[0-9]{5,24}$")

LEAD_ID_RE = re.compile(r"\blead_[a-f0-9]{12}\b")
APPROVAL_ID_RE = re.compile(r"\bapr_[a-f0-9]{12}\b")
CAMPAIGN_ID_RE = re.compile(r"(?<![0-9])([0-9]{5,24})(?![0-9])")

_APPROVE_PHRASES: tuple[str, ...] = (
    "approve the proposal",
    "approve the quote",
    "אשר את ההצעה",
    "אשר הצעת מחיר",
)
_REJECT_PHRASES: tuple[str, ...] = (
    "reject the proposal",
    "reject the quote",
    "דחה את ההצעה",
    "דחי את ההצעה",
    "דחה הצעת מחיר",
)

_CAMPAIGN_REQUEST_PHRASES: tuple[str, ...] = (
    "pause campaign",
    "request campaign pause",
    "השהה קמפיין",
    "בקשת השהייה לקמפיין",
)
_CAMPAIGN_DECIDE_APPROVE_PHRASES: tuple[str, ...] = (
    "approve campaign",
    "אשר קמפיין",
)
_CAMPAIGN_DECIDE_REJECT_PHRASES: tuple[str, ...] = (
    "reject campaign",
    "דחה קמפיין",
)

_WEBSITE_REQUEST_PHRASES: tuple[str, ...] = (
    "propose website change",
    "הצע שינוי באתר",
)
_WEBSITE_DECIDE_APPROVE_PHRASES: tuple[str, ...] = (
    "approve website change",
    "אשר שינוי באתר",
)
_WEBSITE_DECIDE_REJECT_PHRASES: tuple[str, ...] = (
    "reject website change",
    "דחה שינוי באתר",
)

OwnerApprovalStatus = Literal[
    "none",
    "ambiguous",
    "decided",
    "already_decided",
    "skipped",
    "expired",
    "unbound",
    "queued",
]


class OwnerApprovalResult(BaseModel):
    status: OwnerApprovalStatus
    decision: str | None = None
    lead_id: str | None = None
    campaign_id: str | None = None
    website_id: str | None = None


def new_approval_id() -> str:
    return f"apr_{uuid4().hex[:12]}"


def _identity_blob(
    *,
    action: str,
    risk: str,
    channel: str,
    resource_type: str,
    resource_id: str,
) -> dict[str, str]:
    return {
        "action": action,
        "channel": channel,
        "resource_id": resource_id,
        "resource_type": resource_type,
        "risk": risk,
    }


def _identity_json(
    *,
    action: str,
    risk: str,
    channel: str,
    resource_type: str,
    resource_id: str,
) -> str:
    return json.dumps(
        _identity_blob(
            action=action,
            risk=risk,
            channel=channel,
            resource_type=resource_type,
            resource_id=resource_id,
        ),
        separators=(",", ":"),
        sort_keys=True,
    )


def proposed_parameters_json(
    *,
    action: str,
    risk: str,
    channel: str,
    resource_type: str,
    resource_id: str,
) -> str:
    blob = _identity_json(
        action=action,
        risk=risk,
        channel=channel,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    if len(blob) > 255:
        return ""
    return blob


def payload_hash(
    *,
    action: str,
    risk: str,
    channel: str,
    resource_type: str,
    resource_id: str,
) -> str:
    """SHA-256 of compact action identity JSON. No message text."""
    blob = _identity_json(
        action=action,
        risk=risk,
        channel=channel,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def website_edit_payload_hash(
    *,
    action: str,
    risk: str,
    before: str,
    after: str,
) -> str:
    """SHA-256 binding website edit before/after copy (capped)."""
    blob = json.dumps(
        {
            "action": action,
            "after": after[:255],
            "before": before[:255],
            "risk": risk,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def extract_website_edit_parts(text: str) -> tuple[str, str]:
    """Parse before/after from owner message; empty when absent."""
    lowered = text.lower()
    before = ""
    after = ""
    for before_key, after_key in (
        ("before:", "after:"),
        ("לפני:", "אחרי:"),
    ):
        b_idx = lowered.find(before_key)
        a_idx = lowered.find(after_key)
        if b_idx >= 0 and a_idx > b_idx:
            before = text[b_idx + len(before_key) : a_idx].strip()[:255]
            after = text[a_idx + len(after_key) :].strip()[:255]
            break
    return before, after


def approval_expires_at(*, now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return (now + APPROVAL_TTL).isoformat()


def is_approval_expired(row, *, now: datetime) -> bool:
    if not row.expires_at:
        return True
    try:
        expires = datetime.fromisoformat(row.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    effective_now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return effective_now >= expires


def resource_hash_matches(row) -> bool:
    expected = payload_hash(
        action=row.action,
        risk=row.risk,
        channel=row.channel,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
    )
    return row.payload_hash == expected


def _phrase_in_text(text: str, phrase: str) -> bool:
    haystack = text.lower()
    needle = phrase.lower()
    return needle in haystack


def _hebrew_phrase_in_text(text: str, phrase: str) -> bool:
    return phrase in text


def _campaign_request_in_text(text: str) -> bool:
    return any(
        _phrase_in_text(text, phrase)
        if phrase.isascii()
        else _hebrew_phrase_in_text(text, phrase)
        for phrase in _CAMPAIGN_REQUEST_PHRASES
    )


def _campaign_decide_in_text(text: str) -> str | None:
    has_approve = any(
        _phrase_in_text(text, phrase)
        if phrase.isascii()
        else _hebrew_phrase_in_text(text, phrase)
        for phrase in _CAMPAIGN_DECIDE_APPROVE_PHRASES
    )
    has_reject = any(
        _phrase_in_text(text, phrase)
        if phrase.isascii()
        else _hebrew_phrase_in_text(text, phrase)
        for phrase in _CAMPAIGN_DECIDE_REJECT_PHRASES
    )
    if has_approve and not has_reject:
        return DECISION_APPROVED
    if has_reject and not has_approve:
        return DECISION_REJECTED
    return None


def campaign_id_ambiguous(text: str) -> bool:
    scrubbed = LEAD_ID_RE.sub(" ", text)
    return len(set(CAMPAIGN_ID_RE.findall(scrubbed))) > 1


def extract_campaign_id(text: str) -> str | None:
    scrubbed = LEAD_ID_RE.sub(" ", text)
    matches = CAMPAIGN_ID_RE.findall(scrubbed)
    if not matches:
        return None
    unique = set(matches)
    if len(unique) > 1:
        return None
    campaign_id = matches[0]
    if not _CAMPAIGN_ID_VALID.fullmatch(campaign_id):
        return None
    return campaign_id


def parse_campaign_approval_intent(text: str) -> str | None:
    """Return pending_request/approved/rejected when exactly one intent matches; else None."""
    has_request = _campaign_request_in_text(text)
    decide = _campaign_decide_in_text(text)
    if has_request and decide is None:
        return "pending_request"
    if decide is not None and not has_request:
        return decide
    return None


def _website_request_in_text(text: str) -> bool:
    return any(
        _phrase_in_text(text, phrase)
        if phrase.isascii()
        else _hebrew_phrase_in_text(text, phrase)
        for phrase in _WEBSITE_REQUEST_PHRASES
    )


def _website_decide_in_text(text: str) -> str | None:
    has_approve = any(
        _phrase_in_text(text, phrase)
        if phrase.isascii()
        else _hebrew_phrase_in_text(text, phrase)
        for phrase in _WEBSITE_DECIDE_APPROVE_PHRASES
    )
    has_reject = any(
        _phrase_in_text(text, phrase)
        if phrase.isascii()
        else _hebrew_phrase_in_text(text, phrase)
        for phrase in _WEBSITE_DECIDE_REJECT_PHRASES
    )
    if has_approve and not has_reject:
        return DECISION_APPROVED
    if has_reject and not has_approve:
        return DECISION_REJECTED
    return None


def parse_website_approval_intent(text: str) -> str | None:
    has_request = _website_request_in_text(text)
    decide = _website_decide_in_text(text)
    if has_request and decide is None:
        return "pending_request"
    if decide is not None and not has_request:
        return decide
    return None


def parse_approval_intent(text: str) -> str | None:
    """Return approved/rejected when exactly one intent matches; else None."""
    has_approve = any(_phrase_in_text(text, phrase) for phrase in _APPROVE_PHRASES)
    has_reject = any(_phrase_in_text(text, phrase) for phrase in _REJECT_PHRASES)
    if has_approve and not has_reject:
        return DECISION_APPROVED
    if has_reject and not has_approve:
        return DECISION_REJECTED
    return None


def extract_approval_lead_id(text: str) -> str | None:
    match = LEAD_ID_RE.search(text)
    return match.group(0) if match else None


def extract_approval_id(text: str) -> str | None:
    matches = APPROVAL_ID_RE.findall(text)
    if len(matches) != 1:
        return None
    return matches[0]


def _is_valid_lead_pending(row, *, now: datetime) -> bool:
    if is_approval_expired(row, now=now):
        return False
    if not resource_hash_matches(row):
        return False
    if row.resource_type != RESOURCE_LEAD or row.resource_id != row.lead_id:
        return False
    if row.lead_id is None:
        return False
    return True


def _is_valid_campaign_pending(row, *, now: datetime) -> bool:
    if is_approval_expired(row, now=now):
        return False
    if not resource_hash_matches(row):
        return False
    if row.resource_type != RESOURCE_CAMPAIGN or row.lead_id is not None:
        return False
    if not _CAMPAIGN_ID_VALID.fullmatch(row.resource_id or ""):
        return False
    return True


def _validate_pending_row(row, *, lead_id: str, now: datetime) -> OwnerApprovalResult | None:
    if is_approval_expired(row, now=now):
        return OwnerApprovalResult(status="expired", lead_id=lead_id)
    if not resource_hash_matches(row):
        return OwnerApprovalResult(status="unbound", lead_id=lead_id)
    if row.resource_type != RESOURCE_LEAD or row.resource_id != row.lead_id:
        return OwnerApprovalResult(status="unbound", lead_id=lead_id)
    return None


def _validate_campaign_pending_row(
    row, *, campaign_id: str, now: datetime
) -> OwnerApprovalResult | None:
    if is_approval_expired(row, now=now):
        return OwnerApprovalResult(status="expired", campaign_id=campaign_id)
    if not resource_hash_matches(row):
        return OwnerApprovalResult(status="unbound", campaign_id=campaign_id)
    if row.resource_type != RESOURCE_CAMPAIGN or row.resource_id != campaign_id:
        return OwnerApprovalResult(status="unbound", campaign_id=campaign_id)
    if row.lead_id is not None:
        return OwnerApprovalResult(status="unbound", campaign_id=campaign_id)
    return None


def apply_campaign_write_approval_policy(
    store,
    *,
    campaign_id: str,
    channel: Channel,
    kill_switch: bool,
) -> bool:
    """Persist pending campaign_write approval. Never calls Meta."""
    if kill_switch:
        return False
    if not _CAMPAIGN_ID_VALID.fullmatch(campaign_id):
        return False
    try:
        assert_allowed(
            RiskAction(name="approval_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return False
    now = datetime.now(UTC)
    expires_at = approval_expires_at(now=now)
    digest = payload_hash(
        action=ACTION_CAMPAIGN_WRITE,
        risk=RISK_R4,
        channel=channel.value,
        resource_type=RESOURCE_CAMPAIGN,
        resource_id=campaign_id,
    )
    store.upsert_campaign_approval(
        channel=channel.value,
        action=ACTION_CAMPAIGN_WRITE,
        risk=RISK_R4,
        payload_hash=digest,
        decision=DECISION_PENDING,
        resource_type=RESOURCE_CAMPAIGN,
        resource_id=campaign_id,
        expires_at=expires_at,
    )
    claim_key = f"{campaign_id}:approval:{ACTION_CAMPAIGN_WRITE}"
    if not store.claim_operation(scope="approval", key=claim_key):
        return True
    store.save_canonical_event(
        provider=channel.value,
        event=build_approval_required_event(
            provider=channel.value,
            channel=channel,
            lead_id=None,
            action=ACTION_CAMPAIGN_WRITE,
            risk=RISK_R4,
            resource_id=campaign_id,
        ),
    )
    store.complete_operation(
        scope="approval",
        key=claim_key,
        result_json='{"ok": true}',
    )
    return True


def _website_proposed_parts(row) -> tuple[str, str]:
    try:
        data = json.loads(row.proposed_parameters or "{}")
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    before = data.get("before", "")
    after = data.get("after", "")
    return (
        before.strip()[:255] if isinstance(before, str) else "",
        after.strip()[:255] if isinstance(after, str) else "",
    )


def website_resource_hash_matches(row) -> bool:
    before, after = _website_proposed_parts(row)
    expected = website_edit_payload_hash(
        action=row.action,
        risk=row.risk,
        before=before,
        after=after,
    )
    return row.payload_hash == expected


def _validate_website_pending_row(
    row, *, website_id: str, now: datetime
) -> OwnerApprovalResult | None:
    if is_approval_expired(row, now=now):
        return OwnerApprovalResult(status="expired", website_id=website_id)
    if not website_resource_hash_matches(row):
        return OwnerApprovalResult(status="unbound", website_id=website_id)
    if row.resource_type != RESOURCE_WEBSITE or row.resource_id != website_id:
        return OwnerApprovalResult(status="unbound", website_id=website_id)
    if row.lead_id is not None:
        return OwnerApprovalResult(status="unbound", website_id=website_id)
    return None


def apply_website_edit_approval_policy(
    store,
    *,
    before: str,
    after: str,
    channel: Channel,
    kill_switch: bool,
) -> bool:
    """Persist pending website_edit approval. Never writes AssafWeb files."""
    if kill_switch:
        return False
    try:
        assert_allowed(
            RiskAction(name="approval_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return False
    now = datetime.now(UTC)
    expires_at = approval_expires_at(now=now)
    digest = website_edit_payload_hash(
        action=ACTION_WEBSITE_EDIT,
        risk=RISK_R3,
        before=before,
        after=after,
    )
    params = json.dumps(
        {"before": before[:255], "after": after[:255]},
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(params) > 255:
        params = params[:255]
    store.upsert_website_approval(
        channel=channel.value,
        action=ACTION_WEBSITE_EDIT,
        risk=RISK_R3,
        payload_hash=digest,
        decision=DECISION_PENDING,
        resource_type=RESOURCE_WEBSITE,
        resource_id=WEBSITE_RESOURCE_ID,
        expires_at=expires_at,
        proposed_parameters=params,
    )
    claim_key = f"{WEBSITE_RESOURCE_ID}:approval:{ACTION_WEBSITE_EDIT}"
    if not store.claim_operation(scope="approval", key=claim_key):
        return True
    store.save_canonical_event(
        provider=channel.value,
        event=build_approval_required_event(
            provider=channel.value,
            channel=channel,
            lead_id=None,
            action=ACTION_WEBSITE_EDIT,
            risk=RISK_R3,
            resource_id=WEBSITE_RESOURCE_ID,
        ),
    )
    store.complete_operation(
        scope="approval",
        key=claim_key,
        result_json='{"ok": true}',
    )
    return True


def apply_owner_approval_decision(
    store,
    *,
    text: str,
    channel: Channel,
    kill_switch: bool,
    now: datetime | None = None,
) -> OwnerApprovalResult:
    """Persist owner approve/reject on pending rows only. Never sends or calls Meta."""
    if kill_switch:
        return OwnerApprovalResult(status="skipped")
    effective_now = now if now is not None else datetime.now(UTC)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=UTC)

    campaign_intent = parse_campaign_approval_intent(text)
    website_intent = parse_website_approval_intent(text)
    lead_intent = parse_approval_intent(text)
    intents = [item for item in (campaign_intent, website_intent, lead_intent) if item is not None]
    if len(intents) > 1:
        return OwnerApprovalResult(status="ambiguous")

    if website_intent is not None:
        if website_intent == "pending_request":
            before, after = extract_website_edit_parts(text)
            queued = apply_website_edit_approval_policy(
                store,
                before=before,
                after=after,
                channel=channel,
                kill_switch=kill_switch,
            )
            if not queued:
                return OwnerApprovalResult(status="skipped", website_id=WEBSITE_RESOURCE_ID)
            return OwnerApprovalResult(status="queued", website_id=WEBSITE_RESOURCE_ID)
        row = store.get_approval_by_resource(
            RESOURCE_WEBSITE, WEBSITE_RESOURCE_ID, ACTION_WEBSITE_EDIT
        )
        if row is None:
            return OwnerApprovalResult(status="none", website_id=WEBSITE_RESOURCE_ID)
        if row.decision != DECISION_PENDING:
            return OwnerApprovalResult(
                status="already_decided",
                decision=row.decision,
                website_id=WEBSITE_RESOURCE_ID,
            )
        blocked = _validate_website_pending_row(
            row, website_id=WEBSITE_RESOURCE_ID, now=effective_now
        )
        if blocked is not None:
            return blocked
        try:
            assert_allowed(
                RiskAction(name="approval_decide", risk=RiskLevel.R1_LOW_WRITE),
                kill_switch=kill_switch,
            )
        except PolicyDenied:
            return OwnerApprovalResult(status="skipped", website_id=WEBSITE_RESOURCE_ID)
        updated = store.decide_website_approval(
            resource_id=WEBSITE_RESOURCE_ID,
            decision=website_intent,
            now=effective_now,
        )
        if not updated:
            return OwnerApprovalResult(status="none", website_id=WEBSITE_RESOURCE_ID)
        return OwnerApprovalResult(
            status="decided",
            decision=website_intent,
            website_id=WEBSITE_RESOURCE_ID,
        )

    if campaign_intent is not None:
        if campaign_id_ambiguous(text):
            return OwnerApprovalResult(status="ambiguous")
        campaign_id = extract_campaign_id(text)
        if campaign_id is None:
            return OwnerApprovalResult(status="none")
        if campaign_intent == "pending_request":
            queued = apply_campaign_write_approval_policy(
                store,
                campaign_id=campaign_id,
                channel=channel,
                kill_switch=kill_switch,
            )
            if not queued:
                return OwnerApprovalResult(status="skipped", campaign_id=campaign_id)
            return OwnerApprovalResult(status="queued", campaign_id=campaign_id)
        row = store.get_approval_by_resource(
            RESOURCE_CAMPAIGN, campaign_id, ACTION_CAMPAIGN_WRITE
        )
        if row is None:
            return OwnerApprovalResult(status="none", campaign_id=campaign_id)
        if row.decision != DECISION_PENDING:
            return OwnerApprovalResult(
                status="already_decided",
                decision=row.decision,
                campaign_id=campaign_id,
            )
        blocked = _validate_campaign_pending_row(
            row, campaign_id=campaign_id, now=effective_now
        )
        if blocked is not None:
            return blocked
        try:
            assert_allowed(
                RiskAction(name="approval_decide", risk=RiskLevel.R1_LOW_WRITE),
                kill_switch=kill_switch,
            )
        except PolicyDenied:
            return OwnerApprovalResult(status="skipped", campaign_id=campaign_id)
        updated = store.decide_campaign_approval(
            resource_id=campaign_id,
            decision=campaign_intent,
            now=effective_now,
        )
        if not updated:
            return OwnerApprovalResult(status="none", campaign_id=campaign_id)
        return OwnerApprovalResult(
            status="decided",
            decision=campaign_intent,
            campaign_id=campaign_id,
        )

    if lead_intent is None:
        return OwnerApprovalResult(status="none")

    intent = lead_intent
    approval_id = extract_approval_id(text)
    lead_id = extract_approval_lead_id(text)
    if approval_id is not None:
        row = store.get_approval_by_approval_id(approval_id)
        if row is None:
            return OwnerApprovalResult(status="none")
        if row.action != ACTION_PROPOSAL_HANDOFF or not row.lead_id:
            return OwnerApprovalResult(status="unbound")
        if lead_id is not None and row.lead_id != lead_id:
            return OwnerApprovalResult(status="unbound", lead_id=lead_id)
        if row.decision != DECISION_PENDING:
            return OwnerApprovalResult(
                status="already_decided",
                decision=row.decision,
                lead_id=row.lead_id,
            )
        blocked = _validate_pending_row(row, lead_id=row.lead_id, now=effective_now)
        if blocked is not None:
            return blocked
        target_lead_id = row.lead_id
    elif lead_id is not None:
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        if row is None:
            return OwnerApprovalResult(status="none")
        if row.decision != DECISION_PENDING:
            return OwnerApprovalResult(
                status="already_decided",
                decision=row.decision,
                lead_id=lead_id,
            )
        blocked = _validate_pending_row(row, lead_id=lead_id, now=effective_now)
        if blocked is not None:
            return blocked
        target_lead_id = lead_id
    else:
        pending = store.list_pending_approvals(action=ACTION_PROPOSAL_HANDOFF)
        valid = [row for row in pending if _is_valid_lead_pending(row, now=effective_now)]
        if len(valid) == 0:
            return OwnerApprovalResult(status="none")
        if len(valid) > 1:
            return OwnerApprovalResult(status="ambiguous")
        target_lead_id = valid[0].lead_id
        row = valid[0]
        if row.decision != DECISION_PENDING:
            return OwnerApprovalResult(
                status="already_decided",
                decision=row.decision,
                lead_id=target_lead_id,
            )
    try:
        assert_allowed(
            RiskAction(name="approval_decide", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return OwnerApprovalResult(status="skipped", lead_id=target_lead_id)
    updated = store.decide_approval(
        lead_id=target_lead_id,
        action=ACTION_PROPOSAL_HANDOFF,
        decision=intent,
        now=effective_now,
    )
    if not updated:
        return OwnerApprovalResult(status="none", lead_id=target_lead_id)
    return OwnerApprovalResult(
        status="decided",
        decision=intent,
        lead_id=target_lead_id,
    )


def ack_for_approval_result(result: OwnerApprovalResult) -> str:
    if result.status == "queued" and result.website_id is not None:
        return "רשמתי בקשת אישור לשינוי באתר. לא שיניתי קבצים."
    if result.status == "queued":
        return "רשמתי בקשת אישור לקמפיין. לא שיניתי מודעות במטא."
    if result.status == "none":
        return "אין בקשת הצעה ממתינה. לא ביצעתי כלום."
    if result.status == "ambiguous":
        return (
            "יש כמה בקשות ממתינות. אני לא מאשרת בלי שתגיד איזו. לא ביצעתי כלום."
        )
    if result.status == "decided" and result.decision == DECISION_APPROVED:
        if result.website_id is not None:
            return (
                "רשמתי אישור לשינוי באתר. יישם ב-AssafWeb דרך Cursor — "
                "מיא לא תעשה git-push."
            )
        if result.campaign_id is not None:
            return "רשמתי אישור לקמפיין. לא שיניתי מודעות במטא."
        return "רשמתי אישור להצעה. לא שלחתי אותה — זה לטיפול ידני."
    if result.status == "decided" and result.decision == DECISION_REJECTED:
        if result.website_id is not None:
            return "רשמתי דחייה לשינוי באתר. לא שיניתי קבצים."
        if result.campaign_id is not None:
            return "רשמתי דחייה לקמפיין. לא שיניתי מודעות במטא."
        return "רשמתי דחייה לבקשת ההצעה. לא שלחתי כלום."
    if result.status == "already_decided":
        return "הבקשה כבר טופלה. לא שיניתי כלום."
    if result.status == "expired":
        return "הבקשה פגה. לא ביצעתי כלום."
    if result.status == "unbound":
        return "האישור לא תואם למשאב. לא ביצעתי כלום."
    return ""


def apply_approval_policy(
    store,
    *,
    lead_id: str,
    channel: Channel,
    action: str,
    sales: SalesState,
    kill_switch: bool,
) -> None:
    """Persist pending approval on owner handoff. Never sends; swallows PolicyDenied only."""
    action_key = str(action).casefold().strip()
    if action_key != _NEXT_ACTION:
        return
    if not sales.owner_required:
        return
    if kill_switch:
        return
    try:
        assert_allowed(
            RiskAction(name="approval_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    now = datetime.now(UTC)
    expires_at = approval_expires_at(now=now)
    digest = payload_hash(
        action=ACTION_PROPOSAL_HANDOFF,
        risk=RISK_R3,
        channel=channel.value,
        resource_type=RESOURCE_LEAD,
        resource_id=lead_id,
    )
    store.upsert_approval(
        lead_id=lead_id,
        channel=channel.value,
        action=ACTION_PROPOSAL_HANDOFF,
        risk=RISK_R3,
        payload_hash=digest,
        decision=DECISION_PENDING,
        resource_type=RESOURCE_LEAD,
        resource_id=lead_id,
        expires_at=expires_at,
    )
    claim_key = f"{lead_id}:approval:{ACTION_PROPOSAL_HANDOFF}"
    if not store.claim_operation(scope="approval", key=claim_key):
        return
    store.save_canonical_event(
        provider=channel.value,
        event=build_approval_required_event(
            provider=channel.value,
            channel=channel,
            lead_id=lead_id,
        ),
    )
    store.complete_operation(
        scope="approval",
        key=claim_key,
        result_json='{"ok": true}',
    )
