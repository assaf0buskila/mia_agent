"""TG-CAP: natural Hebrew/English capability & tool-inventory phrasing routes locally.

Follows the conventions of test_owner_tool_inventory_route.py: no network, pure
functions of the registry, Settings(_env_file=None) where a Settings instance is
needed at all (none of these tests actually need one -- capability_request_kind and
owner_capability_reply are deliberately model/provider/DB-free).
"""

from __future__ import annotations

import re

import pytest
from app.domain.owner.request_routing import (
    _TOOL_GROUPS,
    capability_request_kind,
    owner_capability_reply,
    owner_tool_inventory_reply,
)
from app.tools.registries.owner_tools import tool_names

# -- 1. table-driven kind classification -------------------------------------------

_MUST_BE_CAPABILITIES = [
    "מה היכולות שלך",
    "מה את יכולה לעשות",
    "מה אפשר לעשות איתך",
    "תפרטי לי את כל היכולות שלך",
    "תפרטי לי פשוט את כל היכולות שלך, הכל",  # Assaf's exact live sentence (P0 timeout)
    "what can you do",
    "what are your capabilities",
    "show me everything you can do",
]

_MUST_BE_TOOLS = [
    "מה הכלים שלך",
    "איזה כלים יש לך",
    "תני לי את רשימת הכלים",
    "what tools do you have",
    "what are your tools",
    "list your tools",
]

_MUST_BE_EMPTY = [
    "מה הכלים שלך ותבדקי לי את המייל",
    "use your tools to check the calendar",
    "מה אפשר לעשות עם הליד הזה",
    "תפרטי לי את כל המיילים",
    "תפרטי לי את כל הפגישות של מחר",
    "את יכולה לשלוח מייל ללקוח",
    "אתה יכול לבדוק את היומן",
    "what tools does this client use",
    "מה היכולות של הכלי הזה",
    "תשלחי מייל ותגידי לי מה היכולות שלך",
    "",
    "   ",
    "?",
]


@pytest.mark.parametrize("text", _MUST_BE_CAPABILITIES, ids=range(len(_MUST_BE_CAPABILITIES)))
def test_natural_capability_phrasing_routes_to_capabilities(text: str) -> None:
    assert capability_request_kind(text) == "capabilities"


@pytest.mark.parametrize("text", _MUST_BE_TOOLS, ids=range(len(_MUST_BE_TOOLS)))
def test_narrow_tools_phrasing_still_routes_to_tools(text: str) -> None:
    assert capability_request_kind(text) == "tools"


@pytest.mark.parametrize("text", _MUST_BE_EMPTY, ids=range(len(_MUST_BE_EMPTY)))
def test_ordinary_business_requests_do_not_match(text: str) -> None:
    assert capability_request_kind(text) == ""


# -- 5b. filler additions do not swallow a business request -------------------------
# Every word added to _INVENTORY_FILLER weakens the empty-remainder guard for every
# OTHER phrase too. Each case below contains the specific added filler word and pins
# that the surrounding business request still falls through to "" (the model path),
# not a capability/tools short-circuit.

_FILLER_SAFETY_CASES = [
    ("פשוט", "תבדקי פשוט את המייל האחרון"),  # "just check the last email"
    ("את (accusative marker)", "תפרטי לי את כל המיילים"),  # covered above too
    ("את (pronoun 'you')", "את יכולה לשלוח מייל ללקוח"),
    ("כל", "תפרטי לי את כל הפגישות של מחר"),
    ("הכל", "תעדכני את הכל ברשומה של הליד"),  # "update everything in the lead's record"
    ("הכול", "תראי לי את הכול על הליד הזה"),  # "show me everything about this lead"
    ("me", "email me everything about this deal"),
    ("everything", "email me everything about this deal"),
    ("all", "check all the calendar events for today"),
    ("your", "email your report to the client"),
]


@pytest.mark.parametrize(
    ("filler_word", "text"), _FILLER_SAFETY_CASES, ids=[c[0] for c in _FILLER_SAFETY_CASES]
)
def test_filler_addition_does_not_swallow_business_request(filler_word: str, text: str) -> None:
    assert capability_request_kind(text) == "", (
        f"adding {filler_word!r} to the filler set must not turn {text!r} into a "
        "capability/tools short-circuit"
    )


