from enum import StrEnum

from pydantic import BaseModel, Field


class CapabilityId(StrEnum):
    HTTP_API = "http_api"
    CONFIG = "config"
    CAPABILITY_REGISTRY = "capability_registry"
    OBSERVABILITY = "observability"
    RISK_POLICY = "risk_policy"
    CANONICAL_EVENTS = "canonical_events"
    IDENTITY = "identity"
    SALES_STATE = "sales_state"
    SALES_REPLY = "sales_reply"
    HUMANITY_LINTER = "humanity_linter"
    WEBSITE = "website"
    VOICE_STT = "voice_stt"
    TELEGRAM = "telegram"
    INSTAGRAM = "instagram"
    GMAIL = "gmail"
    CALENDAR = "calendar"
    CONTENT_PERFORMANCE = "content_performance"
    CONTENT_IDEAS = "content_ideas"
    RESEARCH = "research"
    SEARCH_CONSOLE = "search_console"
    GA4 = "ga4"
    SEO_AUDIT = "seo_audit"
    LINKEDIN = "linkedin"
    OWNER_BRIEF = "owner_brief"
    OWNER_WEEKLY = "owner_weekly"
    DEMO_MODE = "demo_mode"
    FOLLOW_UP = "follow_up"
    DUE_SCAN = "due_scan"
    MEETING_BRIEF = "meeting_brief"
    MEETING_DEBRIEF = "meeting_debrief"
    LEAD_REVIEW = "lead_review"
    OWNER_CALENDAR = "owner_calendar"
    OWNER_NOTIFY = "owner_notify"
    GMAIL_SUMMARY = "gmail_summary"
    GMAIL_INBOX = "gmail_inbox"
    MEETINGS = "meetings"
    APPROVALS = "approvals"
    DEALS = "deals"
    AI_RUNS = "ai_runs"
    TOOL_RUNS = "tool_runs"
    AWS_RUNTIME = "aws_runtime"
    FDE_EXECUTION_POLICY = "fde_execution_policy"
    FDE_SHADOW = "fde_shadow"
    FDE_VALUE = "fde_value"
    FDE_FAILURE_POLICY = "fde_failure_policy"
    FDE_IDEMPOTENCY = "fde_idempotency"
    FRESHNESS_POLICY = "freshness_policy"
    BRAIN_MEMORY = "brain_memory"
    BRAIN_KNOWLEDGE = "brain_knowledge"
    BRAIN_RETRIEVAL = "brain_retrieval"
    OWNER_AGENT = "owner_agent"
    EMBEDDINGS = "embeddings"


class WiringStatus(StrEnum):
    SPECIFIED = "specified"
    WIRED = "wired"
    ALIVE = "alive"


class Capability(BaseModel):
    id: CapabilityId
    prd: str
    status: WiringStatus
    port: str = Field(description="Module that owns this capability's contract")


