"""Pinned business capabilities. Graphs call these names, never Composio slugs."""

from __future__ import annotations

from app.capabilities.types import (
    CapabilitySpec,
    GraphName,
    Sensitivity,
    spec,
)

MAIL_SEARCH = "mail.search"
MAIL_READ = "mail.read"
MAIL_CREATE_DRAFT = "mail.create_draft"
MAIL_DELETE = "mail.delete"
CALENDAR_GET_SCHEDULE = "calendar.get_schedule"
BUSINESS_GET_INFORMATION = "business.get_information"
LEADS_GET_RECENT = "leads.get_recent"
MEMORY_SEARCH = "memory.search"
KNOWLEDGE_SEARCH = "knowledge.search"
RESEARCH_SEARCH = "research.search"

_OWNER = frozenset({GraphName.OWNER})
_CLIENT = frozenset({GraphName.CLIENT})
_BOTH = frozenset({GraphName.OWNER, GraphName.CLIENT})

CAPABILITIES: tuple[CapabilitySpec, ...] = (
    spec(MAIL_SEARCH, Sensitivity.READ, _OWNER),
    spec(MAIL_READ, Sensitivity.READ, _OWNER),
    spec(MAIL_CREATE_DRAFT, Sensitivity.WRITE, _OWNER, confirmation_required=False),
    spec(MAIL_DELETE, Sensitivity.DESTRUCTIVE, _OWNER),
    spec(CALENDAR_GET_SCHEDULE, Sensitivity.READ, _OWNER),
    spec(LEADS_GET_RECENT, Sensitivity.READ, _OWNER),
    spec(MEMORY_SEARCH, Sensitivity.READ, _OWNER),
    spec(KNOWLEDGE_SEARCH, Sensitivity.READ, _BOTH),
    spec(RESEARCH_SEARCH, Sensitivity.READ, _OWNER),
    spec(BUSINESS_GET_INFORMATION, Sensitivity.READ, _CLIENT),
)

_BY_NAME: dict[str, CapabilitySpec] = {item.name: item for item in CAPABILITIES}

OWNER_CAPABILITIES: frozenset[str] = frozenset(
    item.name for item in CAPABILITIES if GraphName.OWNER in item.graphs
)
CLIENT_CAPABILITIES: frozenset[str] = frozenset(
    item.name for item in CAPABILITIES if GraphName.CLIENT in item.graphs
)


def get_capability(name: str) -> CapabilitySpec | None:
    return _BY_NAME.get(name)
