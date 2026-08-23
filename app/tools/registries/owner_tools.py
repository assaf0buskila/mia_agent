"""Allowlisted owner tool registry.

Every tool here is a **read**, or an owner-scoped memory write that cannot leave the
system. Nothing in this registry sends a message, books, approves, spends, publishes or
deletes. Writes that matter still go through `app/domain/approvals.py` and
`app/core/risk.py`, exactly as they did before the agent loop existed.

The allowlist is enforced on the tool name **returned by the model**, not by asking the
API to restrict itself: `allowed_tools` is not documented for Chat Completions, and the
Gemini compatibility layer silently ignores parameters it does not support. Server-side
name validation is the only enforcement that actually holds.

Adding a capability is one `_register(...)` call plus a handler with a JSON schema. Under
`strict: true` every property must be listed in `required`, `additionalProperties` must be
false, and optional arguments are expressed as a `["type","null"]` union.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.brain.context import retrieve_knowledge, retrieve_memories
from app.brain.embeddings import EmbeddingPort
from app.brain.retrieval import MemoryScoreWeights
from app.brain.schemas import (
    MemoryCategory,
    MemoryKind,
    MemorySource,
    clamp_importance,
)
from app.brain.store import BrainStore
from app.core.config import Settings
from app.db.store import LeadStore
from app.domain.briefs import apply_owner_meeting_brief
from app.domain.content_ideas import apply_owner_content_ideas
from app.domain.gmail_summaries import apply_owner_gmail_summary
from app.domain.hot_handoff import format_hot_leads_ack
from app.domain.lead_reviews import apply_owner_lead_review
from app.domain.owner_briefs import apply_owner_brief
from app.domain.owner_calendar import apply_owner_calendar
from app.domain.owner_notify import apply_owner_notify
from app.domain.owner_reads import (
    format_pending_approvals_ack,
    format_website_conversations_ack,
)
from app.domain.owner_snapshot import format_operator_snapshot_ack
from app.domain.owner_status import format_owner_status_ack
from app.domain.owner_weeklies import apply_owner_weekly
from app.domain.seo import enrich_seo_ack
from app.integrations.calendar import CalendarPort
from app.integrations.ga4 import Ga4Port
from app.integrations.instagram_insights import (
    InstagramInsightsPort,
    enrich_content_insights_ack,
)
from app.integrations.linkedin import LinkedInPort, enrich_linkedin_ack
from app.integrations.linkedin_analytics import (
    LinkedInAnalyticsPort,
    enrich_linkedin_analytics_ack,
)
from app.integrations.llm_client import function_tool
from app.integrations.meta_ads import MetaAdsPort, enrich_analytics_ack
from app.integrations.research import ResearchPort, enrich_research_ack
from app.integrations.search_console import SearchConsolePort
from app.integrations.seo_audit import SeoAuditPort

MAX_TOOL_RESULT_CHARS = 3000
_NO_ARGS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}


def _string_arg(name: str, description: str, *, optional: bool = False) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            name: {
                "type": ["string", "null"] if optional else "string",
                "description": description,
            }
        },
        "required": [name],
        "additionalProperties": False,
    }


@dataclass
class ToolContext:
    """Everything a tool handler may touch. No secrets are exposed to the model."""

    store: LeadStore
    brain: BrainStore
    settings: Settings
    embedding_port: EmbeddingPort
    calendar: CalendarPort | None = None
    linkedin: LinkedInPort | None = None
    linkedin_analytics: LinkedInAnalyticsPort | None = None
    search_console: SearchConsolePort | None = None
    ga4: Ga4Port | None = None
    seo_audit: SeoAuditPort | None = None
    instagram_insights: InstagramInsightsPort | None = None
    research: ResearchPort | None = None
    meta_ads: MetaAdsPort | None = None
    kill_switch: bool = False
    demo_active: bool = False
    source_ref: str = ""
    now: datetime | None = None

    def timezone(self) -> str:
        return self.settings.calendar_timezone

    def weights(self) -> MemoryScoreWeights:
        return MemoryScoreWeights(
            relevance=self.settings.memory_weight_relevance,
            recency=self.settings.memory_weight_recency,
            importance=self.settings.memory_weight_importance,
        )


@dataclass
class ToolResult:
    ok: bool
    text: str = ""
    error: str = ""

    def payload(self) -> dict[str, Any]:
        if not self.ok:
            return {"ok": False, "error": self.error or "tool failed"}
        return {"ok": True, "result": self.text[:MAX_TOOL_RESULT_CHARS]}


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any]], ToolResult]
    writes_memory: bool = False


_REGISTRY: dict[str, ToolSpec] = {}
_ORDER: list[str] = []


def _register(spec: ToolSpec) -> None:
    _REGISTRY[spec.name] = spec
    _ORDER.append(spec.name)


def _empty(value: str | None, fallback: str) -> ToolResult:
    if value is None or not str(value).strip():
        return ToolResult(ok=True, text=fallback)
    return ToolResult(ok=True, text=str(value).strip())


# ------------------------------------------------------------------ brain tools


def _search_memory(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, error="query is required")
    hits = retrieve_memories(
        ctx.brain,
        query=query,
        embedding_port=ctx.embedding_port,
        weights=ctx.weights(),
        limit=8,
        now=ctx.now,
    )
    if not hits:
        return ToolResult(ok=True, text="No stored memory matches that.")
    ctx.brain.touch_memories([hit.item_id for hit in hits])
    lines = [f"- [{hit.label or 'memory'}] {hit.text}" for hit in hits]
    return ToolResult(ok=True, text="\n".join(lines))


def _search_knowledge(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, error="query is required")
    hits = retrieve_knowledge(
        ctx.brain, query=query, embedding_port=ctx.embedding_port, limit=5
    )
    if not hits:
        return ToolResult(ok=True, text="Nothing in the website knowledge base matches that.")
    lines = [f"- [{hit.label or 'site'}] {hit.text}" for hit in hits]
    return ToolResult(ok=True, text="\n".join(lines))


def _remember(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Owner-scoped memory write. R1: never leaves the system, so it needs no approval."""
    text = str(args.get("text") or "").strip()
    if not text:
        return ToolResult(ok=False, error="text is required")
    if not ctx.settings.memory_write_enabled:
        return ToolResult(ok=False, error="memory writing is disabled")
    try:
        kind = MemoryKind(str(args.get("kind") or MemoryKind.SEMANTIC.value))
    except ValueError:
        kind = MemoryKind.SEMANTIC
    try:
        category = MemoryCategory(str(args.get("category") or MemoryCategory.OTHER.value))
    except ValueError:
        category = MemoryCategory.OTHER
    vector = None
    if ctx.embedding_port.enabled():
        vectors = ctx.embedding_port.embed([text])
        vector = vectors[0] if vectors else None
    ctx.brain.save_memory(
        text=text,
        kind=kind,
        category=category,
        importance=clamp_importance(args.get("importance", 6)),
        source=MemorySource.TELEGRAM,
        source_ref=ctx.source_ref,
        embedding=vector,
        embedding_model=ctx.embedding_port.model,
    )
    return ToolResult(ok=True, text="Stored.")


