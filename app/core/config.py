import os
from enum import StrEnum
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
    auto_reply_instagram: bool = False
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
    composio_webhook_secret: str = Field(default="")
    openai_api_key: str = Field(default="")
    openai_transcribe_model: str = Field(default="gpt-transcribe")
    openai_transcribe_fallback_model: str = Field(default="")
    sales_model: str = Field(default="")
    sales_fallback_model: str = Field(default="")
    gemini_api_key: str = Field(default="")
    sales_gemini_model: str = Field(default="")

    whatsapp_verify_token: str = Field(default="")
    whatsapp_owner_phones: str = Field(default="")
    whatsapp_app_secret: str = Field(default="")
    whatsapp_access_token: str = Field(default="")
    whatsapp_phone_number_id: str = Field(default="")
    whatsapp_graph_version: str = Field(default="v25.0")
    whatsapp_click_to_chat: str = Field(default="")
    whatsapp_sender: str = Field(default="direct")
    whatsapp_require_business_scope: bool = True
    whatsapp_handoff_send: bool = False

    telegram_bot_token: str = Field(default="")
    telegram_webhook_secret: str = Field(default="")
    telegram_owner_user_ids: str = Field(default="")

    instagram_verify_token: str = Field(default="")
    instagram_app_secret: str = Field(default="")
    instagram_access_token: str = Field(default="")
    instagram_account_id: str = Field(default="")
    instagram_graph_version: str = Field(default="v26.0")
    instagram_graph_host: str = Field(default="graph.instagram.com")
    instagram_sender: str = Field(default="direct")

    calendar_timezone: str = Field(default="Asia/Jerusalem")
    sheets_spreadsheet_id: str = Field(default="")
    meta_ads_account_id: str = Field(default="")
    campaign_monthly_budget: str = Field(default="")
    campaign_name: str = Field(default="")
    campaign_launch_date: str = Field(default="")
    campaign_objective: str = Field(default="")
    campaign_lead_path: str = Field(default="")
    campaign_e2e_tested: str = Field(default="")
    firecrawl_api_key: str = Field(default="")
    linkedin_access_token: str = Field(default="")
    gsc_site_url: str = Field(default="")
    ga4_property_id: str = Field(default="")

    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    def whatsapp_owner_phone_set(self) -> set[str]:
        phones: set[str] = set()
        for part in self.whatsapp_owner_phones.split(","):
            normalized = part.strip().lstrip("+").replace(" ", "")
            if normalized:
                phones.add(normalized)
        return phones

    def sales_llm_ready(self) -> bool:
        """True when a live sales paraphrase path is configured. Never returns secrets."""
        openai_chain = model_chain(self.sales_model, self.sales_fallback_model)
        openai_ok = bool(self.openai_api_key.strip() and openai_chain)
        return openai_ok or self.sales_gemini_ready()

    def sales_gemini_ready(self) -> bool:
        """True when Gemini fallback key + model id are set. Never returns secrets."""
        return bool(self.gemini_api_key.strip() and self.sales_gemini_model.strip())

    def composio_ready(self) -> bool:
        """True when Composio API key + user id are set. Never returns secrets or ids."""
        return bool(self.composio_api_key.strip() and self.composio_user_id.strip())

    def composio_webhook_ready(self) -> bool:
        """True when Composio webhook secret is set. Never returns the secret."""
        return bool(self.composio_webhook_secret.strip())

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

    def whatsapp_ingest_ready(self) -> bool:
        """True when the Meta inbound webhook path can verify. Never Composio-key-only."""
        return bool(self.whatsapp_verify_token.strip() and self.whatsapp_app_secret.strip())

    def whatsapp_owner_ready(self) -> bool:
        """True when at least one owner phone id is set. Never returns the numbers."""
        return bool(self.whatsapp_owner_phone_set())

    def whatsapp_provider_label(self) -> str:
        """Outbound owner only. Inbound is always Meta (ADR-016). Never secrets."""
        sender = self.whatsapp_sender.strip().lower()
        if sender == "composio":
            return "composio"
        return "meta"

    def whatsapp_connected_ready(self) -> bool:
        """True when the chosen outbound auth pool is present. Not ingest."""
        if self.whatsapp_provider_label() == "composio":
            return self.composio_ready()
        return bool(self.whatsapp_access_token.strip())

    def whatsapp_send_ready(self) -> bool:
        """True when the chosen outbound port would not be Disabled."""
        phone = self.whatsapp_phone_number_id.strip()
        if self.whatsapp_provider_label() == "composio":
            return bool(self.composio_ready() and phone)
        return bool(self.whatsapp_access_token.strip() and phone)

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
