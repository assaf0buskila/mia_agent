"""Wiring between the owner inbound path and the brain.

Deliberately surgical. The agent loop takes over **reads and free conversation**, which is
where the keyword switchboard failed. Every high-risk or approval-bound intent that changes
state — approvals, human takeover, conversation scope, stored preferences, outreach drafts,
meeting debriefs — stays
on the existing deterministic path, untouched, because that is where the safety properties
live.

If the agent is not configured, fails, refuses or runs out of steps, the caller keeps the
answer the deterministic path already produced. Assaf never sees an error from this module.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, NamedTuple

from app.agents.owner.graph import compile_owner_graph
from app.agents.shared.state import OwnerState
from app.brain.context import (
    KNOWLEDGE_BUDGET_SHARE,
    PROFILE_BUDGET_SHARE,
    assemble_owner_context,
    build_profile_block,
)
from app.brain.embeddings import EmbeddingPort, build_embedding_port
from app.brain.extraction import consolidate, extract_candidates
from app.brain.retrieval import MemoryScoreWeights, fit_to_budget
from app.brain.schemas import BrainContext, MemorySource, RetrievedItem
from app.brain.store import BrainStore
from app.capabilities.knowledge import knowledge_handlers
from app.capabilities.memory import memory_handlers
from app.capabilities.policy import execute_capability
from app.capabilities.types import Principal
from app.channels.telegram import message_to_owner_state
from app.core.config import Settings
from app.core.errors import MiaError
from app.core.models import model_chain
from app.db.store import LeadStore
from app.domain.memory import ConversationTurn
from app.domain.owner.tasks import OwnerTaskType
from app.graph.owner_agent import AgentOutcome, run_owner_agent
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
    OPENAI_CHAT_URL,
    LlmClient,
    LlmModelChain,
)
from app.integrations.research import ResearchPort, build_research_port
from app.integrations.search_console import SearchConsolePort, build_search_console_port
from app.integrations.seo_audit import SeoAuditPort, build_seo_audit_port
from app.integrations.sheets import SheetsPort, build_sheets_port
from app.integrations.telegram_format import hebrew_datetime
from app.tools.registries.owner_tools import ToolContext

# These approval/high-risk intents mutate state or bind an approval. They keep the deterministic
# handler and never route through the model. ADR-042's bounded Sheets values tools are the narrow
# low-risk exception, with deterministic allowlist, intent, policy and idempotency guards.
DETERMINISTIC_TASK_TYPES: frozenset[OwnerTaskType] = frozenset(
    {
        OwnerTaskType.APPROVAL,
        OwnerTaskType.PREFERENCE,
        OwnerTaskType.HUMAN_TAKEOVER,
        OwnerTaskType.HUMAN_TAKEOVER_RESUME,
        OwnerTaskType.CONVERSATION_SCOPE,
        OwnerTaskType.LEAD_OUTREACH,
        OwnerTaskType.MEETING_DEBRIEF,
        OwnerTaskType.GMAIL_DRAFT,
        OwnerTaskType.OWNER_STATUS,
    }
)

# The honest failure line for a NOTE turn the agent was allowed to run but could not
# complete (provider error, refusal, truncation, empty reply, budget/ceiling exhausted).
# Deliberately not "מה שהבנתי" -- that phrase means "I couldn't classify this", which is
# false once the agent was actually invoked. One line, no apology, no internals, no
# secrets. The parenthetical is a failure *class* Assaf can report (provider / empty /
# timeout), never a model id or key.
NOTE_AGENT_FAILURE_TEXT = "הבדיקה לא עברה כרגע. תנסה שוב."

_NOTE_FAILURE_CLASSES: tuple[tuple[str, str], ...] = (
    ("empty_reply", "תשובה ריקה"),
    ("empty reply", "תשובה ריקה"),
    ("timeout", "תם הזמן"),
    ("timed out", "תם הזמן"),
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

# `fallback_reason` prefixes for the two ways OwnerGraph can fail to produce an answer.
# Both degrade to the deterministic ack the keyword classifier already computed. Neither
# runs the model a second time: a failed turn must never be billed twice.
GRAPH_FAILURE_REASON = "owner_graph_failed"
GRAPH_NO_RESULT_REASON = "owner_graph_no_result"


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


def agent_allowed_for(task_type: OwnerTaskType) -> bool:
    """True when the agent may answer this intent instead of the canned handler."""
    return task_type not in DETERMINISTIC_TASK_TYPES


def build_agent_client(settings: Settings) -> LlmModelChain:
    """Every configured model, in order.

    `MIA_OWNER_AGENT_FALLBACK_MODEL` was documented but previously ignored — only
    `chain[0]` was ever used. A primary the account cannot call therefore dropped straight
    to the keyword classifier instead of trying the secondary.

    The live sales model is appended after the dedicated owner ids: website Ask Mia
    already proves that pair is callable, so a broken owner-agent id must not take the
    whole Telegram console down with it.
    """
    chain = model_chain(
        settings.owner_agent_model,
        settings.owner_agent_fallback_model,
        settings.sales_model,
        settings.sales_fallback_model,
    )
    clients = [
        LlmClient(api_key=settings.openai_api_key, model=name, url=OPENAI_CHAT_URL)
        for name in chain
    ]
    clients.extend(_gemini_clients(settings, settings.owner_agent_gemini_model))
    return LlmModelChain(clients)


def _gemini_clients(settings: Settings, model: str) -> list[LlmClient]:
    """Gemini OpenAI-compat as the cross-provider last resort.

    Same `tools` wire shape as Chat Completions, so the agent loop needs no changes. It is
    last on purpose: the compatibility layer silently ignores parameters it does not
    support, so it is a safety net for an OpenAI-side outage or model block, not a peer.
    """
    key = settings.gemini_api_key.strip()
    name = model.strip()
    if not key or not name:
        return []
    return [LlmClient(api_key=key, model=name, url=GEMINI_CHAT_URL)]


def build_extraction_client(settings: Settings) -> LlmModelChain:
    """Extraction with the same OpenAI-then-Gemini shape as the agent.

    Gemini's compat layer does not document raw `response_format: json_schema`, so a
    silently-ignored schema is possible. `parse_extraction` already validates the payload
    and returns nothing on a mismatch, which is the correct degradation: no memory written
    rather than junk memory written.
    """
    clients = []
    if settings.extraction_model.strip():
        clients.append(
            LlmClient(
                api_key=settings.openai_api_key,
                model=settings.extraction_model.strip(),
                url=OPENAI_CHAT_URL,
            )
        )
    clients.extend(_gemini_clients(settings, settings.owner_agent_gemini_model))
    return LlmModelChain(clients)


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
    # The OwnerGraph state for this turn. When the graph's retrieve node has already run,
    # its hits ARE the context -- assembling a second one here is what used to pay for
    # retrieval twice on every owner message.
    graph_state: Mapping[str, Any] | None = None,
) -> OwnerBrainResult:
    """Answer one owner message, preferring the agent and degrading to `fallback_text`."""
    if kill_switch or not settings.brain_ready():
        return OwnerBrainResult(fallback_text, False, (), fallback_reason="kill_switch_or_disabled")
    if not agent_allowed_for(task_type):
        # By design: approvals, takeover, scope, preferences never reach the model.
        return OwnerBrainResult(fallback_text, False, (), fallback_reason="deterministic_intent")
    agent_client = client or build_agent_client(settings)
    if not agent_client.enabled():
        return OwnerBrainResult(fallback_text, False, (), fallback_reason="no_model_configured")

    port = embedding_port or build_embedding_port(settings)
    moment = now or datetime.now(UTC)
    if graph_state is not None and graph_state.get("retrieval_done"):
        # Exactly one retrieval pass per turn: the graph already did it, through policy.
        context = owner_context_from_state(graph_state)
    else:
        # No graph state (a direct caller, or the retrieve node never ran because no
        # brain/settings were wired into `run_owner_turn`). Retrieve here instead -- still
        # once.
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
        search_console=(
            search_console if search_console is not None else house["search_console"]
        ),
        ga4=ga4 if ga4 is not None else house["ga4"],
        seo_audit=seo_audit if seo_audit is not None else house["seo_audit"],
        instagram_insights=(
            instagram_insights
            if instagram_insights is not None
            else house["instagram_insights"]
        ),
        research=research if research is not None else house["research"],
        sheets=sheets if sheets is not None else house["sheets"],
        kill_switch=kill_switch,
        demo_active=demo_active,
        source_ref=source_ref,
        owner_text=owner_text,
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


def _compact_hits(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    hits: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        hits.append(
            {
                "id": str(item.get("id") or ""),
                "label": str(item.get("label") or ""),
                "text": str(item.get("text") or "")[:500],
            }
        )
    return hits


def _as_items(hits: list[dict[str, str]], *, origin: str) -> list[RetrievedItem]:
    """Compact state hits -> the shape the budget and the prompt renderer speak."""
    return [
        RetrievedItem(
            item_id=str(hit.get("id") or ""),
            text=str(hit.get("text") or ""),
            origin=origin,
            label=str(hit.get("label") or ""),
        )
        for hit in hits
    ]


def _fit_hits(
    hits: list[dict[str, str]], *, origin: str, max_chars: int
) -> tuple[list[dict[str, str]], int]:
    """Trim to the same character budget `assemble_owner_context` applies."""
    kept, used = fit_to_budget(_as_items(hits, origin=origin), max_chars=max_chars)
    return (
        [{"id": item.item_id, "label": item.label, "text": item.text} for item in kept],
        used,
    )


def owner_context_from_state(state: Mapping[str, Any]) -> BrainContext:
    """Rebuild the model's context from what the retrieve node already put in state.

    This is the whole point of the retrieve node: what it found is what the answer sees.
    Before this existed, the node's hits were written to state and then thrown away, and
    `answer_owner` ran the identical retrieval a second time.
    """
    return BrainContext(
        profile=str(state.get("profile") or ""),
        memories=tuple(_as_items(_compact_hits(state.get("memory_hits")), origin="memory")),
        knowledge=tuple(_as_items(_compact_hits(state.get("knowledge_hits")), origin="knowledge")),
        open_questions=tuple(str(item) for item in (state.get("open_questions") or [])),
        used_chars=int(state.get("context_chars") or 0),
        degraded=bool(state.get("context_degraded")),
    )


def retrieve_owner_context(
    state: Mapping[str, Any],
    *,
    principal: Principal,
    brain: BrainStore,
    settings: Settings,
    embedding_port: EmbeddingPort | None = None,
) -> dict:
    """OwnerGraph retrieve node: the turn's single retrieval pass.

    `memory.search` and `knowledge.search` go through policy, then the always-on profile
    and the open questions are added, everything is fitted to the context budget, and the
    result is written to state. `answer_owner` reads it from there.

    `retrieval_done` is the contract: True only when at least one search actually
    completed. If both were denied or unavailable, the responder is told nothing was
    retrieved and assembles its own context rather than answering context-blind.
    """
    query = (state.get("latest_message") or "").strip()
    tools = list(state.get("tools_used") or [])
    if not query or bool(state.get("kill_switch")):
        return {
            "tools_used": tools,
            "memory_hits": [],
            "knowledge_hits": [],
            "profile": "",
            "open_questions": [],
            "context_chars": 0,
            "context_degraded": False,
            "retrieval_done": False,
        }
    port = embedding_port or build_embedding_port(settings)
    memory_hits: list[dict[str, str]] = []
    knowledge_hits: list[dict[str, str]] = []
    searched = False
    try:
        memory_out = execute_capability(
            "memory.search",
            principal=principal,
            args={"query": query},
            handlers=memory_handlers(
                brain=brain,
                embedding_port=port,
                weights=_weights(settings),
            ),
            kill_switch=bool(state.get("kill_switch")),
        )
        memory_hits = _compact_hits(memory_out.get("hits"))
        searched = True
        if "memory.search" not in tools:
            tools.append("memory.search")
    except MiaError:
        pass
    try:
        knowledge_out = execute_capability(
            "knowledge.search",
            principal=principal,
            args={"query": query},
            handlers=knowledge_handlers(brain=brain, embedding_port=port),
            kill_switch=bool(state.get("kill_switch")),
        )
        knowledge_hits = _compact_hits(knowledge_out.get("hits"))
        searched = True
        if "knowledge.search" not in tools:
            tools.append("knowledge.search")
    except MiaError:
        pass

    # The always-on profile is not a search -- no query, no embedding -- so it is read
    # directly. Its facts are excluded from the retrieved memories: repeating one fact in
    # two sections wastes budget and makes it look like two independent sources.
    max_chars = max(0, settings.memory_max_context_chars)
    profile, profile_ids = build_profile_block(
        brain, max_chars=int(max_chars * PROFILE_BUDGET_SHARE)
    )
    memory_hits = [hit for hit in memory_hits if hit["id"] not in profile_ids]
    remaining = max(0, max_chars - len(profile))
    knowledge_budget = int(remaining * KNOWLEDGE_BUDGET_SHARE)
    memory_hits, memory_used = _fit_hits(
        memory_hits, origin="memory", max_chars=remaining - knowledge_budget
    )
    knowledge_hits, knowledge_used = _fit_hits(
        knowledge_hits, origin="knowledge", max_chars=knowledge_budget
    )
    if memory_hits:
        # Recency decays over last access, so the memories actually shown must be touched.
        brain.touch_memories([hit["id"] for hit in memory_hits])
    return {
        "tools_used": tools,
        "memory_hits": memory_hits,
        "knowledge_hits": knowledge_hits,
        "profile": profile,
        "open_questions": [gap.question for gap in brain.list_open_gaps(limit=3)],
        "context_chars": len(profile) + memory_used + knowledge_used,
        "context_degraded": not port.enabled(),
        "retrieval_done": searched,
    }


def _result_payload(result: OwnerBrainResult) -> dict[str, Any]:
    """The parts of the result that have no home among the graph's own state keys."""
    return {
        "used_agent": result.used_agent,
        "memories_written": result.memories_written,
        "fallback_reason": result.fallback_reason,
        "model": result.model,
        "steps": result.steps,
        "tools_failed": list(result.tools_failed),
        "completion": result.completion,
        "approval_ids": list(result.approval_ids),
    }