# -- 2 & 4. owner_capability_reply content and drift guards --------------------------

_CATEGORY_LINE = re.compile(r"^• (?P<label>.+?) \((?P<count>\d+)\):", re.MULTILINE)


def test_capability_reply_covers_registered_categories_and_states_boundaries() -> None:
    reply = owner_capability_reply()

    registered = set(tool_names())
    group_labels = {label for label, names in _TOOL_GROUPS if names & registered}
    for label in group_labels:
        assert label in reply, f"category {label!r} has registered tools but is missing"

    # Registered-vs-live-connectivity boundary, same wording as owner_tool_inventory_reply.
    assert "לא בדיקת חיבור חיה" in reply
    # Approval path for any real side effect.
    assert "באישור" in reply
    # Exact tool names remain available on request -- nothing is lost vs the tools list.
    assert "שמות" in reply or "כלים" in reply


def test_capability_reply_category_labels_and_total_match_the_registry() -> None:
    reply = owner_capability_reply()
    valid_labels = {label for label, _names in _TOOL_GROUPS}

    matches = list(_CATEGORY_LINE.finditer(reply))
    assert matches, "expected at least one '• <label> (<count>):' category line"

    total = 0
    for match in matches:
        label = match.group("label")
        assert label in valid_labels, f"{label!r} is not a real _TOOL_GROUPS category"
        total += int(match.group("count"))

    assert total == len(tool_names())
    assert str(len(tool_names())) in reply


# -- 3. no model / provider / DB guard -----------------------------------------------


def test_capability_reply_and_kind_need_no_model_provider_or_db(monkeypatch) -> None:
    """The whole point of this route is that it costs nothing. Pin all three costs.

    This guard used to patch only `build_agent_client` and `LlmClient` -- neither of
    which `owner_capability_reply` references -- so it proved "does not build an
    agent client" and nothing about the DB or the network. A review demonstrated
    that by making the function open a session, run `select 1` and issue an httpx
    GET: the test stayed green. Everything the stated invariant covers is now
    blocked at the seam the function would actually have to go through.
    """
    import httpx

    def _boom(*_args, **_kwargs):
        raise AssertionError("capability routing must never reach the model/provider path")

    def _no_db(*_args, **_kwargs):
        raise AssertionError("capability routing must never touch the database")

    def _no_network(*_args, **_kwargs):
        raise AssertionError("capability routing must never open a network connection")

    monkeypatch.setattr("app.domain.owner.brain.build_agent_client", _boom)
    monkeypatch.setattr("app.integrations.llm_client.LlmClient", _boom)
    # A regression would reach the DB through the session factory ...
    monkeypatch.setattr("app.db.session.get_session_factory", _no_db)
    # ... and any provider or health check through a client or a module-level
    # helper. Patching `httpx.Client` alone was not enough: a review showed
    # `httpx.get(...)` -- the most idiomatic one-liner of all -- sailed straight
    # past it, because `httpx._api.get` builds its client from
    # `httpx._client.Client`, not from the `httpx.Client` alias. So both the
    # underlying class and the module-level verbs are blocked here.
    monkeypatch.setattr("httpx._client.Client", _no_network)
    monkeypatch.setattr("httpx._client.AsyncClient", _no_network)
    monkeypatch.setattr(httpx, "Client", _no_network)
    monkeypatch.setattr(httpx, "AsyncClient", _no_network)
    for verb in ("get", "post", "put", "patch", "delete", "head", "options", "request"):
        monkeypatch.setattr(httpx, verb, _no_network)

    assert capability_request_kind("מה היכולות שלך") == "capabilities"
    assert isinstance(owner_capability_reply(), str)
    assert owner_capability_reply()  # non-empty
    # The narrow tool-name inventory shares the registry lookup and the same promise.
    assert owner_tool_inventory_reply()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The lead-in is stripped, so the anchored capability pattern is reached.
        ("תגידי לי מה את יכולה לעשות", "capabilities"),
        ("תגיד לי מה היכולות שלך", "capabilities"),
        ("tell me what you can do", "capabilities"),
        # ... but stripping it can never turn an ordinary request into a meta one:
        # the remainder still has to be a complete capability question and nothing
        # else. These are the two shapes that prove the widening is bounded.
        ("תגידי לי מה יש ביומן", ""),
        ("תגידי לי מה הכלים שלך ותשלחי מייל", ""),
        ("tell me the latest lead", ""),
    ],
    ids=[
        "tell-me-can-do",
        "tell-me-capabilities",
        "tell-me-en",
        "tell-me-calendar-is-business",
        "tell-me-tools-plus-action-is-business",
        "tell-me-lead-is-business",
    ],
)
def test_tell_me_lead_in_is_stripped_without_widening_the_guard(
    text: str, expected: str
) -> None:
    assert capability_request_kind(text) == expected


