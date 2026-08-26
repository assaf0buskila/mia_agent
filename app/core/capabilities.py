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
    OWNER_REPLY = "owner_reply"
    HUMANITY_LINTER = "humanity_linter"
    LANGGRAPH = "langgraph"
    WEBSITE = "website"
    WHATSAPP = "whatsapp"
    VOICE_STT = "voice_stt"
    TELEGRAM = "telegram"
    INSTAGRAM = "instagram"
    MANYCHAT = "manychat"
    GMAIL = "gmail"
    CALENDAR = "calendar"
    SHEETS_MIRROR = "sheets_mirror"
    META_ADS = "meta_ads"
    CONTENT_PERFORMANCE = "content_performance"
    CONTENT_IDEAS = "content_ideas"
    CAMPAIGN_ANALYSIS = "campaign_analysis"
    CAMPAIGN_PACING = "campaign_pacing"
    CAMPAIGN_PRELAUNCH = "campaign_prelaunch"
    RESEARCH = "research"
    SEARCH_CONSOLE = "search_console"
    GA4 = "ga4"
    SEO_AUDIT = "seo_audit"
    LINKEDIN = "linkedin"
    OWNER_LEARNING = "owner_learning"
    OWNER_BRIEF = "owner_brief"
    OWNER_WEEKLY = "owner_weekly"
    GRAPH_LAB = "graph_lab"
    DEMO_MODE = "demo_mode"
    FOLLOW_UP = "follow_up"
    DUE_SCAN = "due_scan"
    RECONCILIATION = "reconciliation"
    CONVERSATION_KILL = "conversation_kill"
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
    FDE_FEEDBACK = "fde_feedback"
    FDE_VALUE = "fde_value"
    FDE_FAILURE_POLICY = "fde_failure_policy"
    FDE_HUMAN_TAKEOVER = "fde_human_takeover"
    FDE_IDEMPOTENCY = "fde_idempotency"
    PRELOADED_TOOLS = "preloaded_tools"
    MODEL_TASK_CLASSES = "model_task_classes"
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


