"""Wiring between the owner inbound path and the brain.

Deliberately surgical. The agent loop takes over **reads and free conversation**, which is
where the keyword switchboard failed. Every intent that changes state — approvals, human
takeover, conversation scope, stored preferences, outreach drafts, meeting debriefs — stays
on the existing deterministic path, untouched, because that is where the safety properties
live.

If the agent is not configured, fails, refuses or runs out of steps, the caller keeps the
answer the deterministic path already produced. Assaf never sees an error from this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import NamedTuple

from app.brain.context import assemble_owner_context
from app.brain.embeddings import EmbeddingPort, build_embedding_port
from app.brain.extraction import consolidate, extract_candidates
from app.brain.retrieval import MemoryScoreWeights
from app.brain.schemas import MemorySource
from app.brain.store import BrainStore
from app.core.config import Settings
from app.core.models import model_chain
from app.db.store import LeadStore
from app.domain.memory import ConversationTurn
from app.domain.owner_tasks import OwnerTaskType
from app.graph.owner_agent import AgentOutcome, run_owner_agent
from app.integrations.calendar import CalendarPort
from app.integrations.ga4 import Ga4Port
from app.integrations.instagram_insights import InstagramInsightsPort
from app.integrations.linkedin import LinkedInPort
from app.integrations.linkedin_analytics import LinkedInAnalyticsPort
from app.integrations.llm_client import (
    GEMINI_CHAT_URL,
    OPENAI_CHAT_URL,
    LlmClient,
    LlmModelChain,
)
from app.integrations.meta_ads import MetaAdsPort
from app.integrations.research import ResearchPort
from app.integrations.search_console import SearchConsolePort
from app.integrations.seo_audit import SeoAuditPort
from app.integrations.telegram_format import hebrew_datetime
from app.tools.registries.owner_tools import ToolContext

# These intents mutate state or bind an approval. They keep the deterministic handler and
# never route through the model. Adding to this set is always the safe direction.
DETERMINISTIC_TASK_TYPES: frozenset[OwnerTaskType] = frozenset(
    {
        OwnerTaskType.APPROVAL,
        OwnerTaskType.PREFERENCE,
        OwnerTaskType.HUMAN_TAKEOVER,
        OwnerTaskType.HUMAN_TAKEOVER_RESUME,
        OwnerTaskType.CONVERSATION_SCOPE,
        OwnerTaskType.LEAD_OUTREACH,
        OwnerTaskType.MEETING_DEBRIEF,
    }
)


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


def agent_allowed_for(task_type: OwnerTaskType) -> bool:
    """True when the agent may answer this intent instead of the canned handler."""
    return task_type not in DETERMINISTIC_TASK_TYPES


def build_agent_client(settings: Settings) -> LlmModelChain:
    """Every configured model, in order.

    `MIA_OWNER_AGENT_FALLBACK_MODEL` was documented but previously ignored — only
    `chain[0]` was ever used. A primary the account cannot call therefore dropped straight
    to the keyword classifier instead of trying the secondary.
    """
    chain = model_chain(settings.owner_agent_model, settings.owner_agent_fallback_model)
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


def answer_owner(
    *,
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
    # Typed read ports, passed straight through to the tool registry. Any left None makes
    # its tool answer "not connected" instead of failing the turn.
    linkedin: LinkedInPort | None = None,
    linkedin_analytics: LinkedInAnalyticsPort | None = None,
    search_console: SearchConsolePort | None = None,
    ga4: Ga4Port | None = None,
    seo_audit: SeoAuditPort | None = None,
    instagram_insights: InstagramInsightsPort | None = None,
    research: ResearchPort | None = None,
    meta_ads: MetaAdsPort | None = None,
    embedding_port: EmbeddingPort | None = None,
    client: LlmClient | None = None,
    source_ref: str = "",
    now: datetime | None = None,
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
    context = assemble_owner_context(
        brain,
        query=owner_text,
        embedding_port=port,
        max_chars=settings.memory_max_context_chars,
        weights=_weights(settings),
        now=moment,
    )
    ctx = ToolContext(
        store=store,
        brain=brain,
        settings=settings,
        embedding_port=port,
        calendar=calendar,
        linkedin=linkedin,
        linkedin_analytics=linkedin_analytics,
        search_console=search_console,
        ga4=ga4,
        seo_audit=seo_audit,
        instagram_insights=instagram_insights,
        research=research,
        meta_ads=meta_ads,
        kill_switch=kill_switch,
        demo_active=demo_active,
        source_ref=source_ref,
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
        return OwnerBrainResult(
            fallback_text, False, (), fallback_reason=reason, model=model
        )
    return OwnerBrainResult(
        outcome.text.strip(),
        True,
        outcome.tools_used,
        outcome.tokens_in,
        outcome.tokens_out,
        model=model,
    )


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
    result = extract_candidates(
        extraction_client, owner_message=owner_text, history=history
    )
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
