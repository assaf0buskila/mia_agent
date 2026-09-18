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
    def _boom(*_args, **_kwargs):
        raise AssertionError("capability routing must never reach the model/provider path")

    monkeypatch.setattr("app.domain.owner.brain.build_agent_client", _boom)
    monkeypatch.setattr("app.integrations.llm_client.LlmClient", _boom)

    assert capability_request_kind("מה היכולות שלך") == "capabilities"
    assert isinstance(owner_capability_reply(), str)
    assert owner_capability_reply()  # non-empty
