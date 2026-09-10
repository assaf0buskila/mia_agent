"""Execute the owner loop with scripted model/provider fixtures.

Output is simulated evidence: production ``run_owner_agent`` is executed, while every
provider call is intercepted by a strict fixture dispatcher. No network or credentials.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.graph import owner_agent
from app.tools.registries.owner_tools import ToolResult
from tests.unit.test_brain_agent import (
    _assistant_text,
    _assistant_tool_call,
    _client,
    _ctx,
    _session,
)

os.environ.setdefault("MIA_DATABASE_URL", "sqlite://")


def _run(case: str) -> dict:
    scripts = {
        "crm": [
            _assistant_tool_call("crm-1", "crm_search", {"query": "Contacts"}),
            _assistant_text("fresh CRM sentinel"),
        ],
        "full_linkedin": [
            _assistant_tool_call("li-1", "linkedin_snapshot", {"full_profile": True}),
            _assistant_text("fresh LinkedIn sentinel"),
        ],
        "multi_source": [
            _assistant_tool_call("crm-1", "crm_search", {"query": "Contacts"}),
            _assistant_tool_call("li-1", "linkedin_snapshot", {"full_profile": True}),
            _assistant_text("fresh CRM and LinkedIn sentinels"),
        ],
    }
    client, transport = _client(scripts[case])
    provider_calls: list[str] = []

    def dispatch(name, arguments, ctx, **kwargs):
        provider_calls.append(name)
        evidence = "linkedin_profile" if name == "linkedin_snapshot" else ""
        return ToolResult(ok=True, text=f"fresh {name} sentinel", evidence=evidence)

    original = owner_agent._run_tool_with_timeout
    owner_agent._run_tool_with_timeout = dispatch
    try:
        outcome = owner_agent.run_owner_agent(
            client=client,
            ctx=_ctx(_session()),
            owner_message={
                "crm": "בדוק CRM",
                "full_linkedin": "Show my full LinkedIn profile",
                "multi_source": "Check CRM and my full LinkedIn profile",
            }[case],
        )
    finally:
        owner_agent._run_tool_with_timeout = original
    return {
        "completed": outcome.completed,
        "tools_used": list(outcome.tools_used),
        "provider_calls": provider_calls,
        "model_requests": len(transport.requests),
    }


def main() -> int:
    report = {
        "mode": "simulated_scripted_providers",
        "note": "Current production-loop behavior only; no before/after latency claim.",
        "cases": {},
    }
    for case in ("crm", "full_linkedin", "multi_source"):
        model_led = _run(case)
        assert model_led["completed"] is True
        assert model_led["model_requests"] >= 1
        if case == "full_linkedin":
            assert "linkedin_snapshot" in model_led["tools_used"]
        if case == "multi_source":
            assert {"crm_search", "linkedin_snapshot"}.issubset(model_led["tools_used"])
        report["cases"][case] = model_led
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
