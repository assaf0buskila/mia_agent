from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.api.composio import router as composio_router
from app.api.demo import router as demo_router
from app.api.instagram import router as instagram_router
from app.api.telegram import router as telegram_router
from app.api.website import router as website_router
from app.api.whatsapp import router as whatsapp_router
from app.core.capabilities import capability_map
from app.core.config import MiaEnv, get_settings
from app.core.demo import demo_mode_active
from app.core.errors import MiaError
from app.core.logging import configure_logging
from app.db.session import database_ready, get_session_factory, init_db
from app.db.store import LeadStore

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
app.include_router(instagram_router)
app.include_router(composio_router)


@app.exception_handler(MiaError)
async def mia_error_handler(_request: Request, exc: MiaError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": exc.code, "message": exc.message},
    )


@app.get("/health")
def health() -> dict:
    live = get_settings()
    status = "killed" if live.kill_switch else "ok"
    return {
        "status": status,
        "app": live.app_name,
        "env": live.env.value,
        "version": __version__,
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
        "capabilities": capability_map(),
        "risk": {
            "R4_meta_writes": "approval",
            "R5_destructive": "deny",
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