def _result_from_state(final: Mapping[str, Any]) -> OwnerBrainResult | None:
    """Read the turn's answer back off the returned final state. `None` if none is there.

    `text`, `tools_used` and the token counts come from the graph's own state keys, not
    from the payload, so a node that rewrites `reply` after `respond` genuinely changes
    the answer -- which is what "the graph owns the turn" has to mean.
    """
    payload = final.get("owner_result")
    if not isinstance(payload, dict):
        return None
    return OwnerBrainResult(
        text=str(final.get("reply") or ""),
        used_agent=bool(payload.get("used_agent")),
        tools_used=tuple(str(name) for name in (final.get("tools_used") or ())),
        tokens_in=int(final.get("tokens_in") or 0),
        tokens_out=int(final.get("tokens_out") or 0),
        memories_written=int(payload.get("memories_written") or 0),
        fallback_reason=str(payload.get("fallback_reason") or ""),
        model=str(payload.get("model") or ""),
        steps=int(payload.get("steps") or 0),
        tools_failed=tuple(str(name) for name in (payload.get("tools_failed") or ())),
        completion=str(payload.get("completion") or ""),
        approval_ids=tuple(str(value) for value in (payload.get("approval_ids") or ())),
    )


def run_owner_turn(
    *,
    principal: Principal,
    owner_id: str,
    telegram_chat_id: str,
    run_id: str,
    latest_message: str,
    kill_switch: bool,
    produce: Callable[[OwnerState], OwnerBrainResult],
    fallback_text: str = "",
    source: str = "text",
    brain: BrainStore | None = None,
    settings: Settings | None = None,
    embedding_port: EmbeddingPort | None = None,
) -> OwnerBrainResult:
    """Run the owner reply through OwnerGraph. Ports stay in the closure, not in state.

    The graph owns the turn: it retrieves once, hands that state to `produce`, and the
    answer is read back off the returned final state -- not smuggled out through a
    closure. If the graph raises, or returns without a result, the answer is
    `fallback_text`, the deterministic ack the keyword classifier already computed. The
    model is never called a second time to paper over a graph failure.
    """

    def retrieve(state: OwnerState) -> dict:
        if brain is None or settings is None:
            return {}
        return retrieve_owner_context(
            state,
            principal=principal,
            brain=brain,
            settings=settings,
            embedding_port=embedding_port,
        )

    def respond(state: OwnerState) -> dict:
        result = produce(state)
        used = list(state.get("tools_used") or [])
        for name in result.tools_used:
            if name not in used:
                used.append(name)
        return {
            "reply": result.text,
            "tools_used": used,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "owner_result": _result_payload(result),
        }

    try:
        final = compile_owner_graph(respond=respond, retrieve=retrieve).invoke(
            message_to_owner_state(
                run_id=run_id,
                owner_id=owner_id,
                chat_id=telegram_chat_id,
                text=latest_message,
                source=source,
                kill_switch=kill_switch,
            )
        )
    except Exception as exc:
        # Broad on purpose: a broken graph must degrade to the deterministic ack, not
        # surface a traceback in Telegram. Logged with the traceback so it stays fixable.
        _LOG.exception("owner_graph_failed run_id=%s error=%s", run_id, type(exc).__name__)
        return OwnerBrainResult(
            fallback_text,
            False,
            (),
            fallback_reason=f"{GRAPH_FAILURE_REASON}:{type(exc).__name__}",
        )
    result = _result_from_state(final)
    if result is None:
        _LOG.warning("owner_graph_no_result run_id=%s", run_id)
        return OwnerBrainResult(fallback_text, False, (), fallback_reason=GRAPH_NO_RESULT_REASON)
    return result