def test_scoped_capability_questions_still_reach_the_model() -> None:
    """A narrower question about one area is a real request, not a meta request.

    These deliberately return "" so the model answers them with live detail. Pinned
    because the filler set is the thing most likely to drift into swallowing them.
    """
    for text in (
        "מה היכולות שלך ב-CRM",
        "what are your capabilities for email",
        "what you can do for this client",
        "מה היכולות שלנו",
    ):
        assert capability_request_kind(text) == "", text


# ---------------------------------------------------------------------------
# Review round 2, F1: ingress normalisation. Mia's own egress inserts bidi and
# zero-width controls, so her text round-trips back in carrying them.
# ---------------------------------------------------------------------------

BIDI_AND_ZERO_WIDTH = (
    "\u200b",  # ZWSP
    "\u200c",  # ZWNJ
    "\u200d",  # ZWJ
    "\u200e",  # LRM
    "\u200f",  # RLM -- owner_text() appends one of these per line
    "\u202a",  # LRE
    "\u202b",  # RLE
    "\u202c",  # PDF
    "\u202d",  # LRO
    "\u202e",  # RLO
    "\u2066",  # LRI
    "\u2067",  # RLI
    "\u2068",  # FSI -- isolate() wraps LTR runs in FSI...PDI
    "\u2069",  # PDI
    "\ufeff",  # BOM / ZWNBSP
)

ASSAF_EXACT = "תפרטי לי פשוט את כל היכולות שלך, הכל"


@pytest.mark.parametrize(
    "mark", BIDI_AND_ZERO_WIDTH, ids=[hex(ord(c)) for c in BIDI_AND_ZERO_WIDTH]
)
@pytest.mark.parametrize("position", ["leading", "trailing", "interior"])
def test_invisible_marks_do_not_defeat_the_capability_route(mark: str, position: str) -> None:
    """One stray codepoint must not send the exact live sentence back to the model.

    Before normalisation a single trailing U+200F was enough: the empty-remainder
    rule saw a leftover character and returned "". Invisible in Telegram, and
    indistinguishable from the fix never having shipped.
    """
    if position == "leading":
        text = mark + ASSAF_EXACT
    elif position == "trailing":
        text = ASSAF_EXACT + mark
    else:
        head, _, tail = ASSAF_EXACT.partition(" ")
        text = f"{head}{mark} {tail}"
    assert capability_request_kind(text) == "capabilities", repr(text)


def test_invisible_marks_cannot_turn_a_business_request_into_a_capability_one() -> None:
    """Normalisation only ever shrinks the remainder -- it must not widen acceptance."""
    for mark in BIDI_AND_ZERO_WIDTH:
        assert capability_request_kind(f"תבדקי לי את המייל{mark}") == ""
        assert capability_request_kind(f"{mark}מה הכלים שלך ותבדקי לי את המייל") == ""
        assert capability_request_kind(mark) == ""
        assert capability_request_kind(mark * 5) == ""


