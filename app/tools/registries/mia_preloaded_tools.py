"""Catalog of currently pinned Composio execute tools.

Versions are imported from adapter constants — single source of truth.
No dynamic Composio catalog discovery; customer graph has zero Composio tools.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.integrations.calendar import (
    COMPOSIO_FIND_FREE_SLOTS_TOOL,
    COMPOSIO_GOOGLECALENDAR_VERSION,
)
from app.integrations.calendar_booking import (
    COMPOSIO_CREATE_EVENT_TOOL,
    COMPOSIO_EVENTS_GET_TOOL,
    COMPOSIO_EVENTS_LIST_TOOL,
    COMPOSIO_PATCH_EVENT_TOOL,
)
from app.integrations.ga4 import (
    COMPOSIO_GA4_VERSION,
    COMPOSIO_LIST_CONVERSION_EVENTS_TOOL,
    COMPOSIO_PIVOT_REPORT_TOOL,
)
from app.integrations.gmail import (
    COMPOSIO_FETCH_MESSAGE_TOOL,
    COMPOSIO_GMAIL_VERSION,
    GMAIL_NEW_MESSAGE_TRIGGER,
)
from app.integrations.instagram import (
    COMPOSIO_GET_MEDIA_INSIGHTS_TOOL,
    COMPOSIO_GET_USER_MEDIA_TOOL,
    COMPOSIO_INSTAGRAM_VERSION,
    COMPOSIO_SEND_TEXT_TOOL,
)
from app.integrations.linkedin import (
    COMPOSIO_GET_MY_INFO_TOOL,
    COMPOSIO_LINKEDIN_VERSION,
)
from app.integrations.search_console import (
    COMPOSIO_GSC_VERSION,
    COMPOSIO_INSPECT_URL_TOOL,
    COMPOSIO_LIST_SITES_TOOL,
    COMPOSIO_SEARCH_ANALYTICS_TOOL,
)
from app.integrations.sheets import (
    COMPOSIO_GET_SHEET_NAMES_TOOL,
    COMPOSIO_GOOGLESHEETS_VERSION,
    COMPOSIO_UPSERT_ROWS_TOOL,
    COMPOSIO_VALUES_APPEND_TOOL,
    COMPOSIO_VALUES_GET_TOOL,
    COMPOSIO_VALUES_UPDATE_TOOL,
)

# Aliased: app.integrations.instagram exports the same COMPOSIO_SEND_TEXT_TOOL name.
from app.integrations.whatsapp import (  # isort: skip
    COMPOSIO_SEND_TEXT_TOOL as COMPOSIO_WHATSAPP_SEND_TEXT_TOOL,
)
from app.integrations.whatsapp import COMPOSIO_WHATSAPP_VERSION  # isort: skip


class PreloadedTool(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    toolkit: str
    version: str
    risk: str  # R0/R1/R2/R4/R5
    write: bool
    enabled: bool  # True when typed adapter exists and is callable today


PRELOADED_TOOLS: tuple[PreloadedTool, ...] = (
    PreloadedTool(
        name=COMPOSIO_FETCH_MESSAGE_TOOL,
        toolkit="GMAIL",
        version=COMPOSIO_GMAIL_VERSION,
        risk="R0",
        write=False,
        enabled=True,
    ),
    PreloadedTool(
        name=GMAIL_NEW_MESSAGE_TRIGGER,
        toolkit="GMAIL",
        version=COMPOSIO_GMAIL_VERSION,
        risk="R0",
        write=False,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_FIND_FREE_SLOTS_TOOL,
        toolkit="GOOGLECALENDAR",
        version=COMPOSIO_GOOGLECALENDAR_VERSION,
        risk="R0",
        write=False,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_EVENTS_LIST_TOOL,
        toolkit="GOOGLECALENDAR",
        version=COMPOSIO_GOOGLECALENDAR_VERSION,
        risk="R0",
        write=False,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_CREATE_EVENT_TOOL,
        toolkit="GOOGLECALENDAR",
        version=COMPOSIO_GOOGLECALENDAR_VERSION,
        risk="R2",
        write=True,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_EVENTS_GET_TOOL,
        toolkit="GOOGLECALENDAR",
        version=COMPOSIO_GOOGLECALENDAR_VERSION,
        risk="R0",
        write=False,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_PATCH_EVENT_TOOL,
        toolkit="GOOGLECALENDAR",
        version=COMPOSIO_GOOGLECALENDAR_VERSION,
        risk="R2",
        write=True,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_UPSERT_ROWS_TOOL,
        toolkit="GOOGLESHEETS",
        version=COMPOSIO_GOOGLESHEETS_VERSION,
        risk="R1",
        write=True,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_VALUES_GET_TOOL,
        toolkit="GOOGLESHEETS",
        version=COMPOSIO_GOOGLESHEETS_VERSION,
        risk="R0",
        write=False,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_GET_SHEET_NAMES_TOOL,
        toolkit="GOOGLESHEETS",
        version=COMPOSIO_GOOGLESHEETS_VERSION,
        risk="R0",
        write=False,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_VALUES_UPDATE_TOOL,
        toolkit="GOOGLESHEETS",
        version=COMPOSIO_GOOGLESHEETS_VERSION,
        risk="R1",
        write=True,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_VALUES_APPEND_TOOL,
        toolkit="GOOGLESHEETS",
        version=COMPOSIO_GOOGLESHEETS_VERSION,
        risk="R1",
        write=True,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_SEND_TEXT_TOOL,
        toolkit="INSTAGRAM",
        version=COMPOSIO_INSTAGRAM_VERSION,
        risk="R2",
        write=True,
        enabled=True,
    ),
    # ADR-016 mandates this pin: outbound WhatsApp has one owner selected by
    # MIA_WHATSAPP_SENDER, and production runs `composio`. WHATSAPP_SEND_TEMPLATE_MESSAGE
    # stays unpinned on purpose — ADR-016 leaves it unwired (no mass outreach).
    PreloadedTool(
        name=COMPOSIO_WHATSAPP_SEND_TEXT_TOOL,
        toolkit="WHATSAPP",
        version=COMPOSIO_WHATSAPP_VERSION,
        risk="R2",
        write=True,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_GET_USER_MEDIA_TOOL,
        toolkit="INSTAGRAM",
        version=COMPOSIO_INSTAGRAM_VERSION,
        risk="R0",
        write=False,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_GET_MEDIA_INSIGHTS_TOOL,
        toolkit="INSTAGRAM",
        version=COMPOSIO_INSTAGRAM_VERSION,
        risk="R0",
        write=False,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_GET_MY_INFO_TOOL,
        toolkit="LINKEDIN",
        version=COMPOSIO_LINKEDIN_VERSION,
        risk="R0",
        write=False,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_LIST_SITES_TOOL,
        toolkit="GOOGLE_SEARCH_CONSOLE",
        version=COMPOSIO_GSC_VERSION,
        risk="R0",
        write=False,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_SEARCH_ANALYTICS_TOOL,
        toolkit="GOOGLE_SEARCH_CONSOLE",
        version=COMPOSIO_GSC_VERSION,
        risk="R0",
        write=False,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_INSPECT_URL_TOOL,
        toolkit="GOOGLE_SEARCH_CONSOLE",
        version=COMPOSIO_GSC_VERSION,
        risk="R0",
        write=False,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_PIVOT_REPORT_TOOL,
        toolkit="GOOGLE_ANALYTICS",
        version=COMPOSIO_GA4_VERSION,
        risk="R0",
        write=False,
        enabled=True,
    ),
    PreloadedTool(
        name=COMPOSIO_LIST_CONVERSION_EVENTS_TOOL,
        toolkit="GOOGLE_ANALYTICS",
        version=COMPOSIO_GA4_VERSION,
        risk="R0",
        write=False,
        enabled=True,
    ),
)

PRELOADED_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in PRELOADED_TOOLS)

_BY_NAME: dict[str, PreloadedTool] = {t.name: t for t in PRELOADED_TOOLS}


def preloaded_tool(name: str) -> PreloadedTool | None:
    return _BY_NAME.get(name)