def _list_known_entities(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    entities = ctx.brain.list_entities(limit=25)
    if not entities:
        return ToolResult(ok=True, text="No entities recorded yet.")
    lines = [
        f"- {entity.name} ({entity.kind.value}, mentioned {entity.mention_count}x)"
        for entity in entities
    ]
    return ToolResult(ok=True, text="\n".join(lines))


# ----------------------------------------------------------------- owner reads


def _daily_brief(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(
        apply_owner_brief(
            ctx.store,
            timezone=ctx.timezone(),
            kill_switch=ctx.kill_switch,
            demo_active=ctx.demo_active,
            now=ctx.now,
        ),
        "No activity recorded for today yet.",
    )


def _weekly_brief(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(
        apply_owner_weekly(
            ctx.store,
            timezone=ctx.timezone(),
            kill_switch=ctx.kill_switch,
            demo_active=ctx.demo_active,
            now=ctx.now,
        ),
        "No activity recorded for this week yet.",
    )


def _hot_leads(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(format_hot_leads_ack(ctx.store), "No hot leads right now.")


def _pending_approvals(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(format_pending_approvals_ack(ctx.store), "Nothing is waiting for approval.")


def _website_conversations(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(
        format_website_conversations_ack(ctx.store), "No website conversations yet."
    )


def _owner_status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(
        format_owner_status_ack(ctx.store, timezone=ctx.timezone()), "Nothing to report."
    )


def _operator_snapshot(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(
        format_operator_snapshot_ack(ctx.store, timezone=ctx.timezone()),
        "Nothing to report.",
    )


def _lead_review(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    lead_id = str(args.get("lead_id") or "").strip()
    if not lead_id:
        return ToolResult(ok=False, error="lead_id is required")
    return _empty(
        apply_owner_lead_review(
            ctx.store,
            text=lead_id,
            kill_switch=ctx.kill_switch,
            demo_active=ctx.demo_active,
        ),
        f"No lead found for {lead_id}.",
    )


def _meeting_brief(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    lead_id = str(args.get("lead_id") or "").strip()
    if not lead_id:
        return ToolResult(ok=False, error="lead_id is required")
    return _empty(
        apply_owner_meeting_brief(
            ctx.store,
            text=lead_id,
            timezone=ctx.timezone(),
            kill_switch=ctx.kill_switch,
            demo_active=ctx.demo_active,
        ),
        f"No meeting brief available for {lead_id}.",
    )


def _calendar_availability(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    if ctx.calendar is None:
        return ToolResult(ok=False, error="calendar is not configured")
    text, _outcome = apply_owner_calendar(
        "",
        ctx.calendar,
        kill_switch=ctx.kill_switch,
        timezone=ctx.timezone(),
        now=ctx.now,
        demo_active=ctx.demo_active,
    )
    return _empty(text, "No free slots found.")


def _booked_meetings(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(
        apply_owner_notify(
            ctx.store,
            timezone=ctx.timezone(),
            kill_switch=ctx.kill_switch,
            demo_active=ctx.demo_active,
        ),
        "Nothing new was booked.",
    )


def _content_ideas(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    return _empty(
        apply_owner_content_ideas(
            ctx.store,
            timezone=ctx.timezone(),
            kill_switch=ctx.kill_switch,
            demo_active=ctx.demo_active,
        ),
        "No content ideas available.",
    )


_NOT_CONNECTED = (
    "Not connected yet. Assaf needs to finish this integration in Composio / env."
)


def _gmail_summary(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip() or "סיכום מייל"
    ack = apply_owner_gmail_summary(
        ctx.store,
        text=query,
        kill_switch=ctx.kill_switch,
        demo_active=ctx.demo_active,
    )
    return _empty(ack, "No Gmail thread matched. Name a thread: or lead id.")


def _seo_snapshot(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    if ctx.search_console is None or ctx.ga4 is None or ctx.seo_audit is None:
        return ToolResult(ok=True, text=_NOT_CONNECTED)
    text, _outcomes = enrich_seo_ack(
        "",
        ctx.search_console,
        ctx.ga4,
        ctx.seo_audit,
        kill_switch=ctx.kill_switch,
        store=ctx.store,
        settings=ctx.settings,
        demo_active=ctx.demo_active,
    )
    return _empty(text, "SEO ports returned nothing. Check GSC site URL and GA4 property.")


def _linkedin_snapshot(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    if ctx.linkedin is None:
        return ToolResult(ok=True, text=_NOT_CONNECTED)
    text, _outcome = enrich_linkedin_ack("", ctx.linkedin, ctx.kill_switch)
    if ctx.linkedin_analytics is not None:
        text, _analytics = enrich_linkedin_analytics_ack(
            text,
            ctx.linkedin_analytics,
            ctx.kill_switch,
            now=ctx.now or datetime.now(UTC),
            timezone=ctx.timezone(),
        )
    return _empty(text, "LinkedIn returned nothing. Reconnect LinkedIn in Composio.")


def _instagram_insights(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    if ctx.instagram_insights is None:
        return ToolResult(ok=True, text=_NOT_CONNECTED)
    text, _outcome = enrich_content_insights_ack(
        "",
        ctx.instagram_insights,
        ctx.store,
        ctx.kill_switch,
    )
    return _empty(text, "Instagram insights returned nothing.")


def _research_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, error="query is required")
    if ctx.research is None:
        return ToolResult(ok=True, text=_NOT_CONNECTED)
    text, _outcome = enrich_research_ack(
        "",
        ctx.research,
        query=query,
        kill_switch=ctx.kill_switch,
    )
    return _empty(text, "Research search returned nothing. Check the Firecrawl key.")


def _ads_snapshot(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    if ctx.meta_ads is None:
        return ToolResult(ok=True, text=_NOT_CONNECTED)
    extras: list[Any] = []
    text, _outcome = enrich_analytics_ack(
        "",
        ctx.meta_ads,
        ctx.kill_switch,
        store=ctx.store,
        settings=ctx.settings,
        extra_outcomes=extras,
        inbound_id=ctx.source_ref,
    )
    return _empty(text, "Meta ads returned nothing.")


_register(
    ToolSpec(
        name="search_memory",
        description=(
            "Search everything Mia remembers about Assaf from past conversations: who he "
            "is, his businesses, projects, skills, goals, preferences, decisions and "
            "ongoing tasks. Use this before asking him anything about himself."
        ),
        parameters=_string_arg("query", "What to look for, in natural language."),
        handler=_search_memory,
    )
)
_register(
    ToolSpec(
        name="search_knowledge",
        description=(
            "Search the AssafWeb website knowledge base: services, pricing policy, work "
            "process, portfolio, FAQ, testimonials and contact details. Use this for "
            "anything about what Assaf sells or how he works with clients."
        ),
        parameters=_string_arg("query", "What to look for, in natural language."),
        handler=_search_knowledge,
    )
)
_register(
    ToolSpec(
        name="remember",
        description=(
            "Store a durable fact about Assaf that you learned in this conversation and "
            "that is not already in memory. Only for information that will still matter "
            "in a month. Do not store small talk or anything he only asked about."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The fact as one self-contained sentence.",
                },
                "kind": {
                    "type": "string",
                    "enum": [item.value for item in MemoryKind],
                    "description": "semantic for stable facts, working for active work.",
                },
                "category": {
                    "type": "string",
                    "enum": [item.value for item in MemoryCategory],
                },
                "importance": {
                    "type": "integer",
                    "description": "1 mundane to 10 defining. Most facts are 4-7.",
                },
            },
            "required": ["text", "kind", "category", "importance"],
            "additionalProperties": False,
        },
        handler=_remember,
        writes_memory=True,
    )
)
_register(
    ToolSpec(
        name="list_known_entities",
        description=(
            "List the people, companies, products, projects and technologies Mia has "
            "recorded around Assaf, most-mentioned first."
        ),
        parameters=_NO_ARGS,
        handler=_list_known_entities,
    )
)
_register(
    ToolSpec(
        name="daily_brief",
        description="Today's scorecard: what happened, what the website produced.",
        parameters=_NO_ARGS,
        handler=_daily_brief,
    )
)
_register(
    ToolSpec(
        name="weekly_brief",
        description="This week's scorecard.",
        parameters=_NO_ARGS,
        handler=_weekly_brief,
    )
)
_register(
    ToolSpec(
        name="hot_leads",
        description="Leads that need Assaf personally, right now.",
        parameters=_NO_ARGS,
        handler=_hot_leads,
    )
)
_register(
    ToolSpec(
        name="pending_approvals",
        description="Everything waiting for Assaf's approval.",
        parameters=_NO_ARGS,
        handler=_pending_approvals,
    )
)
_register(
    ToolSpec(
        name="website_conversations",
        description="Website sales conversations ranked by how deep the discovery got.",
        parameters=_NO_ARGS,
        handler=_website_conversations,
    )
)
_register(
    ToolSpec(
        name="operator_snapshot",
        description=(
            "One combined operational picture: daily counts, pending approvals, website "
            "conversations and hot leads. Use for a broad 'what is going on' question."
        ),
        parameters=_NO_ARGS,
        handler=_operator_snapshot,
    )
)
_register(
    ToolSpec(
        name="owner_status",
        description="Short status digest. Use for a greeting or a bare status ping.",
        parameters=_NO_ARGS,
        handler=_owner_status,
    )
)
_register(
    ToolSpec(
        name="lead_review",
        description="Everything known about one lead. Needs the lead id.",
        parameters=_string_arg("lead_id", "The lead id, e.g. lead_ab12cd34."),
        handler=_lead_review,
    )
)
_register(
    ToolSpec(
        name="meeting_brief",
        description="Pre-meeting brief for one lead. Needs the lead id.",
        parameters=_string_arg("lead_id", "The lead id, e.g. lead_ab12cd34."),
        handler=_meeting_brief,
    )
)
_register(
    ToolSpec(
        name="calendar_availability",
        description="Free slots on Assaf's calendar. Read only; never books anything.",
        parameters=_NO_ARGS,
        handler=_calendar_availability,
    )
)
_register(
    ToolSpec(
        name="booked_meetings",
        description="Meetings that were booked recently.",
        parameters=_NO_ARGS,
        handler=_booked_meetings,
    )
)
_register(
    ToolSpec(
        name="content_ideas",
        description="Content ideas derived from real conversations and performance.",
        parameters=_NO_ARGS,
        handler=_content_ideas,
    )
)
_register(
    ToolSpec(
        name="gmail_summary",
        description=(
            "Summarize a Gmail thread already ingested for Assaf. Pass thread:ID or a "
            "lead id. Read only; never sends."
        ),
        parameters=_string_arg(
            "query",
            "Thread id, lead id, or the owner's request text.",
            optional=True,
        ),
        handler=_gmail_summary,
    )
)
_register(
    ToolSpec(
        name="seo_snapshot",
        description="Search Console, GA4 and homepage audit. Read only; never edits the site.",
        parameters=_NO_ARGS,
        handler=_seo_snapshot,
    )
)
_register(
    ToolSpec(
        name="linkedin_snapshot",
        description=(
            "Assaf's LinkedIn profile via Composio. Never posts or DMs. Personal post "
            "analytics is unavailable — Composio has no member analytics tool."
        ),
        parameters=_NO_ARGS,
        handler=_linkedin_snapshot,
    )
)
_register(
    ToolSpec(
        name="instagram_insights",
        description="Organic Instagram content insights. Never replies or publishes.",
        parameters=_NO_ARGS,
        handler=_instagram_insights,
    )
)
_register(
    ToolSpec(
        name="research_search",
        description="Public web search via Firecrawl. Query must be explicit. Never crawls a site.",
        parameters=_string_arg("query", "Search query, usually a company domain or topic."),
        handler=_research_search,
    )
)
_register(
    ToolSpec(
        name="ads_snapshot",
        description="Meta ads read snapshot. Never changes budget, bids or launches.",
        parameters=_NO_ARGS,
        handler=_ads_snapshot,
    )
)


def tool_names() -> tuple[str, ...]:
    return tuple(_ORDER)


def get_tool(name: str) -> ToolSpec | None:
    """The allowlist gate. An unknown name returns None and the call is refused."""
    return _REGISTRY.get(name)


def tool_definitions(*, allow_memory_writes: bool = True) -> list[dict[str, Any]]:
    """Chat Completions `tools` payload for the registry."""
    definitions: list[dict[str, Any]] = []
    for name in _ORDER:
        spec = _REGISTRY[name]
        if spec.writes_memory and not allow_memory_writes:
            continue
        definitions.append(
            function_tool(
                name=spec.name,
                description=spec.description,
                parameters=spec.parameters,
            )
        )
    return definitions


def execute_tool(name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Run one allowlisted tool. An unknown name is refused, never guessed at."""
    spec = get_tool(name)
    if spec is None:
        return ToolResult(ok=False, error=f"unknown tool: {name}")
    if spec.writes_memory and not ctx.settings.memory_write_enabled:
        return ToolResult(ok=False, error="memory writing is disabled")
    try:
        return spec.handler(ctx, arguments or {})
    except Exception as exc:  # noqa: BLE001 - one bad tool must not kill the turn
        return ToolResult(ok=False, error=f"{type(exc).__name__}")


def utc_now() -> datetime:
    return datetime.now(UTC)
