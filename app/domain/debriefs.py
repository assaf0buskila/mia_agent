"""Post-meeting debrief persistence (§12.3): owner WhatsApp, persist-only, no send."""

from typing import Literal

from pydantic import BaseModel

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.approvals import extract_approval_lead_id
from app.domain.events import Channel, build_meeting_debrief_event

OUTCOME_HELD = "held"
OUTCOME_NO_SHOW = "no_show"
OUTCOME_UNCLEAR = "unclear"
ALLOWLISTED_OUTCOMES = frozenset({OUTCOME_HELD, OUTCOME_NO_SHOW, OUTCOME_UNCLEAR})
NEXT_STEP_NONE = "none"
NEXT_STEP_FOLLOW_UP = "follow_up"
NEXT_STEP_PROPOSAL = "proposal"
ALLOWLISTED_NEXT_STEPS = frozenset(
    {NEXT_STEP_NONE, NEXT_STEP_FOLLOW_UP, NEXT_STEP_PROPOSAL}
)

_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})

_NO_SHOW_PHRASES: tuple[str, ...] = (
    "no-show",
    "no show",
    "didn't show",
    "did not show",
    "לא הגיע",
    "לא הגיעה",
)
_HELD_PHRASES: tuple[str, ...] = (
    "we met",
    "meeting went",
    "הפגישה התקיימה",
    "נפגשנו",
    "היה בפגישה",
)
_FOLLOW_UP_PHRASES: tuple[str, ...] = (
    "follow up",
    "follow-up",
    "מעקב",
)
_PROPOSAL_PHRASES: tuple[str, ...] = (
    "send a proposal",
    "send proposal",
    "לשלוח הצעה",
)

OwnerDebriefStatus = Literal["skipped", "ambiguous", "unknown_lead", "persisted"]


class OwnerDebriefResult(BaseModel):
    status: OwnerDebriefStatus
    lead_id: str | None = None


def extract_debrief_lead_id(text: str) -> str | None:
    return extract_approval_lead_id(text)


def _phrase_in_text(text: str, phrase: str) -> bool:
    haystack = text.translate(_APOSTROPHES).lower()
    needle = phrase.translate(_APOSTROPHES).lower()
    return needle in haystack


def parse_debrief_outcome(text: str) -> str:
    """Deterministic outcome from allowlisted phrases only."""
    has_no_show = any(_phrase_in_text(text, phrase) for phrase in _NO_SHOW_PHRASES)
    has_held = any(_phrase_in_text(text, phrase) for phrase in _HELD_PHRASES)
    if has_no_show and has_held:
        return OUTCOME_UNCLEAR
    if has_no_show:
        return OUTCOME_NO_SHOW
    return OUTCOME_HELD


def parse_debrief_next_step(text: str) -> str:
    """Deterministic next_step from allowlisted phrases only."""
    has_follow_up = any(_phrase_in_text(text, phrase) for phrase in _FOLLOW_UP_PHRASES)
    has_proposal = any(_phrase_in_text(text, phrase) for phrase in _PROPOSAL_PHRASES)
    if has_follow_up and has_proposal:
        return NEXT_STEP_NONE
    if has_follow_up:
        return NEXT_STEP_FOLLOW_UP
    if has_proposal:
        return NEXT_STEP_PROPOSAL
    return NEXT_STEP_NONE


def apply_owner_meeting_debrief(
    store,
    *,
    text: str,
    channel: Channel,
    kill_switch: bool,
) -> OwnerDebriefResult:
    """Persist sanitized meeting debrief when owner message includes lead_id. Never sends."""
    if kill_switch:
        return OwnerDebriefResult(status="skipped")
    lead_id = extract_debrief_lead_id(text)
    if lead_id is None:
        return OwnerDebriefResult(status="ambiguous")
    if store.get_lead(lead_id) is None:
        return OwnerDebriefResult(status="unknown_lead", lead_id=lead_id)
    try:
        assert_allowed(
            RiskAction(name="meeting_debrief_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return OwnerDebriefResult(status="skipped", lead_id=lead_id)
    outcome = parse_debrief_outcome(text)
    next_step = parse_debrief_next_step(text)
    event = build_meeting_debrief_event(
        provider=channel.value,
        channel=channel,
        lead_id=lead_id,
        outcome=outcome,
        next_step=next_step,
    )
    store.upsert_meeting_debrief(
        lead_id=lead_id,
        outcome=outcome,
        next_step=next_step,
        estimated_value="",
        notes="",
    )
    store.save_canonical_event(provider=channel.value, event=event)
    return OwnerDebriefResult(status="persisted", lead_id=lead_id)


def ack_for_debrief_result(result: OwnerDebriefResult) -> str | None:
    if result.status == "ambiguous":
        return "מה שהבנתי: סיכום פגישה. אני לא מבצעת כלום. מה מזהה הליד?"
    if result.status == "unknown_lead":
        return "מה שהבנתי: סיכום פגישה. לא מצאתי את הליד. אני לא מבצעת כלום."
    if result.status == "persisted":
        return "נשמר סיכום פגישה. לא עדכנתי שווי עסקה ולא יצרתי אירוע ביומן."
    return None
