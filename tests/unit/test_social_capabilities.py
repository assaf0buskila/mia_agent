"""C6a: social capability truth.

Covers the read-only `social_capabilities` tool (settings/readiness only, no
provider call), the "what this is NOT" clause on the four social tool
descriptions, and the social-writing rule injected only on a LinkedIn,
Instagram, or content-ideas turn.
"""

from __future__ import annotations

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.two_state import (
    OWNER_HOUSE_TOOLS,
    MiaState,
    is_social_writing_turn,
    may_run,
)
from app.graph.owner_agent import SOCIAL_WRITING_RULE, build_messages
from app.integrations.instagram_insights import (
    DisabledInstagramInsightsPort,
    FakeInstagramInsightsPort,
)
from app.integrations.linkedin import DisabledLinkedInPort, FakeLinkedInPort, LinkedInProfile
from app.tools.registries.owner_tools import ToolContext, execute_tool, get_tool, tool_names


def _session():
    init_db()
    return get_session_factory()()


def _ctx(session, **overrides) -> ToolContext:
    base = dict(
        principal=Principal.owner(source="test"),
        store=LeadStore(session),
        brain=BrainStore(session),
        settings=Settings(_env_file=None),
        embedding_port=FakeEmbeddingPort(),
        source_ref="telegram:test",
    )
    base.update(overrides)
    return ToolContext(**base)


def test_social_capabilities_is_registered_owner_only_and_argument_free() -> None:
    assert "social_capabilities" in tool_names()
    assert "social_capabilities" in OWNER_HOUSE_TOOLS
    assert may_run(state=MiaState.OWNER, tool="social_capabilities") is True
    assert may_run(state=MiaState.VISITOR, tool="social_capabilities") is False
    spec = get_tool("social_capabilities")
    assert spec is not None
    assert spec.writes_memory is False
    assert spec.parameters["required"] == []
    assert spec.parameters["additionalProperties"] is False


def test_social_capabilities_reports_not_configured_by_default() -> None:
    session = _session()
    try:
        result = execute_tool("social_capabilities", {}, _ctx(session))
        assert result.ok is True
        assert "LinkedIn profile read: not configured" in result.text
        assert "Instagram insights read: not configured" in result.text
        # These facts hold regardless of configuration.
        assert "Instagram publishing: not available" in result.text
        assert "No scheduling on either platform." in result.text
        assert "not yet verified live" in result.text
    finally:
        session.close()


def test_social_capabilities_reflects_composio_settings_readiness() -> None:
    session = _session()
    try:
        settings = Settings(_env_file=None)
        settings.composio_api_key = "key"
        settings.composio_user_id = "user"
        result = execute_tool("social_capabilities", {}, _ctx(session, settings=settings))
        assert "LinkedIn profile read: available" in result.text
        assert "Instagram insights read: available" in result.text
    finally:
        session.close()


def test_social_capabilities_reflects_an_already_resolved_port_over_settings() -> None:
    """Production always hands the tool loop an already-resolved port

    (`bind_owner_house_ports`); settings are only the fallback for a context built
    without one, e.g. directly in a test. An injected working port must be
    believed even when Composio settings are blank.
    """
    session = _session()
    try:
        ctx = _ctx(
            session,
            linkedin=FakeLinkedInPort(LinkedInProfile(name="Assaf Web")),
            instagram_insights=FakeInstagramInsightsPort([]),
        )
        result = execute_tool("social_capabilities", {}, ctx)
        assert "LinkedIn profile read: available" in result.text
        assert "Instagram insights read: available" in result.text
    finally:
        session.close()


def test_social_capabilities_treats_an_explicit_disabled_port_as_not_configured() -> None:
    """A pre-resolved Disabled* port -- what the real builders hand back when

    nothing is configured -- must read as not configured even if Composio
    settings happen to be set, exactly like linkedin_snapshot/instagram_insights
    would treat it.
    """
    session = _session()
    try:
        settings = Settings(_env_file=None)
        settings.composio_api_key = "key"
        settings.composio_user_id = "user"
        ctx = _ctx(
            session,
            settings=settings,
            linkedin=DisabledLinkedInPort(),
            instagram_insights=DisabledInstagramInsightsPort(),
        )
        result = execute_tool("social_capabilities", {}, ctx)
        assert "LinkedIn profile read: not configured" in result.text
        assert "Instagram insights read: not configured" in result.text
    finally:
        session.close()


def test_social_capabilities_makes_no_provider_call() -> None:
    """A port whose methods raise if invoked proves the tool never calls one --

    `social_capabilities` is settings/readiness only, per the brief.
    """

    class _PoisonedLinkedIn:
        def get_my_profile(self):
            raise AssertionError("social_capabilities must not call a port method")

    class _PoisonedInstagram:
        def list_recent_insights(self, *, limit: int = 5):
            raise AssertionError("social_capabilities must not call a port method")

    session = _session()
    try:
        ctx = _ctx(session, linkedin=_PoisonedLinkedIn(), instagram_insights=_PoisonedInstagram())
        result = execute_tool("social_capabilities", {}, ctx)
        assert result.ok is True
        assert "LinkedIn profile read: available" in result.text
        assert "Instagram insights read: available" in result.text
    finally:
        session.close()


def test_tool_descriptions_state_what_they_are_not() -> None:
    linkedin_desc = get_tool("linkedin_snapshot").description
    assert "NOT company or competitor data" in linkedin_desc
    assert "NOT post/follower/impression analytics" in linkedin_desc

    instagram_desc = get_tool("instagram_insights").description
    assert "NOT a publishing, comment, DM, or ads tool" in instagram_desc
    assert "never invented or shown as zero" in instagram_desc

    content_desc = get_tool("content_ideas").description
    assert "NOT a finished post" in content_desc
    assert "NOT a draft" in content_desc
    assert "nothing here is published" in content_desc

    linkedin_action_desc = get_tool("composio_propose_linkedin_action").description
    assert "NOT a verified publish" in linkedin_action_desc
    assert "not independently re-checked" in linkedin_action_desc


def test_social_writing_rule_injected_only_on_social_turns() -> None:
    for text in (
        "LinkedIn post about crm",
        "תבדקי את האינסטגרם",
        "give me content ideas",
        "רעיונות לתוכן",
    ):
        assert is_social_writing_turn(text) is True, text
        messages = build_messages(owner_message=text, history=(), context=None)
        assert SOCIAL_WRITING_RULE in messages[0]["content"], text

    for text in (
        "מה יש לי היום ביומן?",
        "what's on my calendar today",
        "check gmail",
        "היי",
    ):
        assert is_social_writing_turn(text) is False, text
        messages = build_messages(owner_message=text, history=(), context=None)
        assert SOCIAL_WRITING_RULE not in messages[0]["content"], text


def test_social_writing_rule_states_its_four_guardrails() -> None:
    """Pin the rule's substance, not just its presence, to the brief's four asks."""
    assert "observed data" in SOCIAL_WRITING_RULE
    assert "inference" in SOCIAL_WRITING_RULE
    assert "recommendation" in SOCIAL_WRITING_RULE
    assert "reach, performance, follower counts, or a best time to post" in SOCIAL_WRITING_RULE
    assert "not scheduled or published by writing it" in SOCIAL_WRITING_RULE
    assert "at most one clarifying question" in SOCIAL_WRITING_RULE
