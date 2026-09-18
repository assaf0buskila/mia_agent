"""Owner brain: one Telegram owner turn through the model-led tool loop.

`answer_owner` retrieves owner memory and public knowledge once, binds the house
read ports, and runs `app.graph.owner_agent.run_owner_agent`. When the agent cannot
finish, the owner gets one honest failure line instead of an invented answer.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import NamedTuple

from app.brain.context import (
    assemble_owner_context,
)
from app.brain.embeddings import EmbeddingPort, build_embedding_port
from app.brain.retrieval import MemoryScoreWeights
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.core.models import model_chain
from app.core.owner_timing import owner_stage
from app.db.store import LeadStore
from app.domain.memory import ConversationTurn
from app.domain.owner.request_routing import requests_no_history
from app.domain.owner.tasks import OwnerTaskType
from app.graph.owner_agent import AgentOutcome, OwnerUsage, run_owner_agent
from app.integrations.calendar import (
    CalendarAgendaPort,
    CalendarPort,
    build_calendar_agenda_port,
    build_calendar_port,
)
from app.integrations.ga4 import Ga4Port, build_ga4_port
from app.integrations.gmail import GmailPort, build_gmail_port
from app.integrations.instagram_insights import (
    InstagramInsightsPort,
    build_instagram_insights_port,
)
from app.integrations.linkedin import LinkedInPort, build_linkedin_port
from app.integrations.llm_client import (
    GEMINI_CHAT_URL,
    OPENAI_RESPONSES_URL,
    LlmClient,
    LlmModelChain,
)
from app.integrations.research import ResearchPort, build_research_port
from app.integrations.search_console import SearchConsolePort, build_search_console_port
from app.integrations.seo_audit import SeoAuditPort, build_seo_audit_port
from app.integrations.sheets import SheetsPort, build_sheets_port
from app.integrations.telegram_format import hebrew_datetime
from app.tools.registries.owner_tools import ToolContext

# The honest failure line for a NOTE turn the agent was allowed to run but could not
# complete (provider error, refusal, truncation, empty reply, budget/ceiling exhausted).
# Deliberately not "מה שהבנתי" -- that phrase means "I couldn't classify this", which is
# false once the agent was actually invoked. One line, no apology, no internals, no
# secrets. The parenthetical is a failure *class* Assaf can report (provider / empty /
# timeout), never a model id or key.
NOTE_AGENT_FAILURE_TEXT = "הבדיקה לא עברה כרגע. תנסה שוב."

_NOTE_FAILURE_CLASSES: tuple[tuple[str, str], ...] = (
    ("fresh linkedin profile evidence unavailable", "פרופיל LinkedIn עדכני לא זמין"),
    ("incomplete_evidence", "פרופיל LinkedIn לא הושלם"),
    ("empty_reply", "תשובה ריקה"),
    ("empty reply", "תשובה ריקה"),
    ("timeout", "תם הזמן"),
    ("timed out", "תם הזמן"),
    # The turn budget ran out and the loop exited cooperatively rather than waiting
    # for a provider read timeout to surface. Both spellings appear: `completion` is
    # "deadline_exceeded" and the accompanying `error` is "deadline exceeded", and
    # either can arrive alone. Without these two needles a budget exhaustion fell
    # through to the generic "שגיאה" -- the one failure class Assaf can actually
    # act on, reported as an unclassified error. Deliberately NOT folded in with
    # "budget_exhausted" below: that one means the *step* budget was spent while the
    # clock was still fine, which is a different thing to tell him.
    #
    # Position is deliberate. This scan is first-match-wins over a blob of
    # `completion` + `reason`, and an aggregated `LlmModelChain` failure can carry
    # both a 429 and a chain-deadline message. Placing these above "http 429" means
    # such a turn reports "תם הזמן" rather than "עומס ספק" -- chosen because the
    # actionable fact for Assaf is that the turn ran out of time, and because the
    # pre-existing "timeout"/"timed out" needles directly above already sit above
    # "http 429", so timeout-class beating load-class is the established precedence
    # here, not something these two lines introduce.
    ("deadline_exceeded", "תם הזמן"),
    ("deadline exceeded", "תם הזמן"),
    ("http 429", "עומס ספק"),
    ("refused", "סירוב מודל"),
    ("truncated", "תשובה נחתכה"),
    ("budget_exhausted", "לא הושלם"),
    ("ceiling_hit", "לא הושלם"),
    ("provider_error", "שגיאת ספק"),
    ("http ", "שגיאת ספק"),
    ("llm request failed", "שגיאת ספק"),
)


def classify_note_agent_failure(reason: str, completion: str = "") -> str:
    """Map a fallback_reason onto a short Hebrew class. Never returns secrets."""
    blob = f"{completion} {reason}".lower()
    for needle, label in _NOTE_FAILURE_CLASSES:
        if needle in blob:
            return label
    return "שגיאה"


def format_note_agent_failure(reason: str, completion: str = "") -> str:
    label = classify_note_agent_failure(reason, completion)
    return f"הבדיקה לא עברה כרגע ({label}). תנסה שוב."


# Same channel as `log_owner_agent`, so a graph failure lands next to the turn it broke.
_LOG = logging.getLogger("mia.agent")

class OwnerBrainResult(NamedTuple):
    text: str
    used_agent: bool
    tools_used: tuple[str, ...]
    tokens_in: int = 0
    tokens_out: int = 0
    memories_written: int = 0
    # Why the agent did not answer. Empty when it did. This was the missing piece: the
    # fallback was silent, so a misconfigured model looked exactly like normal operation
    # for a full day of live testing.
    fallback_reason: str = ""
    model: str = ""
    # Observability (Task 3), threaded straight from `AgentOutcome` for `log_owner_agent`.
    # Zero/empty on every early-exit path (kill switch, deterministic intent, no model) --
    # those never construct an outcome, so there is nothing to report.
    steps: int = 0
    tools_failed: tuple[str, ...] = ()
    completion: str = ""
    # Exact durable approvals created by this turn's tool executions.
    approval_ids: tuple[str, ...] = ()


def build_agent_client(settings: Settings) -> LlmModelChain:
    """Every configured model, in order.

    `MIA_OWNER_AGENT_FALLBACK_MODEL` was documented but previously ignored — only
    `chain[0]` was ever used. A primary the account cannot call therefore dropped straight
    to the keyword classifier instead of trying the secondary.

    Website and owner ids stay separate so one purpose cannot silently borrow another
    purpose's rollout or capability assumptions.
    """
    chain = model_chain(
        settings.owner_agent_model,
        settings.owner_agent_fallback_model,
    )
    clients = [
        LlmClient(
            api_key=settings.openai_api_key,
            model=name,
            url=OPENAI_RESPONSES_URL,
            timeout=settings.llm_request_timeout_seconds,
            reasoning_effort=settings.owner_agent_reasoning_effort,
        )
        for name in chain
    ]
    clients.extend(
        _gemini_clients(
            settings,
            settings.owner_agent_gemini_model,
            reasoning_effort=settings.owner_agent_reasoning_effort,
        )
    )
    return LlmModelChain(clients)


def _gemini_clients(
    settings: Settings, model: str, *, reasoning_effort: str = ""
) -> list[LlmClient]:
    """Gemini OpenAI-compat as the cross-provider last resort.

    Same `tools` wire shape as Chat Completions, so the agent loop needs no changes. It is
    last on purpose: the compatibility layer silently ignores parameters it does not
    support, so it is a safety net for an OpenAI-side outage or model block, not a peer.
    """
    key = settings.gemini_api_key.strip()
    name = model.strip()
    if not key or not name:
        return []
    return [
        LlmClient(
            api_key=key,
            model=name,
            url=GEMINI_CHAT_URL,
            timeout=settings.llm_request_timeout_seconds,
            reasoning_effort=reasoning_effort,
        )
    ]


def _weights(settings: Settings) -> MemoryScoreWeights:
    return MemoryScoreWeights(
        relevance=settings.memory_weight_relevance,
        recency=settings.memory_weight_recency,
        importance=settings.memory_weight_importance,
    )


def bind_owner_house_ports(settings: Settings) -> dict[str, object]:
    """Same house Composio entity as Cursor. Never invent a second login."""
    return {
        "calendar": build_calendar_port(settings),
        "calendar_agenda": build_calendar_agenda_port(settings),
        "gmail": build_gmail_port(settings),
        "linkedin": build_linkedin_port(settings),
        "search_console": build_search_console_port(settings),
        "ga4": build_ga4_port(settings),
        "seo_audit": build_seo_audit_port(settings),
        "instagram_insights": build_instagram_insights_port(settings),
        "research": build_research_port(settings),
        "sheets": build_sheets_port(settings),
    }


def answer_owner(
    *,
    principal: Principal,
    store: LeadStore,
    brain: BrainStore,
    settings: Settings,
    task_type: OwnerTaskType,
    owner_text: str,
    raw_owner_request: str | None = None,
    history: tuple[ConversationTurn, ...],
    fallback_text: str,
    kill_switch: bool,
    demo_active: bool,
    calendar: CalendarPort | None = None,
    calendar_agenda: CalendarAgendaPort | None = None,
    gmail: GmailPort | None = None,
    # Typed read ports. None binds the house Composio adapters from settings.
    linkedin: LinkedInPort | None = None,
    search_console: SearchConsolePort | None = None,
    ga4: Ga4Port | None = None,
    seo_audit: SeoAuditPort | None = None,
    instagram_insights: InstagramInsightsPort | None = None,
    research: ResearchPort | None = None,
    sheets: SheetsPort | None = None,
    embedding_port: EmbeddingPort | None = None,
    client: LlmClient | None = None,
    source_ref: str = "",
    now: datetime | None = None,
    deadline_at: float | None = None,
    input_source: str = "text",
    # Kept updated with tokens actually consumed while the loop runs, so a caller
    # that must catch an unexpected exception from this call can still record real
    # provider spend for the turn instead of losing it (see `OwnerUsage`).
    usage: OwnerUsage | None = None,
) -> OwnerBrainResult:
    """Answer one owner message, preferring the agent and degrading to `fallback_text`."""
    if kill_switch or not settings.brain_ready():
        return OwnerBrainResult(fallback_text, False, (), fallback_reason="kill_switch_or_disabled")
    if task_type is OwnerTaskType.APPROVAL:
        # By design: approvals, takeover, scope, preferences never reach the model.
        return OwnerBrainResult(fallback_text, False, (), fallback_reason="deterministic_intent")
    agent_client = client or build_agent_client(settings)
    if not agent_client.enabled():
        return OwnerBrainResult(fallback_text, False, (), fallback_reason="no_model_configured")

    port = embedding_port or build_embedding_port(settings)
    moment = now or datetime.now(UTC)
    if requests_no_history(owner_text):
        history = ()
        context = None
    else:
        # Exactly one retrieval pass per owner turn.
        with owner_stage("retrieval", source_ref=source_ref):
            context = assemble_owner_context(
                brain,
                query=owner_text,
                embedding_port=port,
                max_chars=settings.memory_max_context_chars,
                weights=_weights(settings),
                now=moment,
            )
    house = bind_owner_house_ports(settings)
    ctx = ToolContext(
        principal=principal,
        store=store,
        brain=brain,
        settings=settings,
        embedding_port=port,
        calendar=calendar if calendar is not None else house["calendar"],
        calendar_agenda=(
            calendar_agenda if calendar_agenda is not None else house["calendar_agenda"]
        ),
        gmail=gmail if gmail is not None else house["gmail"],
        linkedin=linkedin if linkedin is not None else house["linkedin"],
        search_console=(search_console if search_console is not None else house["search_console"]),
        ga4=ga4 if ga4 is not None else house["ga4"],
        seo_audit=seo_audit if seo_audit is not None else house["seo_audit"],
        instagram_insights=(
            instagram_insights if instagram_insights is not None else house["instagram_insights"]
        ),
        research=research if research is not None else house["research"],
        sheets=sheets if sheets is not None else house["sheets"],
        kill_switch=kill_switch,
        demo_active=demo_active,
        source_ref=source_ref,
        owner_text=(raw_owner_request if raw_owner_request is not None else owner_text),
        now=moment,
    )
    outcome: AgentOutcome = run_owner_agent(
        client=agent_client,
        ctx=ctx,
        owner_message=owner_text,
        history=history,
        context=context,
        max_steps=max(1, settings.owner_agent_max_steps),
        now_line=hebrew_datetime(moment, timezone=settings.calendar_timezone),
        deadline_at=deadline_at,
        input_source=input_source,
        usage=usage,
    )
    model = getattr(agent_client, "last_model", "")
    if not outcome.completed or not outcome.text.strip():
        reason = outcome.error or "empty_reply"
        errors = getattr(agent_client, "errors", None)
        if errors:
            # Carry the per-model failure so a bad model id is diagnosable from one log
            # line instead of a day of guessing.
            reason = f"{reason} [{'; '.join(errors)[:300]}]"
        # The agent was allowed to run and genuinely failed (as opposed to never being
        # tried -- kill switch, a deterministic intent, or no model configured, all of
        # which return above this point). For an unclassified NOTE, the classifier's
        # "I couldn't classify your message" canned line is dishonest here: the message
        # WAS understood, or the agent would never have been invoked for it -- the live
        # read just failed. Every other task type keeps `fallback_text` untouched,
        # including read types (DAILY_BRIEF, CALENDAR, ...) whose fallback is already a
        # real computed answer, not a "could not classify" placeholder.
        text = (
            format_note_agent_failure(reason, outcome.completion)
            if task_type == OwnerTaskType.NOTE
            else fallback_text
        )
        return OwnerBrainResult(
            text,
            False,
            outcome.tools_used,
            # The agent may have already spent real provider tokens across one or
            # more completed model calls before it failed (provider error, budget
            # exhausted, refusal, ...). Omitting these here used to silently
            # report 0 for every failed-but-attempted turn -- far more common
            # than an outright exception -- so the audit trail undercounted
            # spend on exactly the turns worth auditing.
            outcome.tokens_in,
            outcome.tokens_out,
            fallback_reason=reason,
            model=model,
            steps=outcome.steps_used,
            tools_failed=outcome.tools_failed,
            completion=outcome.completion,
            approval_ids=outcome.approval_ids,
        )
    return OwnerBrainResult(
        outcome.text.strip(),
        True,
        outcome.tools_used,
        outcome.tokens_in,
        outcome.tokens_out,
        model=model,
        steps=outcome.steps_used,
        tools_failed=outcome.tools_failed,
        completion=outcome.completion,
        approval_ids=outcome.approval_ids,
    )
