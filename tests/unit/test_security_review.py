"""Pre-cloud security review — contracts that must stay fail-closed."""

from app.core.config import Settings, get_settings
from app.core.redact import redact
from app.core.risk import PolicyDecision, RiskAction, RiskLevel, decide
from app.core.write_flags import named_write_may_auto, write_flag_enabled
from app.main import app
from app.tools.registries.mia_preloaded_tools import PRELOADED_TOOLS, preloaded_tool
from fastapi.testclient import TestClient

_ALLOWED_WRITE_PINS = frozenset(
    {
        "GOOGLECALENDAR_CREATE_EVENT",
        "GOOGLECALENDAR_PATCH_EVENT",
        "GOOGLESHEETS_UPSERT_ROWS",
        "GOOGLESHEETS_VALUES_UPDATE",
        "GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND",
        # ADR-016: one outbound WhatsApp owner, production is composio.
        "WHATSAPP_SEND_MESSAGE",
        # Named owner Telegram draft → approve → send. Not catalog auto-fire.
        "GMAIL_CREATE_EMAIL_DRAFT",
        "GMAIL_SEND_DRAFT",
    }
)


def test_cors_allowlist_has_no_wildcard() -> None:
    origins = get_settings().cors_origin_list()
    assert origins
    assert "*" not in origins
    assert "https://www.assafweb.com" in origins
    client = TestClient(app)
    denied = client.get("/health", headers={"Origin": "https://evil.example"})
    assert denied.headers.get("access-control-allow-origin") != "https://evil.example"
    allowed = client.get("/health", headers={"Origin": "https://www.assafweb.com"})
    assert allowed.headers.get("access-control-allow-origin") == "https://www.assafweb.com"


def test_redact_strips_secrets_and_pii() -> None:
    cleaned = redact(
        {
            "email": "a@b.com",
            "phone": "050123",
            "api_key": "sk-live",
            "token": "secret",
            "text": "ok",
            "nested": {"composio_api_key": "cmp", "database_url": "postgres://x"},
        }
    )
    assert cleaned["email"] == "[redacted]"
    assert cleaned["phone"] == "[redacted]"
    assert cleaned["api_key"] == "[redacted]"
    assert cleaned["token"] == "[redacted]"
    assert cleaned["text"] == "ok"
    assert (
        redact("HTTP Request: POST https://api.telegram.org/bot123:AAFakeToken/sendMessage")
        == "HTTP Request: POST https://api.telegram.org/bot[redacted]/sendMessage"
    )
    assert cleaned["nested"]["composio_api_key"] == "[redacted]"
    assert cleaned["nested"]["database_url"] == "[redacted]"


def test_preloaded_writes_are_allowlisted_only() -> None:
    writes = {tool.name for tool in PRELOADED_TOOLS if tool.write}
    assert writes == _ALLOWED_WRITE_PINS
    assert preloaded_tool("GMAIL_SEND_EMAIL") is None
    assert preloaded_tool("WHATSAPP_SEND_TEMPLATE_MESSAGE") is None
    assert preloaded_tool("METAADS_UPDATE_CAMPAIGN") is None
    assert preloaded_tool("INSTAGRAM_CREATE_POST") is None
    assert preloaded_tool("INSTAGRAM_CREATE_MEDIA_CONTAINER") is None
    assert preloaded_tool("INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH") is None
    assert preloaded_tool("GOOGLE_ANALYTICS_SEND_EVENTS") is None
    assert preloaded_tool("GOOGLECALENDAR_DELETE") is None
    assert preloaded_tool("GOOGLESHEETS_DELETE_DIMENSION") is None
    assert preloaded_tool("LINKEDIN_DELETE_POST") is None


def test_r4_approval_r5_deny_not_flag_overridable() -> None:
    assert (
        decide(
            RiskAction(name="meta_write", risk=RiskLevel.R4_FINANCIAL_MARKETING),
            kill_switch=False,
        )
        == PolicyDecision.APPROVAL
    )
    assert (
        decide(
            RiskAction(name="delete_data", risk=RiskLevel.R5_DESTRUCTIVE),
            kill_switch=False,
        )
        == PolicyDecision.DENY
    )
    assert named_write_may_auto(enabled=True, risk=RiskLevel.R4_FINANCIAL_MARKETING) is False
    assert named_write_may_auto(enabled=True, risk=RiskLevel.R5_DESTRUCTIVE) is False
    settings = Settings()
    assert write_flag_enabled(settings, "gmail_send") is False
    assert write_flag_enabled(settings, "meta_write") is False
    assert write_flag_enabled(settings, "auto_followup") is False
    assert write_flag_enabled(settings, "browser_automation") is False
    assert write_flag_enabled(settings, "dynamic_tool_discovery") is False
