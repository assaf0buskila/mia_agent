"""Offline coverage for the live synthetic owner probe harness."""

from __future__ import annotations

import pytest
from app.integrations.llm_client import LlmResponse, ToolCall
from scripts import probe_owner_model_led_live as probe


class _ScriptedClient:
    model = "gpt-synthetic"
    last_model = "gpt-synthetic"

    def __init__(self, case: str, enabled: bool = True) -> None:
        self.case = case
        self.enabled_value = enabled
        self.index = 0

    def enabled(self) -> bool:
        return self.enabled_value

    def complete(self, **kwargs):
        scripts = {
            "crm": [("crm_search", {"query": "contacts"}), ("text", "maya.synthetic@example.test")],
            "full_linkedin": [
                ("linkedin_snapshot", {"full_profile": True}),
                ("text", "https://synthetic.example/profile/neta-9K2B"),
            ],
            "multi_source": [
                ("crm_search", {"query": "contacts"}),
                ("linkedin_snapshot", {"full_profile": True}),
                ("text", "maya.synthetic@example.test https://synthetic.example/profile/neta-9K2B"),
            ],
        }[self.case]
        item = scripts[self.index]
        self.index += 1
        if item[0] == "text":
            return LlmResponse(
                item[1], (), "stop", "", 1, 1, {"role": "assistant", "content": item[1]}
            )
        call = ToolCall(f"call-{self.index}", item[0], item[1], "{}")
        return LlmResponse(
            "", (call,), "tool_calls", "", 1, 1, {"role": "assistant", "tool_calls": []}
        )


class _OmitCrmClient(_ScriptedClient):
    def complete(self, **kwargs):
        response = super().complete(**kwargs)
        if response.text:
            return response._replace(text="https://synthetic.example/profile/neta-9K2B")
        return response


@pytest.mark.parametrize("case", ["crm", "full_linkedin", "multi_source"])
def test_probe_run_uses_actual_graph_and_strict_synthetic_tools(case: str) -> None:
    settings = probe.get_settings().model_copy(update={"memory_write_enabled": False})
    messages = {
        "crm": "Check my CRM contacts",
        "full_linkedin": "Show my full LinkedIn profile",
        "multi_source": "Check CRM and my full LinkedIn profile",
    }
    result = probe._run(_ScriptedClient(case), settings, messages[case])
    assert result["completed"] is True
    assert result["stale_sentinel_absent"] is True


def test_probe_blocks_unexpected_tool(monkeypatch) -> None:
    settings = probe.get_settings().model_copy(update={"memory_write_enabled": False})
    client = _ScriptedClient("crm")
    client.complete = lambda **kwargs: LlmResponse(
        "", (ToolCall("bad", "not_a_fixture_tool", {}, "{}"),), "tool_calls", "", 1, 1, {}
    )
    with pytest.raises(AssertionError, match="unapproved synthetic tool"):
        probe._run(client, settings, "crm")


def test_probe_main_reports_disabled_as_failure(monkeypatch, capsys) -> None:
    settings = probe.get_settings()
    monkeypatch.setattr(probe, "get_settings", lambda: settings)
    monkeypatch.setattr(
        probe, "build_agent_client", lambda settings: _ScriptedClient("crm", enabled=False)
    )
    assert probe.main() == 1
    assert '"configured": false' in capsys.readouterr().out


def test_probe_main_reports_scripted_pass(monkeypatch, capsys) -> None:
    settings = probe.get_settings()
    monkeypatch.setattr(probe, "get_settings", lambda: settings)
    monkeypatch.setattr(
        probe, "build_agent_client", lambda settings: _ScriptedClient("multi_source")
    )
    assert probe.main() == 0
    assert '"pass": true' in capsys.readouterr().out


def test_probe_main_reports_missing_crm_value_after_both_tools(monkeypatch, capsys) -> None:
    settings = probe.get_settings()
    monkeypatch.setattr(probe, "get_settings", lambda: settings)
    monkeypatch.setattr(
        probe, "build_agent_client", lambda settings: _OmitCrmClient("multi_source")
    )
    assert probe.main() == 1
    output = capsys.readouterr().out
    assert '"pass": false' in output
    assert '"crm": false' in output
