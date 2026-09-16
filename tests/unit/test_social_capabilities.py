"""C6a: social capability truth.

Covers the read-only `social_capabilities` tool (settings/readiness only, no
provider call), the "what this is NOT" clause on the four social tool
descriptions, and the social-writing rule injected only on a LinkedIn,
Instagram, or content-ideas turn.
"""

from __future__ import annotations

import inspect
import re

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.owner.connection_audit import OwnerAuditResult, _status
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
from app.integrations.telegram_format import owner_text
from app.tools.owner.operations import _owner_system_audit
from app.tools.owner.types import _NOT_CONNECTED as HOUSE_NOT_CONNECTED
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
        assert "קריאת פרופיל LinkedIn: לא מוגדר" in result.text
        assert "קריאת תובנות Instagram: לא מוגדר" in result.text
        # These facts hold regardless of configuration.
        assert "פרסום ב-Instagram: לא זמין" in result.text
        assert "אין תזמון באף אחת מהפלטפורמות." in result.text
    finally:
        session.close()


def test_social_capabilities_unconfigured_linkedin_does_not_promise_the_write_path() -> None:
    """P3-2 regression: an unconfigured install must not describe the

    approval-then-execution flow as if it were currently usable -- there is no
    active LinkedIn connection to propose an action against.
    """
    session = _session()
    try:
        result = execute_tool("social_capabilities", {}, _ctx(session))
        assert "קריאת פרופיל LinkedIn: לא מוגדר" in result.text
        assert "פוסט או תגובה ב-LinkedIn: לא זמין" in result.text
        assert "אין חיבור LinkedIn פעיל להציע מולו" in result.text
        assert "עוד לא אומת בשידור חי" not in result.text
    finally:
        session.close()


def test_social_capabilities_configured_linkedin_states_the_write_path() -> None:
    """The write-path line only appears once LinkedIn is actually configured --

    proves the P3-2 gate branches both ways, not just to "not available".
    """
    session = _session()
    try:
        ctx = _ctx(session, linkedin=FakeLinkedInPort(LinkedInProfile(name="Assaf Web")))
        result = execute_tool("social_capabilities", {}, ctx)
        assert "קריאת פרופיל LinkedIn: זמין" in result.text
        assert "עוד לא אומת בשידור חי" in result.text
        assert "פוסט או תגובה ב-LinkedIn: לא זמין" not in result.text
    finally:
        session.close()


def test_social_capabilities_reflects_composio_settings_readiness() -> None:
    session = _session()
    try:
        settings = Settings(_env_file=None)
        settings.composio_api_key = "key"
        settings.composio_user_id = "user"
        result = execute_tool("social_capabilities", {}, _ctx(session, settings=settings))
        assert "קריאת פרופיל LinkedIn: זמין" in result.text
        assert "קריאת תובנות Instagram: זמין" in result.text
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
        assert "קריאת פרופיל LinkedIn: זמין" in result.text
        assert "קריאת תובנות Instagram: זמין" in result.text
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
        assert "קריאת פרופיל LinkedIn: לא מוגדר" in result.text
        assert "קריאת תובנות Instagram: לא מוגדר" in result.text
    finally:
        session.close()


def test_social_capabilities_agrees_with_instagram_insights_on_a_direct_graph_only_config() -> (
    None
):
    """P3-3 regression: a direct-Graph-token config with no bound port and no

    Composio must be read the same way by both tools. `build_instagram_insights_port`
    has a direct-Graph-token path that returns a real (non-Disabled) port even
    when Composio is not ready, but `_instagram_insights` only calls that builder
    when `composio_ready()` -- so with no pre-bound port and Composio unready, the
    real read tool reports "not connected" even though direct-Graph settings are
    present. `social_capabilities` must report the same "not configured", not
    "available" from unconditionally trying the builder's other path.
    """
    session = _session()
    try:
        settings = Settings(_env_file=None)
        settings.instagram_access_token = "token"
        settings.instagram_account_id = "123"
        assert settings.composio_ready() is False
        ctx = _ctx(session, settings=settings)

        read_result = execute_tool("instagram_insights", {}, ctx)
        assert read_result.ok is True
        assert "Not connected" in read_result.text

        caps_result = execute_tool("social_capabilities", {}, ctx)
        assert "קריאת תובנות Instagram: לא מוגדר" in caps_result.text
        assert "קריאת תובנות Instagram: זמין" not in caps_result.text
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
        assert "קריאת פרופיל LinkedIn: זמין" in result.text
        assert "קריאת תובנות Instagram: זמין" in result.text
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