# Keep in sync with docs/PRODUCT.md and docs/ARCHITECTURE.md.
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        id=CapabilityId.HTTP_API,
        prd="§30",
        status=WiringStatus.ALIVE,
        port="app.main",
    ),
    Capability(
        id=CapabilityId.CONFIG,
        prd="§41",
        status=WiringStatus.ALIVE,
        port="app.core.config",
    ),
    Capability(
        id=CapabilityId.CAPABILITY_REGISTRY,
        prd="Feature wiring status",
        status=WiringStatus.ALIVE,
        port="app.core.capabilities",
    ),
    Capability(
        id=CapabilityId.OBSERVABILITY,
        prd="§34, §36",
        status=WiringStatus.ALIVE,
        port="app.core.logging",
    ),
    Capability(
        id=CapabilityId.RISK_POLICY,
        prd="§33, §34",
        status=WiringStatus.ALIVE,
        port="app.core.risk",
    ),
    Capability(
        id=CapabilityId.CANONICAL_EVENTS,
        prd="§8, §31",
        status=WiringStatus.ALIVE,
        port="app.domain.events",
    ),
    Capability(
        id=CapabilityId.IDENTITY,
        prd="§8",
        status=WiringStatus.ALIVE,
        port="app.domain.identity",
    ),
    Capability(
        id=CapabilityId.SALES_STATE,
        prd="v2 durable historical lead state",
        status=WiringStatus.ALIVE,
        port="app.db.store",
    ),
    Capability(
        id=CapabilityId.SALES_REPLY,
        prd="§9",
        status=WiringStatus.ALIVE,
        port="app.surfaces.site_v2",
    ),
    Capability(
        id=CapabilityId.HUMANITY_LINTER,
        prd="§9 / playbook §25",
        status=WiringStatus.ALIVE,
        port="app.domain.humanity",
    ),
    Capability(
        id=CapabilityId.WEBSITE,
        prd="§7, §30",
        status=WiringStatus.ALIVE,
        port="app.api.website",
    ),
    Capability(
        id=CapabilityId.VOICE_STT,
        prd="§17.3",
        status=WiringStatus.ALIVE,
        port="app.integrations.transcribe",
    ),
    Capability(
        id=CapabilityId.TELEGRAM,
        prd="ADR-017",
        status=WiringStatus.ALIVE,
        port="app.integrations.telegram",
    ),
    Capability(
        id=CapabilityId.INSTAGRAM,
        prd="§16",
        status=WiringStatus.ALIVE,
        port="app.integrations.instagram_insights",
    ),
    Capability(
        id=CapabilityId.GMAIL,
        prd="§18",
        status=WiringStatus.ALIVE,
        port="app.integrations.gmail",
    ),
    Capability(
        id=CapabilityId.GMAIL_SUMMARY,
        prd="§18.1",
        status=WiringStatus.ALIVE,
        port="app.domain.gmail.summaries",
    ),
    Capability(
        id=CapabilityId.GMAIL_INBOX,
        prd="§18 / ADR-030",
        status=WiringStatus.ALIVE,
        port="app.integrations.gmail",
    ),
    Capability(
        id=CapabilityId.CALENDAR,
        prd="§18",
        status=WiringStatus.ALIVE,
        port="app.integrations.calendar",
    ),
    Capability(
        id=CapabilityId.CONTENT_PERFORMANCE,
        prd="§16 / §19",
        status=WiringStatus.ALIVE,
        port="app.integrations.instagram_insights",
    ),
    Capability(
        id=CapabilityId.CONTENT_IDEAS,
        prd="§2.2",
        status=WiringStatus.ALIVE,
        port="app.domain.content_ideas",
    ),
    Capability(
        id=CapabilityId.RESEARCH,
        prd="§21",
        status=WiringStatus.ALIVE,
        port="app.integrations.research",
    ),
    Capability(
        id=CapabilityId.SEARCH_CONSOLE,
        prd="§7 / website SEO",
        status=WiringStatus.ALIVE,
        port="app.integrations.search_console",
    ),
    Capability(
        id=CapabilityId.GA4,
        prd="§7 / website SEO",
        status=WiringStatus.ALIVE,
        port="app.integrations.ga4",
    ),
    Capability(
        id=CapabilityId.SEO_AUDIT,
        prd="§7 / website SEO",
        status=WiringStatus.ALIVE,
        port="app.integrations.seo_audit",
    ),
    Capability(
        id=CapabilityId.LINKEDIN,
        prd="§21A",
        status=WiringStatus.ALIVE,
        port="app.integrations.linkedin",
    ),
    Capability(
        id=CapabilityId.OWNER_BRIEF,
        prd="§2.2 / §17",
        status=WiringStatus.ALIVE,
        port="app.domain.owner.briefs",
    ),
    Capability(
        id=CapabilityId.OWNER_WEEKLY,
        prd="§2.2 / §17",
        status=WiringStatus.ALIVE,
        port="app.domain.owner.weeklies",
    ),
    Capability(
        id=CapabilityId.DEMO_MODE,
        prd="§42",
        status=WiringStatus.ALIVE,
        port="app.core.demo",
    ),
    Capability(
        id=CapabilityId.FOLLOW_UP,
        prd="§12.1",
        status=WiringStatus.ALIVE,
        port="app.domain.followups",
    ),
    Capability(
        id=CapabilityId.DUE_SCAN,
        prd="§12.1, §12.4",
        status=WiringStatus.ALIVE,
        port="app.workers.due_scan",
    ),
    Capability(
        id=CapabilityId.MEETING_BRIEF,
        prd="§12.2",
        status=WiringStatus.ALIVE,
        port="app.domain.meetings.briefs",
    ),
    Capability(
        id=CapabilityId.MEETING_DEBRIEF,
        prd="§12.3",
        status=WiringStatus.ALIVE,
        port="app.domain.debriefs",
    ),
    Capability(
        id=CapabilityId.LEAD_REVIEW,
        prd="§2 / §17",
        status=WiringStatus.ALIVE,
        port="app.domain.lead_reviews",
    ),
    Capability(
        id=CapabilityId.OWNER_CALENDAR,
        prd="§17 / §18.2",
        status=WiringStatus.ALIVE,
        port="app.domain.owner.calendar",
    ),
    Capability(
        id=CapabilityId.OWNER_NOTIFY,
        prd="§12.2 / §26.2",
        status=WiringStatus.ALIVE,
        port="app.domain.owner.notifications",
    ),
    Capability(
        id=CapabilityId.MEETINGS,
        prd="§12.2 / §19",
        status=WiringStatus.ALIVE,
        port="app.domain.meetings.state",
    ),
    Capability(
        id=CapabilityId.APPROVALS,
        prd="§33",
        status=WiringStatus.ALIVE,
        port="app.domain.approvals",
    ),
    Capability(
        id=CapabilityId.DEALS,
        prd="§32",
        status=WiringStatus.ALIVE,
        port="app.domain.deals",
    ),
    Capability(
        id=CapabilityId.AI_RUNS,
        prd="§32, §36, §40.2",
        status=WiringStatus.ALIVE,
        port="app.domain.ai_runs",
    ),
    Capability(
        id=CapabilityId.TOOL_RUNS,
        prd="§32, §36",
        status=WiringStatus.ALIVE,
        port="app.domain.events",
    ),
    Capability(
        id=CapabilityId.AWS_RUNTIME,
        prd="§29",
        status=WiringStatus.SPECIFIED,
        port="app.infra",
    ),
    Capability(
        id=CapabilityId.FDE_EXECUTION_POLICY,
        prd="FDE operating layer",
        status=WiringStatus.ALIVE,
        port="app.domain.policies.execution_policy",
    ),
    Capability(
        id=CapabilityId.FDE_SHADOW,
        prd="FDE operating layer",
        status=WiringStatus.ALIVE,
        port="app.domain.shadow",
    ),
    Capability(
        id=CapabilityId.FDE_VALUE,
        prd="FDE operating layer",
        status=WiringStatus.ALIVE,
        port="app.domain.value",
    ),
    Capability(
        id=CapabilityId.FDE_FAILURE_POLICY,
        prd="FDE operating layer",
        status=WiringStatus.ALIVE,
        port="app.domain.policies.failure_policy",
    ),
    Capability(
        id=CapabilityId.FDE_IDEMPOTENCY,
        prd="FDE operating layer",
        status=WiringStatus.ALIVE,
        port="app.domain.idempotency",
    ),
    Capability(
        id=CapabilityId.FRESHNESS_POLICY,
        prd="Pre-prod Adjustment N",
        status=WiringStatus.ALIVE,
        port="app.domain.policies.freshness",
    ),
    # Brain (ADR-026 / ADR-036). Memory counts on /health are alive. Knowledge ingest is
    # CLI-only. Embeddings, retrieval, and the owner agent stay wired-but-gated until
    # model ids are configured — do not call that ALIVE.
    Capability(
        id=CapabilityId.BRAIN_MEMORY,
        prd="ADR-026",
        status=WiringStatus.ALIVE,
        port="app.brain.store",
    ),
    Capability(
        id=CapabilityId.BRAIN_KNOWLEDGE,
        prd="ADR-026",
        status=WiringStatus.WIRED,
        port="app.brain.knowledge",
    ),
    Capability(
        id=CapabilityId.BRAIN_RETRIEVAL,
        prd="ADR-026",
        status=WiringStatus.WIRED,
        port="app.brain.retrieval",
    ),
    Capability(
        id=CapabilityId.EMBEDDINGS,
        prd="ADR-026",
        status=WiringStatus.WIRED,
        port="app.brain.embeddings",
    ),
    Capability(
        id=CapabilityId.OWNER_AGENT,
        prd="ADR-026",
        status=WiringStatus.WIRED,
        port="app.graph.owner_agent",
    ),
)


def capability_map() -> dict[str, str]:
    return {item.id.value: item.status.value for item in CAPABILITIES}


def require_alive(capability_id: CapabilityId) -> None:
    match = next(item for item in CAPABILITIES if item.id == capability_id)
    if match.status != WiringStatus.ALIVE:
        raise RuntimeError(f"{capability_id} is {match.status}, not alive")
