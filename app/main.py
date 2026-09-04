from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.api.baileys import router as baileys_router
from app.api.composio import router as composio_router
from app.api.demo import router as demo_router
from app.api.telegram import router as telegram_router
from app.api.website import router as website_router
from app.api.whatsapp import router as whatsapp_router
from app.brain.store import BrainStore
from app.core.capabilities import capability_map
from app.core.config import MiaEnv, get_settings
from app.core.demo import demo_mode_active
from app.core.errors import MiaError
from app.core.logging import configure_logging
from app.db.session import database_ready, get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.ai_runs import PROMPT_VERSION as SALES_PROMPT_VERSION

settings = get_settings()
configure_logging(settings.log_level)


def _health_ops() -> dict[str, int | None]:
    if not database_ready():
        return {
            "pending_approvals": None,
            "human_takeover": None,
            "failed_sends": None,
            "integration_failures": None,
        }
    try:
        session = get_session_factory()()
        try:
            store = LeadStore(session)
            return {
                "pending_approvals": store.count_pending_approvals(),
                "human_takeover": store.count_human_takeover(),
                "failed_sends": store.count_failed_webhooks(),
                "integration_failures": store.count_open_reconciliation(),
            }
        finally:
            session.close()
    except SQLAlchemyError:
        return {
            "pending_approvals": None,
            "human_takeover": None,
            "failed_sends": None,
            "integration_failures": None,
        }


def brain_health(settings) -> dict[str, object]:
    """Report exactly which brain/voice configuration is missing. Never returns secrets.

    Each feature names the env vars it still needs, so a half-configured deployment is
    diagnosable from `/health` instead of from silence in the logs.
    """
    # `owner_agent_ready` deliberately falls back to the configured sales chain or
    # the owner Gemini model. Health must not describe a working fallback as
    # missing just because the dedicated OpenAI owner-model setting is blank.
    missing_agent: list[str] = []
    if not settings.owner_agent_ready():
        openai_chain_configured = any(
            (
                settings.owner_agent_model.strip(),
                settings.owner_agent_fallback_model.strip(),
                settings.sales_model.strip(),
                settings.sales_fallback_model.strip(),
            )
        )
        gemini_model_configured = bool(settings.owner_agent_gemini_model.strip())
        if not openai_chain_configured and not gemini_model_configured:
            missing_agent.append("MIA_OWNER_AGENT_MODEL")
        if openai_chain_configured and not settings.openai_api_key.strip():
            missing_agent.append("MIA_OPENAI_API_KEY")
        if gemini_model_configured and not settings.gemini_api_key.strip():
            missing_agent.append("MIA_GEMINI_API_KEY")

    missing_embeddings: list[str] = []
    if not settings.embedding_model.strip():
        missing_embeddings.append("MIA_EMBEDDING_MODEL")
    if settings.embedding_dim <= 0:
        missing_embeddings.append("MIA_EMBEDDING_DIM")
    if settings.embedding_provider.strip().lower() == "gemini":
        if not settings.gemini_api_key.strip():
            missing_embeddings.append("MIA_GEMINI_API_KEY")
    elif not settings.openai_api_key.strip():
        missing_embeddings.append("MIA_OPENAI_API_KEY")

    missing_extraction: list[str] = []
    if not settings.extraction_ready():
        if not settings.extraction_model.strip():
            missing_extraction.append("MIA_EXTRACTION_MODEL")
        if not (settings.openai_api_key.strip() or settings.gemini_api_key.strip()):
            missing_extraction.append("MIA_OPENAI_API_KEY")

    missing_voice: list[str] = []
    if not settings.openai_api_key.strip():
        missing_voice.append("MIA_OPENAI_API_KEY")
    if not settings.openai_transcribe_model.strip():
        missing_voice.append("MIA_OPENAI_TRANSCRIBE_MODEL")
    if not settings.telegram_bot_token.strip():
        missing_voice.append("MIA_TELEGRAM_BOT_TOKEN")

    return {
        "memory_enabled": settings.memory_enabled,
        "memory_write_enabled": settings.memory_write_enabled,
        "owner_agent": {
            "ready": settings.owner_agent_ready(),
            "missing": missing_agent,
            "max_steps": settings.owner_agent_max_steps,
        },
        "embeddings": {
            "ready": settings.embeddings_ready(),
            "provider": settings.embedding_provider,
            "dim": settings.embedding_dim,
            "missing": missing_embeddings,
        },
        "memory_extraction": {
            "ready": settings.extraction_ready(),
            "missing": missing_extraction,
        },
        "voice_in": {
            "ready": not missing_voice,
            "missing": missing_voice,
        },
        "knowledge_sources": settings.knowledge_source_list(),
    }


