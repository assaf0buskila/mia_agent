"""Isolate unit tests from the live-test .env Assaf fills locally."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import pytest

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
os.environ["MIA_GMAIL_SEND"] = "false"
os.environ["MIA_META_WRITE"] = "false"
os.environ["MIA_AUTO_REPLY_INSTAGRAM"] = "false"
os.environ["MIA_FIRECRAWL_API_KEY"] = ""
os.environ["MIA_APIFY_TOKEN"] = ""
os.environ["MIA_COMPOSIO_API_KEY"] = ""
os.environ["MIA_COMPOSIO_USER_ID"] = ""
os.environ["MIA_COMPOSIO_WEBHOOK_SECRET"] = ""
os.environ["MIA_SHEETS_SPREADSHEET_ID"] = ""
os.environ["MIA_GSC_SITE_URL"] = ""
os.environ["MIA_GA4_PROPERTY_ID"] = ""
os.environ["MIA_COMPOSIO_DISCOVERY"] = "false"
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
        "app.domain.owner_calendar.datetime",
        # The VNext capability layer reads the clock too. owner_calendar now routes
        # through it, so leaving it out meant a frozen test silently read the real
        # clock here and every seeded slot rotted into the past overnight.
        "app.capabilities.calendar.datetime",
        "app.integrations.calendar.datetime",
        "app.domain.meeting_availability.datetime",
        "app.domain.meeting_slots.datetime",
        "app.domain.policies.freshness.datetime",
    ):
        monkeypatch.setattr(path, FrozenDateTime)


# Existing website tests post without Origin (TestClient is not a browser).
# Production fail-closed rejects that. Inject the live widget origin unless a
# test opts out, so the suite still exercises the sales path.
_SUPPRESS_WEBSITE_ORIGIN = ContextVar("_suppress_website_origin", default=False)
_WIDGET_ORIGIN = "https://www.assafweb.com"


@contextmanager
def without_injected_website_origin() -> Iterator[None]:
    token = _SUPPRESS_WEBSITE_ORIGIN.set(True)
    try:
        yield
    finally:
        _SUPPRESS_WEBSITE_ORIGIN.reset(token)


def _headers_have_origin(headers: object) -> bool:
    if headers is None:
        return False
    if hasattr(headers, "keys"):
        return any(str(key).lower() == "origin" for key in headers.keys())
    for item in headers:
        if str(item[0]).lower() == "origin":
            return True
    return False


def identify_website_visitor(
    client,
    session_id: str,
    *,
    phone: str = "0501234567",
    name: str = "דנה",
    text: str = "צריכים אתר",
    email: str = "",
    headers: dict | None = None,
):
    """Identify on the live site path so handoff and CRM tests can proceed."""
    payload = {"text": text, "phone": phone, "name": name}
    if email:
        payload["email"] = email
    extras = {"headers": headers} if headers else {}
    response = client.post(
        f"/v1/website/sessions/{session_id}/messages",
        json=payload,
        **extras,
    )
    assert response.status_code == 200, response.text
    assert response.json()["lead_id"] == ""
    assert response.json()["next_action"] in {"handoff", "no_price"}
    return response


@pytest.fixture(autouse=True)
def _owner_turn_coalesce_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.surfaces import turn_coalesce
    from app.workers import telegram_owner

    monkeypatch.setattr(turn_coalesce, "COALESCE_WAIT_S", 0)
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0)
    turn_coalesce.reset_pending_turns()
    yield
    turn_coalesce.reset_pending_turns()


@pytest.fixture(autouse=True)
def _website_origin_and_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.public_website import reset_public_website_limiter
    from starlette.testclient import TestClient

    reset_public_website_limiter()
    original = TestClient.request

    def request(self, method: str, url, **kwargs):  # noqa: ANN001
        path = str(url)
        if (
            not _SUPPRESS_WEBSITE_ORIGIN.get()
            and method.upper() == "POST"
            and "/v1/website/sessions" in path
            and not _headers_have_origin(kwargs.get("headers"))
        ):
            headers = dict(kwargs.get("headers") or {})
            headers["Origin"] = _WIDGET_ORIGIN
            kwargs["headers"] = headers
        return original(self, method, url, **kwargs)

    monkeypatch.setattr(TestClient, "request", request)


