import os
from enum import StrEnum
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.models import model_chain


class MiaEnv(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class AutomationMode(StrEnum):
    OFF = "off"
    DRAFT_ONLY = "draft_only"
    SHADOW = "shadow"
    HYBRID = "hybrid"
    AUTO_APPROVED = "auto_approved"


class Settings(BaseSettings):
    """Runtime settings. Secrets come from env / Secrets Manager, never from code."""

    model_config = SettingsConfigDict(
        env_prefix="MIA_",
        env_file=None if os.environ.get("MIA_ENV") == "test" else ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: MiaEnv = MiaEnv.DEV
    automation_mode: AutomationMode = AutomationMode.SHADOW
    demo_mode: bool = False
    log_level: str = "INFO"
    kill_switch: bool = False
    calendar_write: bool = False
    gmail_send: bool = False
    meta_write: bool = False
    app_name: str = "mia"

    website_url: str = Field(default="https://www.assafweb.com")
    public_base_url: str = Field(default="http://127.0.0.1:8000")
    cors_origins: str = Field(
        default=(
            "https://www.assafweb.com,https://assafweb.com,"
            "http://localhost:3000,http://localhost:5173,http://127.0.0.1:8000"
        )
    )

    database_url: str = Field(default="sqlite:///:memory:")
    composio_api_key: str = Field(default="")
    composio_user_id: str = Field(default="")
    # Ask Composio for resource ids (GSC site, GA4 property) when the matching env var is
    # blank. Default false: ports are built per request, so this adds one network call per
    # process on first use. Verify with `scripts/probe_composio_discovery.py`, then turn
    # it on.
    composio_discovery: bool = False
    crm_delivery_enabled: bool = False
    openai_api_key: str = Field(default="")
    openai_transcribe_model: str = Field(default="gpt-transcribe")
    openai_transcribe_fallback_model: str = Field(default="")
    gemini_transcribe_model: str = Field(default="")
    transcription_timeout_seconds: float = Field(default=20.0, gt=0.0, le=120.0)
    sales_model: str = Field(default="")
    sales_fallback_model: str = Field(default="")
    gemini_api_key: str = Field(default="")
    sales_gemini_model: str = Field(default="")
    sales_reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "low"
    llm_request_timeout_seconds: float = Field(default=45.0, gt=0.0, le=120.0)

    # Brain. Model ids stay config, never hard-coded (AGENTS.md build-time model policy).
    # Recommended values are documented in .env.example.
    owner_agent_model: str = Field(default="")
    owner_agent_fallback_model: str = Field(default="")
    # Owner-only Gemini fallback. Sales and extraction use their own configured models.
    owner_agent_gemini_model: str = Field(default="")
    owner_agent_max_steps: int = Field(default=8)
    owner_agent_reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] = "medium"
    owner_turn_timeout_seconds: float = Field(default=45.0, gt=0.0, le=300.0)
    # Reserved off the end of the owner turn deadline so a model/tool call that
    # finishes right up against the deadline still leaves time for the reply to
    # actually reach Telegram (app/surfaces/owner.py's post-deadline check stops
    # a send that would otherwise start after `deadline_at`). This is the
    # `reserve` in every `child_call_timeout` call the owner loop makes.
    owner_final_reserve_seconds: float = Field(default=5.0, gt=0.0, le=30.0)
    # Per-attempt cap for ONE model rung inside the owner tool loop. Bounds a
    # slow/hanging primary so it cannot burn the whole remaining turn budget and
    # starve every fallback rung behind it (see `LlmModelChain.complete`'s
    # `attempt_timeout`). Independent of `llm_request_timeout_seconds`, which is
    # only the client's own fallback default when no explicit timeout is given.
    owner_model_attempt_timeout_seconds: float = Field(default=20.0, gt=0.0, le=120.0)
    # Below this many seconds of remaining turn budget, a model call is not worth
    # starting at all -- there would not be enough time left for even one
    # plausible attempt plus the final-reserve send. Used as `child_call_timeout`'s
    # `minimum` for the model chain call in `run_owner_agent`.
    owner_min_model_seconds: float = Field(default=6.0, gt=0.0, le=60.0)
    # Same idea as `owner_min_model_seconds`, for one tool dispatch
    # (`_run_tool_with_timeout`'s `child_call_timeout` call): below this many
    # seconds left, a tool call is refused rather than started and immediately
    # cut off.
    owner_min_tool_seconds: float = Field(default=2.0, gt=0.0, le=60.0)
    telegram_typing_interval_seconds: float = Field(default=4.0, gt=0.5, le=10.0)
    embedding_provider: str = Field(default="openai")
    embedding_model: str = Field(default="")
    embedding_dim: int = Field(default=1536)
    memory_enabled: bool = True
    memory_write_enabled: bool = True
    memory_max_context_chars: int = Field(default=4000)
    memory_weight_relevance: float = Field(default=1.0)
    memory_weight_recency: float = Field(default=0.5)
    memory_weight_importance: float = Field(default=0.3)
    # Completion bounds. Nothing passed one before, so a runaway generation was billed
    # in full and the first cost signal was the invoice. The owner loop needs room for a
    # real answer after tool results. 0 means "send no bound", which is the old behaviour.
    max_completion_tokens_owner: int = Field(default=1500)
    knowledge_sources: str = Field(
        default="llms-full.txt,llms.txt,pricing.md",
    )

    whatsapp_click_to_chat: str = Field(default="")
    # Stamped into the image at build time from the tested commit. Never derived
    # from the working tree at runtime: the point is to prove which code is serving.
    build_sha: str = Field(default="")

    telegram_bot_token: str = Field(default="")
    telegram_webhook_secret: str = Field(default="")
    telegram_owner_user_ids: str = Field(default="")

    instagram_access_token: str = Field(default="")
    instagram_account_id: str = Field(default="")
    instagram_graph_version: str = Field(default="v26.0")
    instagram_graph_host: str = Field(default="graph.instagram.com")
    instagram_sender: str = Field(default="direct")

    calendar_timezone: str = Field(default="Asia/Jerusalem")
    # Empty env still resolves to the locked Contacts workbook. Live cannot forget.
    sheets_spreadsheet_id: str = Field(default="")
    sheets_allowed_spreadsheet_ids: str = Field(default="")
    firecrawl_api_key: str = Field(default="")
    apify_token: str = Field(default="")
    gsc_site_url: str = Field(default="")
    ga4_property_id: str = Field(default="")

    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    def resolved_sheets_spreadsheet_id(self) -> str:
        """Env override if set; otherwise the locked Contacts workbook."""
        from app.surfaces.crm import LOCKED_SPREADSHEET_ID

        return self.sheets_spreadsheet_id.strip() or LOCKED_SPREADSHEET_ID

    def allowed_sheets_spreadsheet_ids(self) -> frozenset[str]:
        """Locked CRM workbook is always eligible. Extra IDs stay optional."""
        from app.surfaces.crm import LOCKED_SPREADSHEET_ID

        configured = {
            item.strip() for item in self.sheets_allowed_spreadsheet_ids.split(",") if item.strip()
        }
        configured.add(self.resolved_sheets_spreadsheet_id())
        configured.add(LOCKED_SPREADSHEET_ID)
        return frozenset(configured)

    def sales_llm_ready(self) -> bool:
        """True when a live sales paraphrase path is configured. Never returns secrets."""
        openai_chain = model_chain(self.sales_model, self.sales_fallback_model)
        openai_ok = bool(self.openai_api_key.strip() and openai_chain)
        return openai_ok or self.sales_gemini_ready()

    def sales_gemini_ready(self) -> bool:
        """True when Gemini fallback key + model id are set. Never returns secrets."""
        return bool(self.gemini_api_key.strip() and self.sales_gemini_model.strip())

    def owner_agent_ready(self) -> bool:
        """True when the owner tool-calling loop can run. Never returns secrets.

        False produces an honest unavailable reply; no alternate conversation engine runs.

        Owner and website model ids are separate purpose-specific contracts.
        """
        chain = model_chain(
            self.owner_agent_model,
            self.owner_agent_fallback_model,
        )
        openai_ok = bool(self.openai_api_key.strip() and chain)
        gemini_ok = bool(self.gemini_api_key.strip() and self.owner_agent_gemini_model.strip())
        return openai_ok or gemini_ok

    def embeddings_ready(self) -> bool:
        """True when semantic retrieval is available. Never returns secrets."""
        if not self.embedding_model.strip() or self.embedding_dim <= 0:
            return False
        if self.embedding_provider.strip().lower() == "gemini":
            return bool(self.gemini_api_key.strip())
        return bool(self.openai_api_key.strip())

    def brain_ready(self) -> bool:
        """True when the brain has any usable retrieval path (semantic or keyword)."""
        return bool(self.memory_enabled)

    def knowledge_source_list(self) -> list[str]:
        return [item.strip() for item in self.knowledge_sources.split(",") if item.strip()]

    def composio_ready(self) -> bool:
        """True when Composio API key + user id are set. Never returns secrets or ids."""
        return bool(self.composio_api_key.strip() and self.composio_user_id.strip())

    def postgres_ready(self) -> bool:
        """True when DATABASE_URL is Postgres. Never returns the DSN."""
        scheme = self.database_url.strip().split(":", 1)[0].lower()
        return scheme in {"postgres", "postgresql"} or scheme.startswith("postgresql+")

    def public_https_ready(self) -> bool:
        """True when public_base_url is stable HTTPS. Never a tunnel or loopback."""
        parsed = urlparse(self.public_base_url.strip())
        if parsed.scheme != "https" or not parsed.netloc:
            return False
        host = (parsed.hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            return False
        return not host.endswith(".trycloudflare.com")

    def telegram_owner_user_id_set(self) -> set[str]:
        ids: set[str] = set()
        for part in self.telegram_owner_user_ids.split(","):
            raw = part.strip()
            if raw.isdigit():
                ids.add(raw)
        return ids

    def telegram_owner_ready(self) -> bool:
        """True when token, webhook secret, and numeric owner ids are all set."""
        return bool(
            self.telegram_bot_token.strip()
            and self.telegram_webhook_secret.strip()
            and self.telegram_owner_user_id_set()
        )

    def email_send_policy_label(self) -> str:
        return "approval"


def get_settings() -> Settings:
    return Settings()
