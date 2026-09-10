"""Mechanical drift guards for the owner tool registry and two-state allowlist."""

from app.domain.two_state import FORBIDDEN_OWNER_TOOLS, OWNER_HOUSE_TOOLS
from app.tools.registries.owner_tools import get_tool, tool_names


def test_owner_state_allowlist_exactly_matches_registered_tools() -> None:
    registered = set(tool_names())
    assert registered == OWNER_HOUSE_TOOLS
    assert len(tool_names()) == len(registered)


def test_forbidden_owner_tools_are_not_registered() -> None:
    assert FORBIDDEN_OWNER_TOOLS.isdisjoint(tool_names())
    for name in FORBIDDEN_OWNER_TOOLS:
        assert get_tool(name) is None