# Keep in sync with docs/PRD.md Feature wiring and docs/PROJECT_MAP.md.
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
        prd="§10",
        status=WiringStatus.ALIVE,
        port="app.domain.sales",
    ),
    Capability(
        id=CapabilityId.SALES_REPLY,
        prd="§9",
        status=WiringStatus.ALIVE,
        port="app.integrations.sales_reply",
    ),
    Capability(
        id=CapabilityId.OWNER_REPLY,
        prd="ADR-025",
        status=WiringStatus.ALIVE,
        port="app.integrations.owner_reply",
    ),
    Capability(
        id=CapabilityId.HUMANITY_LINTER,
        prd="§9 / playbook §25",
        status=WiringStatus.ALIVE,
        port="app.domain.humanity",
    ),
    Capability(
        id=CapabilityId.LANGGRAPH,
        prd="§25–27",
        status=WiringStatus.ALIVE,
        port="app.graph.orchestrator",
    ),
    Capability(
        id=CapabilityId.WEBSITE,
        prd="§7, §30",
        status=WiringStatus.ALIVE,
        port="app.api.website",
    ),
    Capability(
        id=CapabilityId.WHATSAPP,
        prd="§17",
        status=WiringStatus.ALIVE,
        port="app.integrations.whatsapp",
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
        port="app.integrations.instagram",
    ),
    Capability(
        id=CapabilityId.MANYCHAT,
        prd="ADR-021 / ADR-033",
        status=WiringStatus.SPECIFIED,
        port="removed",
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
        port="app.domain.gmail_summaries",
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
        id=CapabilityId.SHEETS_MIRROR,
        prd="§19",
        status=WiringStatus.ALIVE,
        port="app.integrations.sheets",
    ),
    Capability(
        id=CapabilityId.META_ADS,
        prd="§20",
        status=WiringStatus.ALIVE,
        port="app.integrations.meta_ads",
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
        id=CapabilityId.CAMPAIGN_ANALYSIS,
        prd="§20.2",
        status=WiringStatus.ALIVE,
        port="app.domain.campaigns",
    ),
    Capability(
        id=CapabilityId.CAMPAIGN_PACING,
        prd="§19.2 / §20",
        status=WiringStatus.ALIVE,
        port="app.domain.pacing",
    ),
    Capability(
        id=CapabilityId.CAMPAIGN_PRELAUNCH,
        prd="§20.3",
        status=WiringStatus.ALIVE,
        port="app.domain.prelaunch",
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
        id=CapabilityId.OWNER_LEARNING,
        prd="§13",
        status=WiringStatus.ALIVE,
        port="app.domain.learning",
    ),
    Capability(
        id=CapabilityId.OWNER_BRIEF,
        prd="§2.2 / §17",
        status=WiringStatus.ALIVE,
        port="app.domain.owner_briefs",
    ),
    Capability(
        id=CapabilityId.OWNER_WEEKLY,
        prd="§2.2 / §17",
        status=WiringStatus.ALIVE,
        port="app.domain.owner_weeklies",
    ),
    Capability(
        id=CapabilityId.GRAPH_LAB,
        prd="§28",
        status=WiringStatus.ALIVE,
        port="app.evals",
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
        id=CapabilityId.RECONCILIATION,
        prd="§31.3",
        status=WiringStatus.ALIVE,
        port="app.domain.reconciliation",
    ),
    Capability(
        id=CapabilityId.CONVERSATION_KILL,
        prd="§34.2",
        status=WiringStatus.ALIVE,
        port="app.domain.conversation_kill",
    ),
    Capability(
        id=CapabilityId.MEETING_BRIEF,
        prd="§12.2",
        status=WiringStatus.ALIVE,
        port="app.domain.briefs",
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
        port="app.domain.owner_calendar",
    ),
    Capability(
        id=CapabilityId.OWNER_NOTIFY,
        prd="§12.2 / §26.2",
        status=WiringStatus.ALIVE,
        port="app.domain.owner_notify",
    ),
    Capability(
        id=CapabilityId.MEETINGS,
        prd="§12.2 / §19",
        status=WiringStatus.ALIVE,
        port="app.domain.meetings",
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
        id=CapabilityId.FDE_FEEDBACK,
        prd="FDE operating layer",
        status=WiringStatus.ALIVE,
        port="app.domain.feedback",
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
        id=CapabilityId.FDE_HUMAN_TAKEOVER,
        prd="FDE operating layer",
        status=WiringStatus.ALIVE,
        port="app.domain.takeover",
    ),
    Capability(
        id=CapabilityId.FDE_IDEMPOTENCY,
        prd="FDE operating layer",
        status=WiringStatus.ALIVE,
        port="app.domain.idempotency",
    ),
    Capability(
        id=CapabilityId.PRELOADED_TOOLS,
        prd="Pre-prod Adjustment F",
        status=WiringStatus.ALIVE,
        port="app.tools.registries.mia_preloaded_tools",
    ),
    Capability(
        id=CapabilityId.MODEL_TASK_CLASSES,
        prd="Pre-prod Adjustment J",
        status=WiringStatus.ALIVE,
        port="app.domain.policies.task_classes",
    ),
    Capability(
        id=CapabilityId.FRESHNESS_POLICY,
        prd="Pre-prod Adjustment N",
        status=WiringStatus.ALIVE,
        port="app.domain.policies.freshness",
    ),
    # Brain (ADR-026). Storage, retrieval and ingestion are alive with no model keys:
    # they degrade to keyword search rather than switching off.
    Capability(
        id=CapabilityId.BRAIN_MEMORY,
        prd="ADR-026",
        status=WiringStatus.ALIVE,
        port="app.brain.store",
    ),
    Capability(
        id=CapabilityId.BRAIN_KNOWLEDGE,
        prd="ADR-026",
        status=WiringStatus.ALIVE,
        port="app.brain.knowledge",
    ),
    Capability(
        id=CapabilityId.BRAIN_RETRIEVAL,
        prd="ADR-026",
        status=WiringStatus.ALIVE,
        port="app.brain.retrieval",
    ),
    Capability(
        id=CapabilityId.EMBEDDINGS,
        prd="ADR-026",
        status=WiringStatus.ALIVE,
        port="app.brain.embeddings",
    ),
    Capability(
        id=CapabilityId.OWNER_AGENT,
        prd="ADR-026",
        status=WiringStatus.ALIVE,
        port="app.graph.owner_agent",
    ),
)


def capability_map() -> dict[str, str]:
    return {item.id.value: item.status.value for item in CAPABILITIES}


def require_alive(capability_id: CapabilityId) -> None:
    match = next(item for item in CAPABILITIES if item.id == capability_id)
    if match.status != WiringStatus.ALIVE:
        raise RuntimeError(f"{capability_id} is {match.status}, not alive")
