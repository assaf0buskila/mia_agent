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

MAX_ASKED_ACTIONS = 24

# Minimum meaningful prospect answers before the website may offer WhatsApp. Guideline
# from ADR-023: enough context to be useful, early enough not to become an interview.
MIN_DISCOVERY_TURNS_FOR_WHATSAPP = 3


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

    def has_asked(self, action: "NextAction") -> bool:
        return action.value in self.asked_actions

    def known_facts(self) -> list[str]:
        """Stable fact keys already established. Used to forbid re-asking."""
        facts: list[str] = []
        if self.workflow_known:
            facts.append("business_and_daily_work")
        if self.manual_step_known:
            facts.append("specific_manual_step")
        if self.data_source_known:
            facts.append("where_the_data_comes_from")
        if self.impact_confirmed:
            facts.append("frequency_or_time_cost")
        if self.metric_known:
            facts.append("business_consequence")
        if self.authority_known:
            facts.append("decision_maker")
        if self.timeline_known:
            facts.append("timeline")
        if self.company_domain:
            facts.append("company_domain")
        return facts

    def open_questions(self) -> list[str]:
        """Important facts still missing, in the order they are worth asking."""
        pending: list[str] = []
        if not self.workflow_known:
            pending.append("business_and_daily_work")
        if not self.manual_step_known:
            pending.append("specific_manual_step")
        if not self.data_source_known:
            pending.append("where_the_data_comes_from")
        if not self.impact_confirmed:
            pending.append("frequency_or_time_cost")
        if not self.metric_known:
            pending.append("business_consequence")
        return pending


def compute_missing_fields(state: SalesState) -> list[str]:
    known = {
        "decision_maker": state.authority_known,
        "timeline": state.timeline_known,
        "metric": state.metric_known,
    }
    return [name for name in MEDDPICC_MISSING_ORDER if not known[name]]


def has_reframe_context(sales: SalesState | None) -> bool:
    return (
        sales is not None
        and sales.workflow_known
        and sales.impact_confirmed
        and sales.reflected
    )


def website_whatsapp_continuation_ready(state: SalesState) -> bool:
    """Website may offer WhatsApp once there is real context worth continuing.

    Two ways to qualify. Stated intent ("I want a website", "I'm opening a business")
    needs no workflow ladder — there is no workflow yet, and interviewing someone who
    already told us what they want is the interview failure mode. Everyone else needs
    the business, one concrete manual step and a real friction.

    Either way at least one substantive answer is required, so a greeting never
    triggers the offer. `discovery_turns` is a floor on engagement, not a script
    counter, and it never triggers the offer on its own.
    """
    if state.whatsapp_handoff_offered:
        return False
    if state.fit == FitLevel.POOR:
        return False
    if state.willingness_to_meet is not None:
        return False
    if state.owner_required:
        return False
    if state.discovery_turns < 1:
        return False
    if state.explicit_buying_intent:
        return True
    if not state.workflow_known:
        return False
    if not manual_step_established(state):
        return False
    if state.pain_level < PainLevel.P2:
        return False
    return state.discovery_turns >= MIN_DISCOVERY_TURNS_FOR_WHATSAPP


MAX_REPEATS_PER_QUESTION = 2


def times_asked(state: SalesState, action: NextAction) -> int:
    return state.asked_actions.count(action.value)


def manual_step_established(state: SalesState) -> bool:
    """A concrete manual step is known, or a later rung already implies it.

    Deliberately does not accept `impact_confirmed`. "We miss calls all day" is a
    confirmed cost with no manual step behind it yet, and inferring one there is what
    made Mia reflect back manual work the prospect never described.
    """
    return state.manual_step_known or state.reflected or state.hypothesis_offered


def discovery_ladder(state: SalesState) -> list[NextAction]:
    """Consultative progression, coarsest first. Only unmet rungs appear.

    The fine-grained rungs from the sales philosophy (specific manual step, where the
    data comes from, frequency, time, consequence) map onto the existing action
    vocabulary rather than adding new actions: DEEPEN_PAIN owns the concrete manual
    step, QUANTIFY owns source, frequency and cost.
    """
    rungs: list[NextAction] = []
    if not state.workflow_known:
        rungs.append(NextAction.UNDERSTAND_WORKFLOW)
    if not manual_step_established(state) or state.pain_level <= PainLevel.P1:
        rungs.append(NextAction.DEEPEN_PAIN)
    if not state.impact_confirmed:
        rungs.append(NextAction.QUANTIFY)
    if not state.reflected:
        rungs.append(NextAction.REFLECT)
    if not state.hypothesis_offered:
        rungs.append(NextAction.OFFER_HYPOTHESIS)
    if not state.buying_reality_known:
        rungs.append(NextAction.QUALIFY)
    return rungs


def select_next_action(
    state: SalesState, *, channel: str | None = None, meeting_first: bool = False
) -> NextAction:
    """Deterministic next-best-action. Not a script. Not an LLM.

    Omit `channel` for Graph Lab / inbound WhatsApp / owner review so the
    full MEDDPICC funnel stays unchanged. Website graph passes channel.

    A discovery question is never selected more than `MAX_REPEATS_PER_QUESTION` times.
    Once a rung is exhausted the ladder advances, which is what stops the website loop
    when keyword extraction cannot confirm a short answer.

    `meeting_first` (ADR-028, default False so every existing caller keeps today's
    behavior): once the website continuation gate passes, the booked meeting is the
    measurable, closable exit, so it is offered before WhatsApp. WhatsApp stays the
    fallback: once the meeting has already been offered (`meeting_exit_offered`) and
    the visitor did not take it, or the visitor explicitly asks for WhatsApp, the next
    continuation-ready turn offers WhatsApp exactly as it always has.
    """
    if state.fit == FitLevel.POOR:
        return NextAction.DISQUALIFY
    if state.willingness_to_meet is False:
        return NextAction.STOP
    if state.owner_required:
        return NextAction.HANDOFF
    if state.active_objection is not None:
        return NextAction.HANDLE_OBJECTION
    if state.willingness_to_meet is True:
        # They asked for the meeting. One qualifying question is fair so Assaf arrives
        # prepared; more discovery is the fastest way to lose a buyer who said yes.
        if not state.workflow_known or not state.buying_reality_known:
            return NextAction.QUALIFY
        return NextAction.OFFER_MEETING
    if channel == "website" and website_whatsapp_continuation_ready(state):
        if meeting_first and not state.meeting_exit_offered:
            return NextAction.OFFER_MEETING
        return NextAction.OFFER_WHATSAPP
    for rung in discovery_ladder(state):
        if times_asked(state, rung) < MAX_REPEATS_PER_QUESTION:
            return rung
    if (
        state.fit == FitLevel.GOOD
        and state.willingness_to_meet is True
        and state.pain_level >= PainLevel.P2
    ):
        return NextAction.OFFER_MEETING
    return NextAction.QUALIFY


def mark_action_delivered(state: SalesState, action: NextAction) -> SalesState:
    """Mark funnel moves delivered this turn so the next inbound can progress."""
    updated = state.model_copy()
    if action == NextAction.REFLECT:
        updated.reflected = True
    elif action == NextAction.OFFER_HYPOTHESIS:
        updated.hypothesis_offered = True
    elif action == NextAction.OFFER_WHATSAPP:
        updated.whatsapp_handoff_offered = True
    elif action == NextAction.OFFER_MEETING:
        updated.meeting_exit_offered = True
    updated.asked_actions = [*updated.asked_actions, action.value][-MAX_ASKED_ACTIONS:]
    return updated