# --- Social pass: the rendered capability text is Hebrew ---------------------
#
# `format_social_capabilities` renders the one owner-facing capability *answer*.
# The tool `description`s and `SOCIAL_WRITING_RULE` asserted above are model-facing
# prompt text and stay English on purpose: they are instructions to the model, not
# anything Assaf ever reads.

FSI = "⁨"
PDI = "⁩"
_HEBREW = re.compile(r"[֐-׿]")


def test_capability_text_is_hebrew_with_only_brand_names_in_latin() -> None:
    """Every verdict reads in Hebrew; the only Latin runs left are brand names.

    Deliberately not a blanket "no ASCII" check: `LinkedIn` and `Instagram` are
    proper nouns and must stay Latin. What must not survive is an English verdict
    or an English sentence, which is what made this the one owner-facing read
    answering in a different language from every other one.
    """
    session = _session()
    try:
        result = execute_tool("social_capabilities", {}, _ctx(session))
        latin_runs = {run.strip() for run in re.findall(r"[A-Za-z][A-Za-z ]*", result.text)}
        assert latin_runs == {"LinkedIn", "Instagram"}, latin_runs
        assert _HEBREW.search(result.text) is not None
    finally:
        session.close()


def test_capability_text_keeps_every_claim_when_nothing_is_configured() -> None:
    session = _session()
    try:
        result = execute_tool("social_capabilities", {}, _ctx(session))
        assert "קריאת פרופיל LinkedIn: לא מוגדר" in result.text
        assert "קריאת תובנות Instagram: לא מוגדר" in result.text
        assert "פוסט או תגובה ב-LinkedIn: לא זמין" in result.text
        assert "אין חיבור LinkedIn פעיל להציע מולו" in result.text
        # True regardless of configuration.
        assert "פרסום ב-Instagram: לא זמין, חסום במדיניות" in result.text
        assert "הודעות ישירות ומודעות ב-Instagram: לא זמינים בשום מסלול" in result.text
        assert "אין תזמון באף אחת מהפלטפורמות" in result.text
        assert "לא פוסט שפורסם" in result.text
    finally:
        session.close()


def test_capability_text_keeps_every_claim_when_linkedin_is_configured() -> None:
    session = _session()
    try:
        ctx = _ctx(session, linkedin=FakeLinkedInPort(LinkedInProfile(name="Assaf Web")))
        result = execute_tool("social_capabilities", {}, ctx)
        assert "קריאת פרופיל LinkedIn: זמין" in result.text
        assert "עוד לא אומת בשידור חי" in result.text
        assert "פוסט או תגובה ב-LinkedIn: לא זמין" not in result.text
        # The policy denial does not soften because something else got connected.
        assert "פרסום ב-Instagram: לא זמין, חסום במדיניות" in result.text
    finally:
        session.close()


def test_capability_text_survives_the_c9_egress_normaliser() -> None:
    """C9 integration: the Latin brand runs get isolated at egress, not here.

    `owner_text()` isolates LTR runs only when the string contains Hebrew, so this
    property could not hold while the text was English -- it is new behaviour the
    translation buys, and it is what stops `LinkedIn`/`Instagram` reordering inside
    the surrounding RTL text on a real client. Isolation belongs at egress:
    hand-isolating here would apply a builder-level primitive to whole prose.
    """
    session = _session()
    try:
        result = execute_tool("social_capabilities", {}, _ctx(session))
        rendered = owner_text(result.text)
        for brand in ("LinkedIn", "Instagram"):
            assert f"{FSI}{brand}{PDI}" in rendered
            # No bare, unisolated occurrence may remain anywhere.
            assert re.search(f"(?<!{FSI}){brand}", rendered) is None
        assert owner_text(rendered) == rendered, "owner_text must stay idempotent"
    finally:
        session.close()


def test_connection_audit_not_connected_marker_is_independent_of_this_module() -> None:
    """The audit's English marker comes from `_house_unavailable`, not from here.

    `connection_audit._status` classifies a probe as unconnected by matching the
    literal English "not connected" / "not configured" in the probed tool's own
    text, and `format_social_capabilities` used to be the loudest producer of the
    second phrase. The two never met -- `social_capabilities` is not one of the
    audit's probes -- and that is the only reason translating this module is safe
    rather than a silent break. Pin both halves so a later change cannot quietly
    couple them: the marker still works from its real producer, and this module is
    still not a probe.
    """
    assert "not connected" in HOUSE_NOT_CONNECTED.casefold()
    probe = OwnerAuditResult(label="LinkedIn profile", ok=True, text=HOUSE_NOT_CONNECTED)
    assert _status(probe) == "לא מחובר או לא מוגדר"
    assert "_social_capabilities" not in inspect.getsource(_owner_system_audit)
