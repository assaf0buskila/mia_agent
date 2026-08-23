"""Deterministic relative due-date parsing and due scan for owner task commitments."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, field_validator

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.followups import follow_up_due_on
from app.domain.owner_tasks import OwnerTaskDecision, OwnerTaskType

TRIGGER_NONE = "none"
TRIGGER_DUE_DATE = "due_date"
TRIGGER_SPEND_THRESHOLD = "spend_threshold"
CONDITION_NONE = "none"
CONDITION_IF_NOT_REPLIED = "if_not_replied"
ACTION_NONE = "none"
ACTION_FOLLOW_UP = "follow_up"
ACTION_ANALYZE = "analyze"
ACTION_RESEARCH = "research"
ACTION_LOG = "log"

_VALID_TRIGGERS = frozenset({TRIGGER_NONE, TRIGGER_DUE_DATE, TRIGGER_SPEND_THRESHOLD})
ALLOWLISTED_OWNER_TASK_LIST_TRIGGERS = frozenset({TRIGGER_SPEND_THRESHOLD})
_VALID_CONDITIONS = frozenset({CONDITION_NONE, CONDITION_IF_NOT_REPLIED})
_VALID_ACTIONS = frozenset(
    {ACTION_NONE, ACTION_FOLLOW_UP, ACTION_ANALYZE, ACTION_RESEARCH, ACTION_LOG}
)

OWNER_TASK_STATUS_LOGGED = "logged"

ALLOWLISTED_OWNER_TASK_SCAN_REASONS = frozenset(
    {
        "due_pending",
        "if_not_replied",
        "needs_clarification",
        "not_due_trigger",
        "spend_reached",
        "spend_below",
        "spend_unknown",
        "no_budget",
    }
)

_TOKENS: tuple[tuple[str, int], ...] = (
    ("today", 0),
    ("היום", 0),
    ("tomorrow", 1),
    ("מחר", 1),
    ("next week", 7),
    ("בשבוע הבא", 7),
)
_HEBREW_LETTER = "\u0590-\u05FF"

_SPEND_THRESHOLD_TOKENS = (
    "after spend reaches",
    "when spend reaches",
    "when spend hits",
    "spend reaches the",
    "כשההוצאה מגיעה",
    "כשההוצאה תגיע",
    "אחרי שההוצאה",
)

_CONDITION_TOKENS = (
    "if he has not replied",
    "if they have not replied",
    "if they haven't replied",
    "hasn't replied",
    "has not replied",
    "if he hasn't replied",
    "if they don't reply",
    "if no reply",
    "אם לא ענה",
    "אם לא יענה",
    "אם לא ענתה",
    "אם לא תענה",
)

_ACTION_BY_TYPE = {
    "sales": ACTION_FOLLOW_UP,
    "analytics": ACTION_ANALYZE,
    "research": ACTION_RESEARCH,
    "linkedin": ACTION_LOG,
    "support": ACTION_LOG,
    "meeting_debrief": ACTION_LOG,
    "daily_brief": ACTION_LOG,
    "weekly_brief": ACTION_LOG,
    "lead_review": ACTION_LOG,
    "content_idea": ACTION_LOG,
    "gmail_summary": ACTION_LOG,
    "seo": ACTION_LOG,
    "calendar": ACTION_LOG,
    "owner_notify": ACTION_LOG,
    "meeting_brief": ACTION_LOG,
    "owner_status": ACTION_LOG,
}


class OwnerCommitment(BaseModel):
    trigger: str
    condition: str
    action: str

    @field_validator("trigger")
    @classmethod
    def _validate_trigger(cls, value: str) -> str:
        if value not in _VALID_TRIGGERS:
            raise ValueError(f"invalid trigger: {value}")
        return value

    @field_validator("condition")
    @classmethod
    def _validate_condition(cls, value: str) -> str:
        if value not in _VALID_CONDITIONS:
            raise ValueError(f"invalid condition: {value}")
        return value

    @field_validator("action")
    @classmethod
    def _validate_action(cls, value: str) -> str:
        if value not in _VALID_ACTIONS:
            raise ValueError(f"invalid action: {value}")
        return value


def _token_in_text(text: str, token: str) -> bool:
    if any(ord(ch) > 127 for ch in token):
        pattern = rf"(?<![{_HEBREW_LETTER}]){re.escape(token)}(?![{_HEBREW_LETTER}])"
        return re.search(pattern, text) is not None
    haystack = text.lower()
    needle = token.lower()
    if " " in needle:
        return needle in haystack
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def parse_due_at(text: str, *, now: datetime, timezone: str = "Asia/Jerusalem") -> str | None:
    """Return YYYY-MM-DD in *timezone* when a allowlisted relative token matches."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_now = now.astimezone(ZoneInfo(timezone))
    matched_offsets: list[int] = []
    for token, offset_days in _TOKENS:
        if _token_in_text(text, token):
            matched_offsets.append(offset_days)
    if not matched_offsets:
        return None
    due_date = local_now.date() + timedelta(days=min(matched_offsets))
    return due_date.isoformat()


def parse_condition(text: str) -> str:
    for token in _CONDITION_TOKENS:
        if _token_in_text(text, token):
            return CONDITION_IF_NOT_REPLIED
    return CONDITION_NONE


