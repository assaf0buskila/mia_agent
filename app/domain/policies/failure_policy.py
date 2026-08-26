"""Per-node failure policy registry. Lookup only; does not retry adapters."""

from pydantic import BaseModel, Field, field_validator

from app.domain.tools import ALLOWLISTED_TOOLS

ALLOWLISTED_FALLBACKS = frozenset({
    "omit",
    "canned_slots",
    "deny",
    "skip",
    "empty",
    "canned",
})

_DEFAULT_TIMEOUT_MS = 15_000


class NodeFailurePolicy(BaseModel):
    node: str
    timeout_ms: int = Field(ge=0, le=120_000)
    maximum_retries: int = Field(ge=0, le=5)
    fail_closed: bool = True
    fallback: str
    notify_owner: bool = False

    @field_validator("fallback")
    @classmethod
    def fallback_must_be_allowlisted(cls, value: str) -> str:
        if len(value) > 32:
            raise ValueError("fallback token too long")
        if value not in ALLOWLISTED_FALLBACKS:
            raise ValueError(f"unknown fallback: {value}")
        return value


def _pin(
    node: str,
    *,
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    maximum_retries: int = 0,
    fallback: str,
) -> NodeFailurePolicy:
    return NodeFailurePolicy(
        node=node,
        timeout_ms=timeout_ms,
        maximum_retries=maximum_retries,
        fail_closed=True,
        fallback=fallback,
        notify_owner=False,
    )


_REGISTRY: dict[str, NodeFailurePolicy] = {}

for _node in ("instagram_insights",):
    _REGISTRY[_node] = _pin(_node, fallback="omit")

_REGISTRY["calendar_find_free_slots"] = _pin(
    "calendar_find_free_slots", fallback="canned_slots"
)

for _node in (
    "calendar_booking_lookup",
    "calendar_booking_verify",
    "calendar_create",
    "calendar_reschedule_get",
    "calendar_patch_event",
    "calendar_reschedule_verify",
):
    _REGISTRY[_node] = _pin(_node, fallback="deny")

for _node in ("research_search", "meeting_research"):
    _REGISTRY[_node] = _pin(_node, fallback="omit")

for _node in ("gsc_search_analytics", "ga4_pivot_report", "seo_audit"):
    _REGISTRY[_node] = _pin(_node, fallback="omit")

for _node in ("linkedin_profile",):
    _REGISTRY[_node] = _pin(_node, fallback="omit")

_REGISTRY["sheets_mirror"] = _pin("sheets_mirror", fallback="skip")
_REGISTRY["sheets_mirror_campaign"] = _pin("sheets_mirror_campaign", fallback="skip")
_REGISTRY["sheets_mirror_content"] = _pin("sheets_mirror_content", fallback="skip")
_REGISTRY["voice_transcribe"] = _pin(
    "voice_transcribe", timeout_ms=30_000, maximum_retries=1, fallback="empty"
)
_REGISTRY["sales_reply"] = _pin(
    "sales_reply", timeout_ms=30_000, maximum_retries=1, fallback="canned"
)
_REGISTRY["meta_write"] = _pin("meta_write", timeout_ms=0, fallback="deny")
_REGISTRY["gmail_fetch"] = _pin("gmail_fetch", fallback="empty")
_REGISTRY["opt_out_status"] = _pin("opt_out_status", fallback="omit")

for _node in (
    "conversation_ownership",
    "owner_permissions",
    "lead_recent_messages",
    "website_session_events",
):
    _REGISTRY[_node] = _pin(_node, fallback="omit")

assert ALLOWLISTED_TOOLS <= frozenset(_REGISTRY)


def failure_policy_for(node: str) -> NodeFailurePolicy:
    known = _REGISTRY.get(node)
    if known is not None:
        return known.model_copy(deep=True)
    return NodeFailurePolicy(
        node=node,
        timeout_ms=0,
        maximum_retries=0,
        fail_closed=True,
        fallback="omit",
        notify_owner=False,
    )
