"""Typed task-class owner registry. Lookup only; not a live model router."""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MODEL_BRAND_RE = re.compile(r"(?i)gpt-|claude|gemini|grok|o1-")

ALLOWLISTED_OWNERS = frozenset({
    "code",
    "sales_reply_port",
    "owner_reply_port",
    "transcription_port",
    "thread_summary_port",
    "research_port",
    "linter",
})

ALLOWLISTED_MODEL_SOURCES = frozenset({"none", "env", "canned"})


class TaskClass(StrEnum):
    ROUTE = "route"
    EXTRACT = "extract"
    TRANSCRIBE = "transcribe"
    NORMAL_SALES_CONVERSATION = "normal_sales_conversation"
    OWNER_CONVERSATION = "owner_conversation"
    SALES_REFRAME = "sales_reframe"
    OBJECTION_HANDLING = "objection_handling"
    DEEP_RESEARCH = "deep_research"
    SUMMARIZATION = "summarization"
    MESSAGE_HUMANITY_REVIEW = "message_humanity_review"
    SAFETY_VERIFICATION = "safety_verification"


class TaskClassPin(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_class: str
    owner: str
    model_source: str
    benchmark_later: bool = False
    notes: str | None = Field(default=None, max_length=256)

    @field_validator("owner")
    @classmethod
    def owner_must_be_allowlisted(cls, value: str) -> str:
        if _MODEL_BRAND_RE.search(value):
            raise ValueError("owner must not contain model brand tokens")
        if value not in ALLOWLISTED_OWNERS:
            raise ValueError(f"unknown owner: {value}")
        return value

    @field_validator("notes")
    @classmethod
    def notes_must_not_contain_model_brands(cls, value: str | None) -> str | None:
        if value is not None and _MODEL_BRAND_RE.search(value):
            raise ValueError("notes must not contain model brand tokens")
        return value


def _pin(
    task_class: TaskClass,
    *,
    owner: str,
    model_source: str,
    benchmark_later: bool = False,
    notes: str | None = None,
) -> TaskClassPin:
    return TaskClassPin(
        task_class=task_class.value,
        owner=owner,
        model_source=model_source,
        benchmark_later=benchmark_later,
        notes=notes,
    )


_REGISTRY: dict[str, TaskClassPin] = {
    TaskClass.ROUTE.value: _pin(
        TaskClass.ROUTE,
        owner="code",
        model_source="none",
        notes="owner_tasks classify",
    ),
    TaskClass.EXTRACT.value: _pin(
        TaskClass.EXTRACT,
        owner="code",
        model_source="none",
        notes="app.domain.extract",
    ),
    TaskClass.TRANSCRIBE.value: _pin(
        TaskClass.TRANSCRIBE,
        owner="transcription_port",
        model_source="env",
        benchmark_later=True,
    ),
    TaskClass.NORMAL_SALES_CONVERSATION.value: _pin(
        TaskClass.NORMAL_SALES_CONVERSATION,
        owner="sales_reply_port",
        model_source="env",
        benchmark_later=True,
    ),
    TaskClass.OWNER_CONVERSATION.value: _pin(
        TaskClass.OWNER_CONVERSATION,
        owner="owner_reply_port",
        model_source="env",
        benchmark_later=True,
        notes="phrase typed owner RESULT; no tool selection",
    ),
    TaskClass.SALES_REFRAME.value: _pin(
        TaskClass.SALES_REFRAME,
        owner="sales_reply_port",
        model_source="env",
        benchmark_later=True,
    ),
    TaskClass.OBJECTION_HANDLING.value: _pin(
        TaskClass.OBJECTION_HANDLING,
        owner="code",
        model_source="none",
        notes="NBA objection; copy via sales_reply_port",
    ),
    TaskClass.DEEP_RESEARCH.value: _pin(
        TaskClass.DEEP_RESEARCH,
        owner="research_port",
        model_source="none",
        notes="Firecrawl search; no LLM synthesize",
    ),
    TaskClass.SUMMARIZATION.value: _pin(
        TaskClass.SUMMARIZATION,
        owner="thread_summary_port",
        model_source="env",
        benchmark_later=True,
    ),
    TaskClass.MESSAGE_HUMANITY_REVIEW.value: _pin(
        TaskClass.MESSAGE_HUMANITY_REVIEW,
        owner="linter",
        model_source="none",
        benchmark_later=True,
        notes="app.domain.humanity",
    ),
    TaskClass.SAFETY_VERIFICATION.value: _pin(
        TaskClass.SAFETY_VERIFICATION,
        owner="code",
        model_source="none",
        notes="risk policy",
    ),
}

_FAIL_CLOSED = TaskClassPin(
    task_class="unknown",
    owner="code",
    model_source="none",
    benchmark_later=False,
)


def task_class_pin(name: str) -> TaskClassPin:
    known = _REGISTRY.get(name)
    if known is not None:
        return known
    return _FAIL_CLOSED.model_copy(update={"task_class": name})
