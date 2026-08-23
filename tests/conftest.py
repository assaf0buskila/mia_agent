"""Isolate unit tests from the live-test .env Assaf fills locally."""

import os

os.environ["MIA_ENV"] = "test"
os.environ["MIA_AUTOMATION_MODE"] = "auto_approved"
os.environ["MIA_DEMO_MODE"] = "false"
os.environ["MIA_KILL_SWITCH"] = "false"
os.environ["MIA_DATABASE_URL"] = "sqlite:///:memory:"
os.environ["MIA_WEBSITE_URL"] = "https://www.assafweb.com"
os.environ["MIA_PUBLIC_BASE_URL"] = "http://127.0.0.1:8000"
os.environ["MIA_CORS_ORIGINS"] = (
    "https://www.assafweb.com,https://assafweb.com,"
    "http://localhost:3000,http://localhost:5173,http://127.0.0.1:8000"
)
os.environ["MIA_SALES_MODEL"] = ""
os.environ["MIA_SALES_FALLBACK_MODEL"] = ""
os.environ["MIA_OPENAI_API_KEY"] = ""
os.environ["MIA_GEMINI_API_KEY"] = ""
os.environ["MIA_SALES_GEMINI_MODEL"] = ""
os.environ["MIA_OPENAI_TRANSCRIBE_FALLBACK_MODEL"] = ""
os.environ["MIA_WHATSAPP_ACCESS_TOKEN"] = ""
os.environ["MIA_WHATSAPP_APP_SECRET"] = ""
os.environ["MIA_WHATSAPP_VERIFY_TOKEN"] = ""
os.environ["MIA_WHATSAPP_SENDER"] = "direct"
os.environ["MIA_GMAIL_SEND"] = "false"
os.environ["MIA_META_WRITE"] = "false"
os.environ["MIA_AUTO_REPLY_INSTAGRAM"] = "false"
os.environ["MIA_FIRECRAWL_API_KEY"] = ""
os.environ["MIA_COMPOSIO_API_KEY"] = ""
os.environ["MIA_COMPOSIO_USER_ID"] = ""
os.environ["MIA_COMPOSIO_WEBHOOK_SECRET"] = ""
os.environ["MIA_LINKEDIN_ACCESS_TOKEN"] = ""
os.environ["MIA_SHEETS_SPREADSHEET_ID"] = ""
os.environ["MIA_META_ADS_ACCOUNT_ID"] = ""
os.environ["MIA_CAMPAIGN_MONTHLY_BUDGET"] = ""
os.environ["MIA_CAMPAIGN_NAME"] = ""
os.environ["MIA_CAMPAIGN_LAUNCH_DATE"] = ""
os.environ["MIA_CAMPAIGN_OBJECTIVE"] = ""
os.environ["MIA_CAMPAIGN_LEAD_PATH"] = ""
os.environ["MIA_CAMPAIGN_E2E_TESTED"] = ""
os.environ["MIA_CALENDAR_WRITE"] = "true"
os.environ["MIA_WHATSAPP_REQUIRE_BUSINESS_SCOPE"] = "false"
os.environ["MIA_TELEGRAM_BOT_TOKEN"] = ""
os.environ["MIA_TELEGRAM_WEBHOOK_SECRET"] = ""
os.environ["MIA_TELEGRAM_OWNER_USER_IDS"] = ""


def freeze_mia_clock(monkeypatch, frozen) -> None:
    """Pin datetime.now on calendar paths so ADR-012 fixtures are not Sunday-flaky."""
    from datetime import datetime as DateTime

    class FrozenDateTime(DateTime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    for path in (
        "app.api.inbound.datetime",
        "app.domain.calendar_booking.datetime",
        "app.domain.meeting_changes.datetime",
        "app.integrations.calendar.datetime",
        "app.domain.meeting_availability.datetime",
        "app.domain.meeting_slots.datetime",
        "app.domain.policies.freshness.datetime",
    ):
        monkeypatch.setattr(path, FrozenDateTime)

