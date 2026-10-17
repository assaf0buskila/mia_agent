import logging
import os
import re
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Literal, Self
from urllib.parse import urlparse

from pydantic import (
    Field,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
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


_LOG = logging.getLogger("mia.config")
_BOOL = TypeAdapter(bool)
_INT = TypeAdapter(int)


def _os_bool(value: Any) -> bool:
    return _BOOL.validate_python(value)


def _os_int(low: int, high: int | None) -> Callable[[Any], int]:
    def parse(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError
        number = _INT.validate_python(value)
        if number < low or (high is not None and number > high):
            raise ValueError
        return number

    return parse


def _os_choice(*allowed: str) -> Callable[[Any], str]:
    def parse(value: Any) -> str:
        if value not in allowed:
            raise ValueError
        return value

    return parse


def _os_str(value: Any) -> str:
    if type(value) is not str:
        raise ValueError
    return value


def _os_secret(value: Any) -> str:
    """Unset, or at least 32 bytes. Fingerprints and other checks still happen at use."""
    secret = _os_str(value)
    if secret and len(secret.encode("utf-8")) < 32:
        raise ValueError
    return secret


_SHEET_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _os_sheet_id(value: Any) -> str:
    """Unset, or exactly one spreadsheet id (no list, no whitespace)."""
    sheet_id = _os_str(value).strip()
    if sheet_id and not _SHEET_ID.fullmatch(sheet_id):
        raise ValueError
    return sheet_id


# Field -> (default, parser) for the §3.3 block. A parser raises on any unparsable or out-of-range
# value, and the field then falls back to its default.
_OS_CONFIG_RULES: dict[str, tuple[Any, Callable[[Any], Any]]] = {
    "assaf_os_backend": ("sheets", _os_choice("sheets", "postgres")),
    "assaf_os_ledger_enabled": (False, _os_bool),
    "assaf_os_authority_epoch": (0, _os_int(0, 2**63 - 1)),  # PostgreSQL BIGINT
    "assaf_os_freeze": (False, _os_bool),
    "assaf_os_owner_direct_enabled": (False, _os_bool),
    "owner_undo_window_seconds": (30, _os_int(10, 60)),
    "assaf_os_statements_enabled": (False, _os_bool),
    "os_statement_pepper": ("", _os_secret),
    "assaf_os_mirror_enabled": (False, _os_bool),
    "os_mirror_spreadsheet_id": ("", _os_sheet_id),
    "os_mirror_service_account_json": ("", _os_str),
    "os_mirror_lag_warn_seconds": (300, _os_int(60, 3600)),
    "os_mirror_anti_entropy_seconds": (900, _os_int(300, 3600)),
    "assaf_os_assistant_enabled": (False, _os_bool),
    "os_assistant_ip_hmac_key": ("", _os_secret),
    "os_step_up_secret": ("", _os_secret),
}
_OS_CONFIG_COERCED: set[str] = set()


def _os_config_value(name: str, value: Any) -> Any:
    default, parse = _OS_CONFIG_RULES[name]
    if isinstance(value, str) and not value.strip() and default != "":
        return default  # An empty env value for a non-string field means unset.
    try:
        return parse(value)
    except (TypeError, ValueError, ValidationError):
        if name not in _OS_CONFIG_COERCED:
            _OS_CONFIG_COERCED.add(name)
            _LOG.warning(
                "os_config op=load outcome=coerced reason=config_invalid field=%s", name
            )
        return default


def os_config_coerced() -> bool:
    """True once any §3.3 value in this process was invalid and fell back to its default."""
    return bool(_OS_CONFIG_COERCED)


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
    # V2-14a. A plain str on purpose: no Literal and no validator, so a bad value can never fail
    # the settings load. app.db.app_role_posture treats anything but exactly "enforce" as
    # "observe" at use and logs the coercion once.
    app_role_posture: str = "observe"
    composio_api_key: str = Field(default="")
    composio_user_id: str = Field(default="")
    # Ask Composio for resource ids (GSC site, GA4 property) when the matching env var is
    # blank. Default false: ports are built per request, so this adds one network call per
    # process on first use. Verify with `scripts/probe_composio_discovery.py`, then turn
    # it on.
    composio_discovery: bool = False
    crm_delivery_enabled: bool = False
    # Off until a read-only identity report and separately approved migration establish
    # that every persisted phone comparison key is compatible. Never rewrites history.
    crm_israeli_phone_normalization_enabled: bool = False
    reminders_enabled: bool = False
    reminder_delivery_enabled: bool = False
    # Private standalone owner app. Both gates stay off until a separate release.
    owner_os_enabled: bool = False
    owner_os_actions_enabled: bool = False
    owner_os_google_enabled: bool = False
    owner_os_google_client_id: str = ""
    owner_os_google_subject: str = Field(default="", repr=False)
    owner_os_google_owner_id: str = Field(default="", repr=False)
    owner_os_session_secret: str = Field(default="", repr=False)
    # One pinned Apps Script port for the shared Assaf OS workbook. Independent
    # from the legacy CRM and the synthetic TEST business-ledger adapter.
    assaf_os_enabled: bool = False
    assaf_os_script_url: str = ""
    assaf_os_workbook_id: str = ""
    assaf_os_workspace_id: str = ""
    assaf_os_hmac_secret: str = Field(default="", repr=False)
    assaf_os_mode: Literal["TEST", "LIVE"] = "TEST"
    # Assaf OS v2 on PostgreSQL (V2_PG_CONTRACTS.md §3.3). Every field is default-off and plainly
    # typed: _coerce_os_config turns an invalid value into the safe default, so a misconfiguration
    # never fails the settings load. Secrets and ids are checked where they are used.
    assaf_os_backend: str = "sheets"
    assaf_os_ledger_enabled: bool = False
    assaf_os_authority_epoch: int = 0
    assaf_os_freeze: bool = False
    assaf_os_owner_direct_enabled: bool = False
    owner_undo_window_seconds: int = 30
    assaf_os_statements_enabled: bool = False
    os_statement_pepper: str = Field(default="", repr=False)
    assaf_os_mirror_enabled: bool = False
    os_mirror_spreadsheet_id: str = ""
    os_mirror_service_account_json: str = Field(default="", repr=False)
    os_mirror_lag_warn_seconds: int = 300
    os_mirror_anti_entropy_seconds: int = 900
    assaf_os_assistant_enabled: bool = False
    os_assistant_ip_hmac_key: str = Field(default="", repr=False)
    os_step_up_secret: str = Field(default="", repr=False)
    # Separate, TEST-only Sheets ledger. Never enables CRM or reminder writes.
    business_ledger_test_enabled: bool = False
    business_ledger_workspace_id: str = ""
    business_ledger_script_url: str = ""
    business_ledger_hmac_secret: str = Field(default="", repr=False)
    owner_os_origin: str = ""
    owner_os_workspace_id: str = ""
    owner_os_session_database_url: str = ""
    owner_os_read_database_url: str = ""
    owner_os_action_database_url: str = ""
    owner_os_cursor_secret: str = ""
    # Declared only so the existing mia-env still validates. Nothing reads it since V2-08.
    owner_os_session_ttl_seconds: int = Field(default=1800, gt=0, le=1800)
    # V2-08 sliding sessions: a renewal slides the idle window, never past the absolute cap
    # measured from the chain's first login.
    owner_os_session_idle_seconds: int = Field(default=43_200, ge=300, le=43_200)
    owner_os_session_absolute_seconds: int = Field(default=1_209_600, ge=3_600, le=1_209_600)
    owner_os_session_scopes: str = (
        "owner-os:read:contacts,owner-os:read:activities,owner-os:read:reminders"
    )
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
    assafweb_form_intake_secret: str = Field(default="")

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

    @field_validator(*_OS_CONFIG_RULES, mode="before")
    @classmethod
    def _coerce_os_config(cls, value: Any, info: ValidationInfo) -> Any:
        return _os_config_value(info.field_name, value)

    @model_validator(mode="after")
    def _owner_os_session_windows(self) -> Self:
        if self.owner_os_session_absolute_seconds < self.owner_os_session_idle_seconds:
            raise ValueError("owner_os_session_absolute_seconds must be at least the idle window")
        return self

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
        # The OS mirror workbook is never a write target. The OS workbook stops being one once it
        # is frozen or PostgreSQL holds the authority.
        configured.discard(self.os_mirror_spreadsheet_id.strip())
        if self.assaf_os_freeze or self.assaf_os_backend == "postgres":
            configured.discard(self.assaf_os_workbook_id.strip())
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