def test_mias_own_egress_really_does_feed_marks_back_into_this_route() -> None:
    """The normalisation is load-bearing, not merely defence in depth.

    Two corrections are baked into this test, in order.

    A review argued Mia's own egress feeds these marks back: `owner_text` wraps LTR
    runs in FSI...PDI, so a copy-paste of her text carries them. The first response
    was that this is only half true, because `owner_text` is a no-op on pure-Hebrew
    and pure-Latin text and "every phrasing that actually routes is one or the
    other". That second half was wrong. A Hebrew capability question carrying an
    English filler word -- `please`, `mia`, `just`, `available`, `tell` are all
    filler -- both ROUTES and is mixed-script, so egress marks it up. One case even
    gains a U+200F.

    So the round trip really is reachable: Mia sends a line, Assaf copies part of it
    back, and before normalisation the invisible marks pushed it to the model path.
    Kept measured rather than argued, because both earlier readings of this were
    confidently stated and wrong.
    """
    import unicodedata

    from app.integrations.telegram_format import owner_text

    # Pure script: egress genuinely is a no-op, so nothing to round-trip.
    for text in (ASSAF_EXACT, "what can you do"):
        assert owner_text(text, html=False) == text

    # Mixed script that DOES route: egress inserts marks, and the round trip still
    # routes to the same kind. Before the normalisation these returned "".
    mixed_and_routing = (
        ("מה הכלים שלך please", "tools"),
        ("tell me מה הכלים שלך", "tools"),
        ("מה היכולות שלך available", "capabilities"),
    )
    for text, expected in mixed_and_routing:
        assert capability_request_kind(text) == expected, text
        marked = owner_text(text, html=False)
        assert marked != text, f"egress inserted nothing into {text!r}"
        assert any(unicodedata.category(ch) == "Cf" for ch in marked), marked
        assert capability_request_kind(marked) == expected, repr(marked)

    # And a mixed-script phrase carrying a real scope stays a business request,
    # marks or no marks: the scope survives normalisation and keeps the remainder
    # non-empty.
    scoped = "מה הכלים שלך CRM"
    assert capability_request_kind(scoped) == ""
    assert capability_request_kind(owner_text(scoped, html=False)) == ""


# ---------------------------------------------------------------------------
# Review round 2, F5: "show me what you can do" is the more natural phrasing and
# was rejected while "show me everything you can do" was accepted.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "show me what you can do",
        "show me everything you can do",
        "show what you can do",
    ],
    ids=["show-what", "show-everything", "show-bare"],
)
def test_both_show_phrasings_route_the_same_way(text: str) -> None:
    assert capability_request_kind(text) == "capabilities"


def test_show_phrasing_still_rejects_a_scoped_or_compound_request() -> None:
    for text in (
        "show me what you can do for this client",
        "show me the latest lead",
        "show me what you can do and send the email",
    ):
        assert capability_request_kind(text) == "", text


# ---------------------------------------------------------------------------
# Review round 2, F6: two invariants that no test could break.
# ---------------------------------------------------------------------------


def test_every_advertised_category_carries_a_real_description() -> None:
    """A category must never be announced with an empty blurb.

    Deleting one entry from the blurb table used to render "• מחקר ציבורי (1): "
    -- a capability advertised with nothing after the colon -- and the whole suite
    stayed green.
    """
    reply = owner_capability_reply()
    advertised = [line for line in reply.splitlines() if line.startswith("•")]
    assert advertised, reply
    for line in advertised:
        label, _, description = line.partition(":")
        assert description.strip(), f"category advertised with no description: {label!r}"
        # A count in parentheses, not a bare label.
        assert "(" in label and ")" in label, label


def test_capability_reply_never_advertises_an_unregistered_ability() -> None:
    """Every advertised category must map to at least one genuinely registered tool."""
    registered = set(tool_names())
    reply = owner_capability_reply()
    for label, names in _TOOL_GROUPS:
        live = [name for name in names if name in registered]
        mentioned = any(line.startswith(f"• {label}") for line in reply.splitlines())
        if mentioned:
            assert live, f"{label!r} is advertised but has no registered tool"
        else:
            assert not live, f"{label!r} has registered tools but is not advertised"


