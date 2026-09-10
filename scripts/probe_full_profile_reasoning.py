"""Real owner model with synthetic profile tools; no provider reads or writes.

Scripts are not packaged in the image. Pass source to run_ecs_command.py
--task-definition mia:REV -- python -c SOURCE.
"""

import json
import logging
import sys
from time import monotonic
from types import SimpleNamespace

import app.graph.owner_agent as agent
from app.core.config import get_settings
from app.domain.memory import ConversationTurn
from app.domain.owner.brain import build_agent_client
from app.tools.owner.types import ToolResult


def main():
    logging.disable(logging.CRITICAL)
    settings = get_settings()
    calls = []

    def fixture_tool(name, arguments, ctx, **kwargs):
        calls.append(name)
        if name == "linkedin_snapshot":
            return ToolResult(ok=True, text=(
                "Fresh LinkedIn profile: name Test Person; headline CURRENT_PROFILE_2026; "
                "About: Builds scheduling software. Experience: Test Company. "
                "Education: not returned by provider."
            ))
        if name == "composio_search_tools":
            return ToolResult(ok=True, text="No additional profile tools available.")
        return ToolResult(ok=False, error="No additional tool is available in this fixture.")

    agent._run_tool_with_timeout = fixture_tool
    client = build_agent_client(settings)
    result = agent.run_owner_agent(
        client=client, ctx=SimpleNamespace(settings=settings),
        owner_message="Show my full LinkedIn profile. Include the exact headline.",
        history=(ConversationTurn(role="mia", text="Old headline: STALE_PROFILE_2020."),),
        deadline_at=monotonic() + 60,
    )
    checks = {
        "completed": result.completed,
        "fresh_headline": "CURRENT_PROFILE_2026" in result.text,
        "old_headline_not_reused": "STALE_PROFILE_2020" not in result.text,
        "live_read_and_discovery": calls[:2] == ["linkedin_snapshot", "composio_search_tools"],
    }
    passed = all(checks.values())
    print(json.dumps({"result": "PASS" if passed else "FAIL", "checks": checks,
                      "tools": calls, "tokens_in": result.tokens_in,
                      "tokens_out": result.tokens_out}))
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "error_type": type(exc).__name__}))
        sys.exit(1)
