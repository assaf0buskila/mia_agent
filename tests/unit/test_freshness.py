from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.capabilities import CapabilityId, require_alive
from app.domain.policies.freshness import (
    ALLOWLISTED_SOURCES,
    LISTED_FACTS,
    FreshnessClass,
    FreshnessStatus,
    freshness_pin,
    stamp_freshness,
)

_LIVE_ONLY_FACTS = (
    "calendar_availability",
    "owner_permissions",
    "opt_out_status",
)

_SHORT_CACHE_FACTS = (
    "instagram_content_metrics",
    "linkedin_profile",
    "gmail_results",
    "research_snippets",
    "gsc_search_metrics",
    "ga4_traffic_metrics",
    "seo_audit_snapshot",
    "lead_recent_messages",
    "website_session_events",
)

_VERSIONED_FACTS = (
    "services",
    "approved_pricing_rules",
    "security_explanation",
    "sales_playbooks",
    "case_studies",
    "communication_policies",
)

_SHORT_CACHE_TTLS = {
    "instagram_content_metrics": 300,
    "linkedin_profile": 300,
    "gmail_results": 300,
    "research_snippets": 300,
    "gsc_search_metrics": 300,
    "ga4_traffic_metrics": 300,
    "seo_audit_snapshot": 300,
    "lead_recent_messages": 60,
    "website_session_events": 60,
}


def test_all_listed_facts_pin() -> None:
    assert LISTED_FACTS == frozenset(
        _LIVE_ONLY_FACTS + _SHORT_CACHE_FACTS + _VERSIONED_FACTS
    )
    for fact in LISTED_FACTS:
        pin = freshness_pin(fact)
        assert pin.fact == fact
        assert pin.source in ALLOWLISTED_SOURCES


def test_live_only_facts_pin() -> None:
    for fact in _LIVE_ONLY_FACTS:
        pin = freshness_pin(fact)
        assert pin.freshness_class == FreshnessClass.LIVE_ONLY.value
        assert pin.ttl_seconds == 0
        assert pin.version == "none"


def test_short_cache_facts_pin() -> None:
    for fact in _SHORT_CACHE_FACTS:
        pin = freshness_pin(fact)
        assert pin.freshness_class == FreshnessClass.SHORT_CACHE.value
        assert pin.ttl_seconds == _SHORT_CACHE_TTLS[fact]
        assert pin.version == "none"


def test_versioned_knowledge_facts_pin() -> None:
    for fact in _VERSIONED_FACTS:
        pin = freshness_pin(fact)
        assert pin.freshness_class == FreshnessClass.VERSIONED_KNOWLEDGE.value
        assert pin.source == "none"
        assert pin.ttl_seconds == 0
        assert pin.version == "none"


def test_unknown_fail_closed() -> None:
    pin = freshness_pin("not_a_real_fact")
    assert pin.fact == "not_a_real_fact"
    assert pin.freshness_class == FreshnessClass.LIVE_ONLY.value
    assert pin.source == "none"
    assert pin.ttl_seconds == 0
    assert pin.version == "none"
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    stamp = stamp_freshness(
        "not_a_real_fact",
        present=True,
        fetched_at=now,
        now=now,
    )
    assert stamp.status == FreshnessStatus.UNVERIFIED.value
    assert stamp.source == "none"


def test_live_only_present_is_live() -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    stamp = stamp_freshness(
        "calendar_availability",
        present=True,
        fetched_at=now,
        now=now,
    )
    assert stamp.status == FreshnessStatus.LIVE.value
    assert stamp.source == "calendar_port"
    assert stamp.fetched_at == now


def test_live_only_missing_is_unverified() -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    stamp = stamp_freshness(
        "calendar_availability",
        present=False,
        fetched_at=now,
        now=now,
    )
    assert stamp.status == FreshnessStatus.UNVERIFIED.value


def test_short_cache_within_ttl_is_cached() -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    fetched_at = now - timedelta(seconds=120)
    stamp = stamp_freshness(
        "instagram_content_metrics",
        present=True,
        fetched_at=fetched_at,
        now=now,
    )
    assert stamp.status == FreshnessStatus.CACHED.value


def test_short_cache_past_ttl_is_stale() -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    fetched_at = now - timedelta(seconds=301)
    stamp = stamp_freshness(
        "instagram_content_metrics",
        present=True,
        fetched_at=fetched_at,
        now=now,
    )
    assert stamp.status == FreshnessStatus.STALE.value


def test_versioned_knowledge_present_still_unverified() -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    stamp = stamp_freshness(
        "services",
        present=True,
        fetched_at=now,
        now=now,
    )
    assert stamp.status == FreshnessStatus.UNVERIFIED.value
    assert stamp.source == "none"
    assert stamp.version == "none"


def test_freshness_policy_capability_alive() -> None:
    require_alive(CapabilityId.FRESHNESS_POLICY)


def test_orchestrator_does_not_import_stamp_freshness() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source = (repo_root / "app/graph/orchestrator.py").read_text(encoding="utf-8")
    assert "stamp_freshness" not in source
