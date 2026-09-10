from enum import IntEnum, StrEnum

from pydantic import BaseModel, Field


class PainLevel(IntEnum):
    P0 = 0
    P1 = 1
    P2 = 2
    P3 = 3
    P4 = 4
    P5 = 5


class FitLevel(StrEnum):
    UNKNOWN = "unknown"
    POOR = "poor"
    POSSIBLE = "possible"
    GOOD = "good"


class ObjectionKind(StrEnum):
    PRICE = "price"
    # Asking what it costs is not the same as saying it is too expensive. Answering
    # "what feels expensive?" to a pricing question is how Mia sounds like a bot.
    PRICE_QUESTION = "price_question"
    AI_TRUST = "ai_trust"
    NO_TIME = "no_time"
    HAS_VENDOR = "has_vendor"
    NOT_URGENT = "not_urgent"
    NEED_PARTNER = "need_partner"


class NextAction(StrEnum):
    UNDERSTAND_WORKFLOW = "understand_workflow"
    DEEPEN_PAIN = "deepen_pain"
    QUANTIFY = "quantify"
    REFLECT = "reflect"
    OFFER_HYPOTHESIS = "offer_hypothesis"
    QUALIFY = "qualify"
    OFFER_MEETING = "offer_meeting"
    OFFER_WHATSAPP = "offer_whatsapp"
    HANDOFF = "handoff"
    HANDLE_OBJECTION = "handle_objection"
    DISQUALIFY = "disqualify"
    STOP = "stop"


MEDDPICC_MISSING_ORDER = ("decision_maker", "timeline", "metric")

# Historical persistence bound for the retained SalesState schema.
MAX_ASKED_ACTIONS = 24

class SalesState(BaseModel):
    lead_id: str
    pain_level: PainLevel = PainLevel.P0
    fit: FitLevel = FitLevel.UNKNOWN
    workflow_known: bool = False
    impact_confirmed: bool = False
    reflected: bool = False
    hypothesis_offered: bool = False
    buying_reality_known: bool = False
    authority_known: bool = False
    timeline_known: bool = False
    metric_known: bool = False
    willingness_to_meet: bool | None = None
    owner_required: bool = False
    active_objection: ObjectionKind | None = None
    missing_fields: list[str] = Field(default_factory=list)
    company_domain: str = ""
    whatsapp_handoff_offered: bool = False
    # ADR-028: the booked meeting is the website's default exit; WhatsApp is the
    # fallback once the meeting offer has already been made and not taken.
    meeting_exit_offered: bool = False
    manual_step_known: bool = False
    data_source_known: bool = False
    discovery_turns: int = 0
    asked_actions: list[str] = Field(default_factory=list)
    explicit_buying_intent: bool = False
    # Short human label from the prospect's own words, for owner-facing lists only.
    headline: str = ""
    # Person name only when they said it. Never inferred from the business description.
    display_name: str = ""

def compute_missing_fields(state: SalesState) -> list[str]:
    known = {
        "decision_maker": state.authority_known,
        "timeline": state.timeline_known,
        "metric": state.metric_known,
    }
    return [name for name in MEDDPICC_MISSING_ORDER if not known[name]]


def manual_step_established(state: SalesState) -> bool:
    """A concrete manual step is known, or a later rung already implies it.

    Deliberately does not accept `impact_confirmed`. "We miss calls all day" is a
    confirmed cost with no manual step behind it yet, and inferring one there is what
    made Mia reflect back manual work the prospect never described.
    """
    return state.manual_step_known or state.reflected or state.hypothesis_offered