# ---------------------------------------------------------------------------
# Review round 2, F5 (full): the remaining natural phrasings the reviewer listed
# as still taking the slow model path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "what else can you do",
        "what are you capable of",
        "tell me your capabilities",
        "list your capabilities",
        "let me know what you can do",
        "capabilities",
        "מה עוד את יכולה לעשות",
        "מה עוד אפשר לעשות איתך",
        "איזה יכולות יש לך",
        "היכולות שלך",
        "אני רוצה לדעת מה היכולות שלך",
    ],
    ids=[
        "what-else",
        "capable-of",
        "tell-me-caps",
        "list-caps",
        "let-me-know",
        "bare-caps",
        "od-can-do",
        "od-with-you",
        "which-caps",
        "bare-caps-he",
        "want-to-know",
    ],
)
def test_remaining_natural_capability_phrasings_route(text: str) -> None:
    assert capability_request_kind(text) == "capabilities"


@pytest.mark.parametrize(
    "text",
    ["הצג את כל הכלים שלך", "תפרטי את הכלים שלך", "מה הכלים הזמינים שלך"],
    ids=["show-all-tools", "detail-tools", "available-tools"],
)
def test_remaining_natural_tools_phrasings_route(text: str) -> None:
    assert capability_request_kind(text) == "tools"


@pytest.mark.parametrize(
    "text",
    [
        # `עוד` / `else` as filler must not swallow a real question.
        "מה עוד יש ביומן",
        "what else is on the calendar",
        # The multi-word lead-ins.
        "אני רוצה לדעת מה יש ביומן",
        "i want to know the latest lead",
        "let me know when dana replies",
        # The bare noun-phrase patterns.
        "תעדכני את היכולות שלך במסמך",
        "your capabilities are limited",
        "tell me the capabilities of this tool",
        "update capabilities in the doc",
        "list your capabilities and send the email",
        # Someone else's tools/capabilities, not Mia's.
        "איזה יכולות יש ללקוח",
        "הצג את כל הכלים של דני",
        "תפרטי את הכלים שבשימוש הלקוח",
        # `הזמינים` as filler.
        "מה הפגישות הזמינות מחר",
        # Scoped, so still a real request.
        "מה עוד את יכולה לעשות עם הליד הזה",
    ],
)
def test_the_round_two_widenings_swallow_no_business_request(text: str) -> None:
    assert capability_request_kind(text) == "", text


# The reviewer's own methodology, kept as a permanent guard: every anchor crossed
# with realistic business prefixes and suffixes. Any combination that still routes
# is a phrase where Mia would answer "here is what I can do" instead of doing the
# work -- the one failure mode that matters for this route.
_SWEEP_ANCHORS = (
    "מה היכולות שלך",
    "מה עוד את יכולה לעשות",
    "איזה יכולות יש לך",
    "היכולות שלך",
    "what can you do",
    "what else can you do",
    "what are you capable of",
    "capabilities",
    "list your capabilities",
    "tell me your capabilities",
    "show me what you can do",
    "הצג את כל הכלים שלך",
    "תפרטי את הכלים שלך",
    "מה הכלים הזמינים שלך",
    "מה הכלים שלך",
    "אני רוצה לדעת מה היכולות שלך",
    "תפרטי לי פשוט את כל היכולות שלך, הכל",
)
_SWEEP_PREFIXES = (
    "",
    "תבדקי ",
    "תשלחי מייל ו",
    "אחרי שתסיימי ",
    "לדני כהן ",
    "בשביל הליד ",
    "check ",
    "send an email and ",
    "for acme ",
    "before you ",
)
_SWEEP_SUFFIXES = (
    "",
    " ותבדקי את היומן",
    " לדני",
    " על הליד הזה",
    " במסמך",
    " של הלקוח",
    " and send the email",
    " for this client",
    " to dana",
    " in the doc",
)


def test_no_anchor_plus_business_text_is_ever_treated_as_a_meta_request() -> None:
    leaks = []
    for anchor in _SWEEP_ANCHORS:
        for prefix in _SWEEP_PREFIXES:
            for suffix in _SWEEP_SUFFIXES:
                if not prefix and not suffix:
                    continue  # the bare anchor is supposed to route
                text = f"{prefix}{anchor}{suffix}"
                kind = capability_request_kind(text)
                if kind:
                    leaks.append((text, kind))
    assert leaks == [], leaks


def test_every_sweep_anchor_still_routes_on_its_own() -> None:
    """The other half: the sweep above is only meaningful if the anchors work."""
    for anchor in _SWEEP_ANCHORS:
        assert capability_request_kind(anchor), anchor