def owner_integrations(settings) -> dict[str, object]:
    """Which owner-console reads can actually fire. Never returns secrets.

    `ready` is configuration, not a live Composio ping. Assaf still has to keep the
    matching Composio connected account Active. Apify is a ResearchPort fallback
    (pinned ``apify/google-search-scraper``) when Firecrawl is unset.
    """
    composio = settings.composio_ready()
    discovery = bool(settings.composio_discovery) and composio
    sheets_id = settings.resolved_sheets_spreadsheet_id()
    authorized_sheets = bool(settings.allowed_sheets_spreadsheet_ids())
    gsc_site = settings.gsc_site_url.strip()
    ga4_property = settings.ga4_property_id.strip()
    firecrawl = bool(settings.firecrawl_api_key.strip())
    apify = bool(settings.apify_token.strip())
    missing: list[str] = []
    if not composio:
        missing.extend(["MIA_COMPOSIO_API_KEY", "MIA_COMPOSIO_USER_ID"])
    if not firecrawl and not apify:
        missing.append("MIA_FIRECRAWL_API_KEY")
    if not discovery:
        if not gsc_site:
            missing.append("MIA_GSC_SITE_URL")
        if not ga4_property:
            missing.append("MIA_GA4_PROPERTY_ID")
    return {
        "composio": composio,
        "gmail_read": composio,
        "gmail_send": settings.gmail_send,
        "calendar_read": composio,
        "calendar_write": settings.calendar_write,
        "sheets_crm": composio and bool(sheets_id),
        # ADR-042 readiness is deliberately config-only: health never makes an
        # authenticated provider call and never returns credentials.
        "sheets_read": composio and authorized_sheets,
        "sheets_update": composio and authorized_sheets,
        "sheets_append": composio and authorized_sheets,
        "linkedin_profile": composio,
        "instagram_insights": composio
        or bool(settings.instagram_access_token.strip()),
        "search_console": composio and (bool(gsc_site) or discovery),
        "ga4": composio and (bool(ga4_property) or discovery),
        "research_firecrawl": firecrawl,
        "research_apify": (not firecrawl) and apify,
        "whatsapp_handoff_send": settings.whatsapp_handoff_send,
        "missing": missing,
    }


def brain_counts() -> dict[str, int | None]:
    """Live corpus sizes, so an empty brain is visible rather than mysterious."""
    if not database_ready():
        return {"memories": None, "knowledge_chunks": None}
    try:
        session = get_session_factory()()
        try:
            brain = BrainStore(session)
            return {
                "memories": brain.count_memories(),
                "knowledge_chunks": brain.count_knowledge_chunks(),
            }
        finally:
            session.close()
    except SQLAlchemyError:
        return {"memories": None, "knowledge_chunks": None}


def openapi_surface(*, env: MiaEnv) -> dict[str, str | None]:
    if env is MiaEnv.PROD:
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {
        "docs_url": "/docs",
        "redoc_url": "/redoc",
        "openapi_url": "/openapi.json",
    }


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Prod schema is mia-migrate. create_all here would delay /health/live and
    # still would not apply SQL migrations (docs/PRODUCTION_BUILD.md §3.6).
    if get_settings().env is not MiaEnv.PROD:
        init_db()
    yield


app = FastAPI(
    title="Mia",
    description="AssafWeb AI Growth & Sales Operator",
    version=__version__,
    lifespan=lifespan,
    **openapi_surface(env=settings.env),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
app.include_router(website_router)
app.include_router(demo_router)
app.include_router(whatsapp_router)
app.include_router(telegram_router)
app.include_router(composio_router)
# Off until MIA_WHATSAPP_BAILEYS_TOKEN is set; fails closed without it.
app.include_router(baileys_router)


@app.exception_handler(MiaError)
async def mia_error_handler(_request: Request, exc: MiaError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": exc.code, "message": exc.message},
    )


def _deployment_block(live) -> dict:
    """Which exact code is serving, so a stale image is visible from production.

    Deliberately identifiers only: a commit sha, an environment name and version
    strings. No secrets, no connection strings, no infrastructure paths.
    """
    schema_version = ""
    if database_ready():
        try:
            from app.db.migrate import applied_schema_version
            from app.db.session import get_engine

            schema_version = applied_schema_version(get_engine())
        except Exception:
            schema_version = ""
    return {
        "commit_sha": live.build_sha.strip(),
        "env": live.env.value,
        "app_version": __version__,
        "prompt_version": SALES_PROMPT_VERSION,
        "schema_version": schema_version,
    }


@app.get("/health")
def health() -> dict:
    live = get_settings()
    status = "killed" if live.kill_switch else "ok"
    return {
        "status": status,
        "app": live.app_name,
        "env": live.env.value,
        "version": __version__,
        "deployment": _deployment_block(live),
        "kill_switch": live.kill_switch,
        "demo": demo_mode_active(live),
        "website_url": live.website_url,
        "public_base_url": live.public_base_url,
        "sales_llm": live.sales_llm_ready(),
        "sales_gemini": live.sales_gemini_ready(),
        "composio": live.composio_ready(),
        "composio_webhook": live.composio_webhook_ready(),
        "postgres": live.postgres_ready(),
        "public_https": live.public_https_ready(),
        "whatsapp_provider": live.whatsapp_provider_label(),
        "whatsapp_connected": live.whatsapp_connected_ready(),
        "whatsapp_ingest": live.whatsapp_ingest_ready(),
        "whatsapp_send": live.whatsapp_send_ready(),
        "whatsapp_owner": live.whatsapp_owner_ready(),
        "website_chat": True,
        "telegram_owner": live.telegram_owner_ready(),
        "email_read": live.composio_ready(),
        "email_send_policy": live.email_send_policy_label(),
        "automation_mode": live.automation_mode.value,
        "whatsapp_handoff_send": live.whatsapp_handoff_send,
        "auto_reply_instagram": live.auto_reply_instagram,
        "ops": _health_ops(),
        "brain": {**brain_health(live), "corpus": brain_counts()},
        "owner_integrations": owner_integrations(live),
        "capabilities": capability_map(),
        "risk": {
            "R4_meta_writes": "approval",
            # Approval, not deny. `propose_composio_write` mints an approval row for a
            # destructive slug and `execute_approved_composio_write` runs it once Assaf
            # taps approve; `test_gmail_send_is_never_auto_executed_and_delete_requires_approval`
            # pins that. "deny" here meant "never automatic" and read as "never".
            "R5_destructive": "approval",
            "kill_switch": live.kill_switch,
        },
    }


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", response_model=None)
def health_ready() -> JSONResponse:
    if database_ready():
        return JSONResponse(content={"status": "ok"})
    return JSONResponse(status_code=503, content={"status": "not_ready"})
