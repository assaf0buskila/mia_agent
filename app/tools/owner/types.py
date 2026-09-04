"""Shared owner tool types, argument-schema helpers and small result shims.

Every leaf owner tool module imports from here, so this module must stay free of
leaf imports. `ToolResult` is defined exactly once: several call sites test it with
`isinstance`, and a second class would make those checks fail open.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.brain.embeddings import EmbeddingPort
from app.brain.retrieval import MemoryScoreWeights
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.store import LeadStore
from app.integrations.calendar import CalendarAgendaPort, CalendarPort
from app.integrations.ga4 import Ga4Port
from app.integrations.gmail import GmailPort
from app.integrations.instagram_insights import InstagramInsightsPort
from app.integrations.linkedin import LinkedInPort
from app.integrations.research import ResearchPort
from app.integrations.search_console import SearchConsolePort
from app.integrations.seo_audit import SeoAuditPort
from app.integrations.sheets import SheetsPort

MAX_TOOL_RESULT_CHARS = 3000

# How a tool call actually ended. `ok` answers "is there an answer to use"; these
# answer "what happened", so a timeout or a half-read is never filed as a clean
# success. Telemetry reads the outcome; owner-facing copy stays natural.
OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_PARTIAL = "partial"
_NO_ARGS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}


def _string_arg(name: str, description: str, *, optional: bool = False) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            name: {
                "type": ["string", "null"] if optional else "string",
                "description": description,
            }
        },
        "required": [name],
        "additionalProperties": False,
    }


def _enum_arg(name: str, description: str, *, enum: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            name: {
                "type": "string",
                "description": description,
                "enum": enum,
            }
        },
        "required": [name],
        "additionalProperties": False,
    }


@dataclass
class ToolContext:
    """Everything a tool handler may touch. No secrets are exposed to the model."""

    store: LeadStore
    brain: BrainStore
    settings: Settings
    # Derived from the request at the channel entry point, never chosen here.
    principal: Principal
    embedding_port: EmbeddingPort
    calendar: CalendarPort | None = None
    calendar_agenda: CalendarAgendaPort | None = None
    gmail: GmailPort | None = None
    linkedin: LinkedInPort | None = None
    search_console: SearchConsolePort | None = None
    ga4: Ga4Port | None = None
    seo_audit: SeoAuditPort | None = None
    instagram_insights: InstagramInsightsPort | None = None
    research: ResearchPort | None = None
    sheets: SheetsPort | None = None
    kill_switch: bool = False
    demo_active: bool = False
    source_ref: str = ""
    owner_text: str = ""
    now: datetime | None = None

    def timezone(self) -> str:
        return self.settings.calendar_timezone

    def weights(self) -> MemoryScoreWeights:
        return MemoryScoreWeights(
            relevance=self.settings.memory_weight_relevance,
            recency=self.settings.memory_weight_recency,
            importance=self.settings.memory_weight_importance,
        )


@dataclass
class ToolResult:
    ok: bool
    text: str = ""
    error: str = ""
    max_chars: int = MAX_TOOL_RESULT_CHARS
    # Exact durable approval created by this tool call. This is orchestration
    # metadata, not provider/model text, and must remain bound to this turn.
    approval_id: str = ""
    # Blank means "derive it from ok". Set explicitly for timeout and partial.
    outcome: str = ""

    def outcome_label(self) -> str:
        """success | failure | timeout | partial. Never blank."""
        if self.outcome:
            return self.outcome
        return OUTCOME_SUCCESS if self.ok else OUTCOME_FAILURE

    def payload(self) -> dict[str, Any]:
        label = self.outcome_label()
        if not self.ok:
            body: dict[str, Any] = {"ok": False, "error": self.error or "tool failed"}
            if label != OUTCOME_FAILURE:
                body["outcome"] = label
            # A timeout still has honest copy for the owner. Carry it so the model can
            # say what Mia was doing rather than invent a result it never got.
            if self.text:
                body["result"] = self.text[: self.max_chars]
            return body
        if label != OUTCOME_SUCCESS:
            return {"ok": True, "outcome": label, "result": self.text[: self.max_chars]}
        return {"ok": True, "result": self.text[: self.max_chars]}


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any]], ToolResult]
    writes_memory: bool = False


def _empty(value: str | None, fallback: str) -> ToolResult:
    if value is None or not str(value).strip():
        return ToolResult(ok=True, text=fallback)
    return ToolResult(ok=True, text=str(value).strip())


def _house_unavailable(ctx: ToolContext, label: str) -> ToolResult:
    if ctx.settings.composio_ready():
        return ToolResult(ok=False, error=f"{label} failed on the house Composio account.")
    return ToolResult(ok=True, text=_NOT_CONNECTED)


_NOT_CONNECTED = "Not connected yet. Assaf needs to finish this integration in Composio / env."


def _crm_spreadsheet_id(ctx: ToolContext) -> str:
    return ctx.settings.resolved_sheets_spreadsheet_id()


def utc_now() -> datetime:
    return datetime.now(UTC)
