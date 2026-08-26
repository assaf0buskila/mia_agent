"""Freshness policy lookup and stamp helpers. Not a cache; not RAG."""

import re
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

_MAX_TTL_SECONDS = 86_400
_VERSION_RE = re.compile(r"^[a-zA-Z0-9._-]{1,32}$")

ALLOWLISTED_SOURCES = frozenset({
    "calendar_port",
    "instagram_insights_port",
    "linkedin_port",
    "config",
    "lead_store",
    "gmail_port",
    "research_port",
    "gsc_port",
    "ga4_port",
    "seo_audit_port",
    "none",
})


class FreshnessClass(StrEnum):
    LIVE_ONLY = "live_only"
    SHORT_CACHE = "short_cache"
    VERSIONED_KNOWLEDGE = "versioned_knowledge"


class FreshnessStatus(StrEnum):
    LIVE = "live"
    CACHED = "cached"
    STALE = "stale"
    UNVERIFIED = "unverified"


class FreshnessPin(BaseModel):
    model_config = ConfigDict(frozen=True)

    fact: str
    freshness_class: str
    source: str
    ttl_seconds: int = Field(ge=0, le=_MAX_TTL_SECONDS)
    version: str

    @field_validator("source")
    @classmethod
    def source_must_be_allowlisted(cls, value: str) -> str:
        if value not in ALLOWLISTED_SOURCES:
            raise ValueError(f"unknown source: {value}")
        return value

    @field_validator("freshness_class")
    @classmethod
    def freshness_class_must_be_allowlisted(cls, value: str) -> str:
        if value not in {item.value for item in FreshnessClass}:
            raise ValueError(f"unknown freshness_class: {value}")
        return value

    @field_validator("version")
    @classmethod
    def version_must_match_pattern(cls, value: str) -> str:
        if value != "none" and _VERSION_RE.fullmatch(value) is None:
            raise ValueError("version must be 'none' or match ^[a-zA-Z0-9._-]{1,32}$")
        return value

    @field_validator("ttl_seconds")
    @classmethod
    def ttl_must_match_class(cls, value: int, info: ValidationInfo) -> int:
        freshness_class = info.data.get("freshness_class")
        if freshness_class in {
            FreshnessClass.LIVE_ONLY.value,
            FreshnessClass.VERSIONED_KNOWLEDGE.value,
        } and value != 0:
            raise ValueError(f"{freshness_class} requires ttl_seconds=0")
        return value


class FreshnessStamp(BaseModel):
    model_config = ConfigDict(frozen=True)

    fact: str
    source: str
    fetched_at: datetime
    version: str
    status: str

    @field_validator("status")
    @classmethod
    def status_must_be_allowlisted(cls, value: str) -> str:
        if value not in {item.value for item in FreshnessStatus}:
            raise ValueError(f"unknown freshness status: {value}")
        return value


def _pin(
    fact: str,
    *,
    freshness_class: FreshnessClass,
    source: str,
    ttl_seconds: int = 0,
    version: str = "none",
) -> FreshnessPin:
    return FreshnessPin(
        fact=fact,
        freshness_class=freshness_class.value,
        source=source,
        ttl_seconds=ttl_seconds,
        version=version,
    )


