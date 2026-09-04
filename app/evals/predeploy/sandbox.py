"""The seal that makes the real-model suite structurally unable to touch production.

A predeploy eval that can reach the live CRM, Telegram, Gmail or Calendar is not a
safety net, it is a second production writer. Everything here exists so that "the eval
did not write anything" is a property of the wiring rather than a promise about the
scenarios.

Three independent layers, on purpose:

1. **Credentials are removed.** `sealed_settings` keeps the model configuration and
   blanks every integration credential. With no Composio key, `build_gmail_port`,
   `build_sheets_port`, `build_calendar_port` and friends can only return their
   `Disabled*` variants, so even a scenario that forgot to inject a fake cannot reach a
   provider.
2. **Fakes are injected anyway.** `owner_ports` passes an explicit in-memory double for
   every port `answer_owner` accepts, so no argument is left `None` for
   `bind_owner_house_ports` to fill from settings.
3. **The process environment is sealed.** `get_engine()` reads the *global* settings, not
   the object we pass around, so `MIA_DATABASE_URL` is forced to in-memory sqlite and
   `MIA_ENV=test` stops a local `.env` from re-injecting what layer 1 removed. Model keys
   are captured before this happens, which is why the seal takes a `Settings` argument.

`assert_sealed` is the tripwire: it runs before any scenario and raises rather than let a
run proceed against something live.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.core.config import Settings
from app.db.session import get_session_factory, init_db, reset_engine
from app.db.store import LeadStore
from app.integrations.base import OutboundMessage
from app.integrations.calendar import FakeCalendarAgendaPort, FakeCalendarPort
from app.integrations.ga4 import FakeGa4Port
from app.integrations.gmail import FakeGmailPort
from app.integrations.instagram_insights import FakeInstagramInsightsPort
from app.integrations.linkedin import FakeLinkedInPort
from app.integrations.research import FakeResearchPort
from app.integrations.search_console import FakeSearchConsolePort
from app.integrations.seo_audit import FakeSeoAuditPort
from app.integrations.sheets import FakeSheetsPort
from app.surfaces.crm import FakeContactsCrm

# A Telegram owner id that exists only here. It is what lets the website handoff path
# actually run end to end (`_ping_assaf` returns early when the owner set is empty)
# while `MIA_TELEGRAM_BOT_TOKEN` stays blank, so no real Telegram adapter can be built.
SANDBOX_OWNER_ID = "900000001"

# Process-level seal. Applied once, before the first scenario, because `get_engine` and
# any helper that calls `get_settings()` read these rather than the object we thread
# through the call sites.
SEALED_ENV: dict[str, str] = {
    # Stops pydantic-settings from reading the operator's `.env`, which is where the
    # live Composio and Telegram credentials normally live.
    "MIA_ENV": "test",
    "MIA_DATABASE_URL": "sqlite:///:memory:",
    "MIA_COMPOSIO_API_KEY": "",
    "MIA_COMPOSIO_USER_ID": "",
    "MIA_COMPOSIO_WEBHOOK_SECRET": "",
    "MIA_COMPOSIO_DISCOVERY": "false",
    "MIA_TELEGRAM_BOT_TOKEN": "",
    "MIA_TELEGRAM_WEBHOOK_SECRET": "",
    "MIA_TELEGRAM_OWNER_USER_IDS": SANDBOX_OWNER_ID,
    "MIA_WHATSAPP_ACCESS_TOKEN": "",
    "MIA_WHATSAPP_APP_SECRET": "",
    "MIA_WHATSAPP_VERIFY_TOKEN": "",
    "MIA_WHATSAPP_PHONE_NUMBER_ID": "",
    "MIA_WHATSAPP_BAILEYS_URL": "",
    "MIA_WHATSAPP_BAILEYS_TOKEN": "",
    "MIA_INSTAGRAM_ACCESS_TOKEN": "",
    "MIA_FIRECRAWL_API_KEY": "",
    "MIA_APIFY_TOKEN": "",
    "MIA_SHEETS_SPREADSHEET_ID": "",
    "MIA_SHEETS_ALLOWED_SPREADSHEET_IDS": "",
    "MIA_GMAIL_SEND": "false",
    "MIA_CALENDAR_WRITE": "false",
    "MIA_META_WRITE": "false",
    "MIA_AUTO_REPLY_INSTAGRAM": "false",
    "MIA_KILL_SWITCH": "false",
    "MIA_DEMO_MODE": "false",
}

# Settings fields the seal overrides on the object the scenarios actually pass around.
# Anything absent here is inherited from the operator's real configuration, which is the
# point: model ids, token ceilings and prompt-shaping settings must match production or
# the eval is measuring a different product.
_SEALED_FIELDS: dict[str, Any] = {
    "database_url": "sqlite:///:memory:",
    "composio_api_key": "",
    "composio_user_id": "",
    "composio_webhook_secret": "",
    "composio_discovery": False,
    "telegram_bot_token": "",
    "telegram_webhook_secret": "",
    "telegram_owner_user_ids": SANDBOX_OWNER_ID,
    "whatsapp_access_token": "",
    "whatsapp_app_secret": "",
    "whatsapp_verify_token": "",
    "whatsapp_phone_number_id": "",
    "whatsapp_baileys_url": "",
    "whatsapp_baileys_token": "",
    "whatsapp_handoff_send": False,
    "instagram_access_token": "",
    "firecrawl_api_key": "",
    "apify_token": "",
    "sheets_spreadsheet_id": "",
    "sheets_allowed_spreadsheet_ids": "",
    "gmail_send": False,
    "calendar_write": False,
    "meta_write": False,
    "auto_reply_instagram": False,
    "kill_switch": False,
    "demo_mode": False,
}


class SealBroken(RuntimeError):
    """A live credential or a live database survived the seal. Never run past this."""


class _Delivered:
    """An already-finished awaitable.

    `run_site_turn` calls `MessagePort.send` synchronously and discards the result, so a
    real coroutine here would leak an un-awaited warning on every handoff turn. This
    satisfies both that call site and `ping_assaf_async`.
    """

    def __await__(self) -> Iterator[Any]:
        return iter(())


class SealedOwnerPort:
    """Records the owner ping instead of sending it. There is no Telegram behind this."""

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> _Delivered:
        self.sent.append(message)
        return _Delivered()


def sealed_settings(base: Settings) -> Settings:
    """Model configuration from `base`, every integration credential removed."""
    return base.model_copy(update=dict(_SEALED_FIELDS))


def seal_process_environment() -> None:
    """Apply `SEALED_ENV` and rebuild the engine against in-memory sqlite.

    Impure by necessity: `app.db.session` resolves its DSN from the global settings, so
    the only way to guarantee no scenario writes the production database is to change
    what the global settings resolve to before the first connection is opened.
    """
    os.environ.update(SEALED_ENV)
    reset_engine()
    init_db()


def assert_sealed(settings: Settings) -> None:
    """Raise unless this configuration is structurally unable to reach production."""
    live: list[str] = []
    if settings.composio_api_key.strip() or settings.composio_user_id.strip():
        live.append("composio credentials survived the seal")
    if settings.telegram_bot_token.strip():
        live.append("telegram bot token survived the seal")
    if settings.whatsapp_access_token.strip() or settings.whatsapp_phone_number_id.strip():
        live.append("whatsapp credentials survived the seal")
    if settings.instagram_access_token.strip():
        live.append("instagram token survived the seal")
    if settings.gmail_send:
        live.append("gmail send is enabled")
    if settings.calendar_write:
        live.append("calendar write is enabled")
    if settings.meta_write:
        live.append("meta write is enabled")
    if ":memory:" not in settings.database_url:
        live.append(f"database is not in-memory sqlite ({settings.database_url[:32]})")
    if live:
        raise SealBroken("; ".join(live))


@dataclass
class OwnerWorld:
    """One owner scenario's isolated universe. Every port is an in-memory double."""

    settings: Settings
    store: LeadStore
    brain: BrainStore
    embedding: FakeEmbeddingPort
    gmail: Any = field(default_factory=FakeGmailPort)
    sheets: Any = field(default_factory=FakeSheetsPort)
    calendar: Any = field(default_factory=FakeCalendarPort)
    calendar_agenda: Any = field(default_factory=FakeCalendarAgendaPort)
    linkedin: Any = field(default_factory=FakeLinkedInPort)
    search_console: Any = field(default_factory=FakeSearchConsolePort)
    ga4: Any = field(default_factory=FakeGa4Port)
    seo_audit: Any = field(default_factory=FakeSeoAuditPort)
    instagram_insights: Any = field(default_factory=FakeInstagramInsightsPort)
    research: Any = field(default_factory=FakeResearchPort)

    def ports(self) -> dict[str, Any]:
        """Keyword arguments for `answer_owner`. No port is left for settings to bind."""
        return {
            "gmail": self.gmail,
            "sheets": self.sheets,
            "calendar": self.calendar,
            "calendar_agenda": self.calendar_agenda,
            "linkedin": self.linkedin,
            "search_console": self.search_console,
            "ga4": self.ga4,
            "seo_audit": self.seo_audit,
            "instagram_insights": self.instagram_insights,
            "research": self.research,
            "embedding_port": self.embedding,
        }

    def sheet_writes(self) -> tuple[str, ...]:
        """Every write the fake Sheets port saw. Reads are not recorded, by design."""
        return tuple(str(operation[0]) for operation in self.sheets.owner_operations)


def build_owner_world(settings: Settings) -> OwnerWorld:
    """A fresh session, store and brain per scenario so runs cannot leak into each other."""
    session = get_session_factory()()
    return OwnerWorld(
        settings=settings,
        store=LeadStore(session),
        brain=BrainStore(session),
        embedding=FakeEmbeddingPort(),
    )


def build_site_crm() -> FakeContactsCrm:
    """The website CRM double. `FakeContactsCrm` enforces the same tab lock as live."""
    return FakeContactsCrm()