def learn_from_exchange(
    *,
    brain: BrainStore,
    settings: Settings,
    owner_text: str,
    history: tuple[ConversationTurn, ...] = (),
    embedding_port: EmbeddingPort | None = None,
    client: LlmClient | None = None,
    source_ref: str = "",
    kill_switch: bool = False,
    demo_active: bool = False,
) -> int:
    """Extract and consolidate durable facts from one owner message.

    Runs after the reply is composed so it never adds latency to what Assaf sees. Only
    owner-channel text reaches this function — a website visitor cannot write owner memory.
    Returns the number of memories added or updated.
    """
    if kill_switch or demo_active or not settings.memory_write_enabled:
        return 0
    if not settings.extraction_ready() or not owner_text.strip():
        return 0
    extraction_client = client or build_extraction_client(settings)
    if not extraction_client.enabled():
        return 0
    result = extract_candidates(extraction_client, owner_message=owner_text, history=history)
    if not result.candidates and not result.gaps:
        return 0
    port = embedding_port or build_embedding_port(settings)
    counts = consolidate(
        brain,
        candidates=result.candidates,
        embedding_port=port,
        client=extraction_client,
        source=MemorySource.TELEGRAM,
        source_ref=source_ref,
    )
    for question in result.gaps:
        brain.open_gap(topic=question[:120], question=question, priority=5)
    return counts.get("add", 0) + counts.get("update", 0) + counts.get("delete", 0)