_REGISTRY: dict[str, FreshnessPin] = {
    "calendar_availability": _pin(
        "calendar_availability",
        freshness_class=FreshnessClass.LIVE_ONLY,
        source="calendar_port",
    ),
    "conversation_ownership": _pin(
        "conversation_ownership",
        freshness_class=FreshnessClass.LIVE_ONLY,
        source="config",
    ),
    "owner_permissions": _pin(
        "owner_permissions",
        freshness_class=FreshnessClass.LIVE_ONLY,
        source="config",
    ),
    "opt_out_status": _pin(
        "opt_out_status",
        freshness_class=FreshnessClass.LIVE_ONLY,
        source="lead_store",
    ),
    "instagram_content_metrics": _pin(
        "instagram_content_metrics",
        freshness_class=FreshnessClass.SHORT_CACHE,
        source="instagram_insights_port",
        ttl_seconds=300,
    ),
    "linkedin_profile": _pin(
        "linkedin_profile",
        freshness_class=FreshnessClass.SHORT_CACHE,
        source="linkedin_port",
        ttl_seconds=300,
    ),
    "gmail_results": _pin(
        "gmail_results",
        freshness_class=FreshnessClass.SHORT_CACHE,
        source="gmail_port",
        ttl_seconds=300,
    ),
    "research_snippets": _pin(
        "research_snippets",
        freshness_class=FreshnessClass.SHORT_CACHE,
        source="research_port",
        ttl_seconds=300,
    ),
    "gsc_search_metrics": _pin(
        "gsc_search_metrics",
        freshness_class=FreshnessClass.SHORT_CACHE,
        source="gsc_port",
        ttl_seconds=300,
    ),
    "ga4_traffic_metrics": _pin(
        "ga4_traffic_metrics",
        freshness_class=FreshnessClass.SHORT_CACHE,
        source="ga4_port",
        ttl_seconds=300,
    ),
    "seo_audit_snapshot": _pin(
        "seo_audit_snapshot",
        freshness_class=FreshnessClass.SHORT_CACHE,
        source="seo_audit_port",
        ttl_seconds=300,
    ),
    "lead_recent_messages": _pin(
        "lead_recent_messages",
        freshness_class=FreshnessClass.SHORT_CACHE,
        source="lead_store",
        ttl_seconds=60,
    ),
    "website_session_events": _pin(
        "website_session_events",
        freshness_class=FreshnessClass.SHORT_CACHE,
        source="lead_store",
        ttl_seconds=60,
    ),
    "services": _pin(
        "services",
        freshness_class=FreshnessClass.VERSIONED_KNOWLEDGE,
        source="none",
    ),
    "approved_pricing_rules": _pin(
        "approved_pricing_rules",
        freshness_class=FreshnessClass.VERSIONED_KNOWLEDGE,
        source="none",
    ),
    "security_explanation": _pin(
        "security_explanation",
        freshness_class=FreshnessClass.VERSIONED_KNOWLEDGE,
        source="none",
    ),
    "sales_playbooks": _pin(
        "sales_playbooks",
        freshness_class=FreshnessClass.VERSIONED_KNOWLEDGE,
        source="none",
    ),
    "case_studies": _pin(
        "case_studies",
        freshness_class=FreshnessClass.VERSIONED_KNOWLEDGE,
        source="none",
    ),
    "communication_policies": _pin(
        "communication_policies",
        freshness_class=FreshnessClass.VERSIONED_KNOWLEDGE,
        source="none",
    ),
}

_FAIL_CLOSED = FreshnessPin(
    fact="unknown",
    freshness_class=FreshnessClass.LIVE_ONLY.value,
    source="none",
    ttl_seconds=0,
    version="none",
)

LISTED_FACTS = frozenset(_REGISTRY)


def freshness_pin(fact: str) -> FreshnessPin:
    known = _REGISTRY.get(fact)
    if known is not None:
        return known
    return _FAIL_CLOSED.model_copy(update={"fact": fact})


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def stamp_freshness(
    fact: str,
    *,
    present: bool,
    fetched_at: datetime,
    now: datetime | None = None,
) -> FreshnessStamp:
    pin = freshness_pin(fact)
    fetched_at_utc = _coerce_utc(fetched_at)
    now_utc = _coerce_utc(now or datetime.now(UTC))

    if not present or fact not in _REGISTRY:
        status = FreshnessStatus.UNVERIFIED
    elif pin.freshness_class == FreshnessClass.VERSIONED_KNOWLEDGE.value:
        status = FreshnessStatus.UNVERIFIED
    elif pin.freshness_class == FreshnessClass.LIVE_ONLY.value:
        status = FreshnessStatus.LIVE
    else:
        age_seconds = max(0.0, (now_utc - fetched_at_utc).total_seconds())
        if age_seconds <= pin.ttl_seconds:
            status = FreshnessStatus.CACHED
        else:
            status = FreshnessStatus.STALE

    return FreshnessStamp(
        fact=fact,
        source=pin.source,
        fetched_at=fetched_at_utc,
        version=pin.version,
        status=status.value,
    )


def overlay_stale(*, base_status: str, stamp: FreshnessStamp) -> str:
    if stamp.status == FreshnessStatus.STALE.value and base_status in {
        "ok",
        "partial",
        "empty",
    }:
        return "stale"
    return base_status
