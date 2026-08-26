from pydantic import BaseModel, Field, field_validator

ALLOWLISTED_TOOLS = frozenset({
    "calendar_find_free_slots",
    "calendar_booking_lookup",
    "calendar_booking_verify",
    "calendar_create",
    "calendar_reschedule_get",
    "calendar_patch_event",
    "calendar_reschedule_verify",
    "sheets_mirror",
    "sheets_mirror_content",
    "instagram_insights",
    "research_search",
    "meeting_research",
    "linkedin_profile",
    "voice_transcribe",
    "gmail_fetch",
    "gsc_search_analytics",
    "ga4_pivot_report",
    "seo_audit",
    "opt_out_status",
    "conversation_ownership",
    "owner_permissions",
    "lead_recent_messages",
    "website_session_events",
})

ALLOWLISTED_TOOL_STATUSES = frozenset({
    "ok",
    "denied",
    "empty",
    "error",
    "unauthorized",
    "rate_limited",
    "malformed",
    "retryable",
    "partial",
    "stale",
})

ALLOWLISTED_TOOL_FRESHNESS = frozenset({
    "",
    "live",
    "cached",
    "stale",
    "unverified",
})

_MAX_FRESHNESS_LEN = 16


class AdapterHttpError(Exception):
    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code

    def tool_status(self) -> str:
        return tool_status_from_http(self.status_code)


def tool_status_from_http(status_code: object) -> str:
    if status_code is None:
        return "retryable"
    if isinstance(status_code, bool):
        return "error"
    if not isinstance(status_code, int):
        return "error"
    if status_code in (401, 403):
        return "unauthorized"
    if status_code == 429:
        return "rate_limited"
    if status_code in (400, 422):
        return "malformed"
    if status_code in (408, 409, 425, 502, 503, 504) or status_code >= 500:
        return "retryable"
    if status_code >= 400:
        return "error"
    return "error"


_MAX_LATENCY_MS = 86_400_000


def clamp_tool_freshness(value: str) -> str:
    if value not in ALLOWLISTED_TOOL_FRESHNESS:
        return ""
    return value[:_MAX_FRESHNESS_LEN]


class ToolOutcome(BaseModel):
    tool: str
    status: str
    result_count: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    freshness: str = ""

    @field_validator("latency_ms", mode="before")
    @classmethod
    def clamp_latency_ms(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return max(0, min(value, _MAX_LATENCY_MS))

    @field_validator("tool")
    @classmethod
    def tool_must_be_allowlisted(cls, value: str) -> str:
        if value not in ALLOWLISTED_TOOLS:
            raise ValueError(f"unknown tool: {value}")
        return value

    @field_validator("status")
    @classmethod
    def status_must_be_allowlisted(cls, value: str) -> str:
        if value not in ALLOWLISTED_TOOL_STATUSES:
            raise ValueError(f"unknown tool status: {value}")
        return value

    @field_validator("freshness")
    @classmethod
    def freshness_must_be_allowlisted(cls, value: str) -> str:
        if value not in ALLOWLISTED_TOOL_FRESHNESS:
            raise ValueError(f"unknown tool freshness: {value}")
        return value