def parse_spend_threshold(text: str) -> bool:
    for token in _SPEND_THRESHOLD_TOKENS:
        if _token_in_text(text, token):
            return True
    return False


def evaluate_spend_threshold(
    *, spend_mtd: float | None, monthly_budget: float | None
) -> tuple[bool, str]:
    if monthly_budget is None:
        return False, "no_budget"
    if spend_mtd is None:
        return False, "spend_unknown"
    if spend_mtd >= monthly_budget:
        return True, "spend_reached"
    return False, "spend_below"


class OwnerTaskScanResult(BaseModel):
    provider: str
    provider_event_id: str
    due_ready: bool
    reason: str

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        if value not in ALLOWLISTED_OWNER_TASK_SCAN_REASONS:
            raise ValueError(f"invalid owner task scan reason: {value}")
        return value


def _scan_date_due_row(row) -> tuple[bool, str]:
    if row.status != OWNER_TASK_STATUS_LOGGED:
        return False, "needs_clarification"
    if row.trigger != TRIGGER_DUE_DATE:
        return False, "not_due_trigger"
    if row.condition == CONDITION_IF_NOT_REPLIED:
        return False, "if_not_replied"
    return True, "due_pending"


def scan_due_owner_tasks(
    store,
    *,
    timezone: str,
    now: datetime | None = None,
    monthly_budget: float | None = None,
    spend_mtd: float | None = None,
) -> list[OwnerTaskScanResult]:
    """Evaluate due logged owner tasks and persist scan fields. Never executes."""
    effective_now = now or datetime.now(UTC)
    date_rows: list = []
    try:
        today = follow_up_due_on(now=effective_now, timezone=timezone, offset_days=0)
        date_rows = store.list_due_owner_tasks(due_on=today)
    except (ValueError, OSError, KeyError, ZoneInfoNotFoundError):
        date_rows = []

    spend_rows = store.list_owner_tasks_by_trigger(trigger=TRIGGER_SPEND_THRESHOLD)
    seen: set[tuple[str, str]] = set()
    rows_to_scan: list[tuple[object, str]] = []
    for row in date_rows:
        key = (row.provider, row.provider_event_id)
        if key in seen:
            continue
        seen.add(key)
        rows_to_scan.append((row, "date"))
    for row in spend_rows:
        key = (row.provider, row.provider_event_id)
        if key in seen:
            continue
        seen.add(key)
        rows_to_scan.append((row, "spend_threshold"))

    results: list[OwnerTaskScanResult] = []
    for row, mode in rows_to_scan:
        try:
            assert_allowed(
                RiskAction(name="owner_task_scan", risk=RiskLevel.R1_LOW_WRITE),
                kill_switch=False,
            )
        except PolicyDenied:
            continue

        if mode == "spend_threshold":
            due_ready, reason = evaluate_spend_threshold(
                spend_mtd=spend_mtd,
                monthly_budget=monthly_budget,
            )
        else:
            due_ready, reason = _scan_date_due_row(row)

        store.save_owner_task_scan(
            provider=row.provider,
            provider_event_id=row.provider_event_id,
            due_ready=due_ready,
            block_reason=reason,
        )
        results.append(
            OwnerTaskScanResult(
                provider=row.provider,
                provider_event_id=row.provider_event_id,
                due_ready=due_ready,
                reason=reason,
            )
        )
    return results


def plan_owner_commitment(
    *, decision: OwnerTaskDecision, text: str, due_at: str | None
) -> OwnerCommitment:
    if (
        decision.needs_clarification
        or decision.task_type
        in (OwnerTaskType.PREFERENCE, OwnerTaskType.APPROVAL)
    ):
        return OwnerCommitment(
            trigger=TRIGGER_NONE,
            condition=CONDITION_NONE,
            action=ACTION_NONE,
        )
    action = _ACTION_BY_TYPE.get(decision.task_type.value, ACTION_LOG)
    if action == ACTION_ANALYZE and parse_spend_threshold(text):
        trigger = TRIGGER_SPEND_THRESHOLD
    elif due_at and decision.task_type not in (
        OwnerTaskType.DAILY_BRIEF,
        OwnerTaskType.WEEKLY_BRIEF,
        OwnerTaskType.LEAD_REVIEW,
        OwnerTaskType.CONTENT_IDEA,
        OwnerTaskType.GMAIL_SUMMARY,
        OwnerTaskType.SEO,
        OwnerTaskType.CALENDAR,
        OwnerTaskType.OWNER_NOTIFY,
        OwnerTaskType.MEETING_BRIEF,
        OwnerTaskType.HUMAN_TAKEOVER,
        OwnerTaskType.HUMAN_TAKEOVER_RESUME,
        OwnerTaskType.CONVERSATION_SCOPE,
        OwnerTaskType.HOT_LEADS,
        OwnerTaskType.OWNER_STATUS,
    ):
        trigger = TRIGGER_DUE_DATE
    else:
        trigger = TRIGGER_NONE
    condition = (
        parse_condition(text) if action == ACTION_FOLLOW_UP else CONDITION_NONE
    )
    return OwnerCommitment(trigger=trigger, condition=condition, action=action)
