"""Keep deterministic Composio enrich when the agent skips the live tool."""

from __future__ import annotations

from app.api.owner import _prefer_live_enrich
from app.domain.owner.tasks import OwnerTaskType


def test_prefer_live_enrich_when_agent_skipped_seo_tool() -> None:
    assert (
        _prefer_live_enrich(
            task_type=OwnerTaskType.SEO,
            tools_used=("search_memory",),
            live_ack="GSC clicks: 12",
        )
        is True
    )


def test_use_agent_text_when_seo_tool_was_called() -> None:
    assert (
        _prefer_live_enrich(
            task_type=OwnerTaskType.SEO,
            tools_used=("seo_snapshot",),
            live_ack="GSC clicks: 12",
        )
        is False
    )


def test_linkedin_and_instagram_same_rule() -> None:
    assert (
        _prefer_live_enrich(
            task_type=OwnerTaskType.LINKEDIN,
            tools_used=(),
            live_ack="headline",
        )
        is True
    )
    assert (
        _prefer_live_enrich(
            task_type=OwnerTaskType.ANALYTICS,
            tools_used=("instagram_insights",),
            live_ack="reach",
        )
        is False
    )


def test_other_tasks_always_keep_agent_text() -> None:
    assert (
        _prefer_live_enrich(
            task_type=OwnerTaskType.NOTE,
            tools_used=(),
            live_ack="canned",
        )
        is False
    )
