"""V2-19 (V2_PG_CONTRACTS.md I8, §20): the kill switch blocks the Mia owner tool loop.

The loop is the only way a model reaches propose (and, through approval cards, decide). Three pins:
  * ``answer_owner`` under the kill switch never builds the agent, so no tool can run, while the
    same call without the switch does reach the agent;
  * every production entry into the loop forwards the live switch: each ``answer_owner(...)`` and
    ``ToolContext(...)`` call in ``app/`` passes ``kill_switch=`` explicitly and never the literal
    ``False`` (``ToolContext`` defaults it to False, so an omitted keyword would disable it). The
    one exception is the sealed predeploy eval, which binds fake ports to every integration;
  * the owner surface passes ``settings.kill_switch`` itself.
The ledger side (propose, decide, commit refused with ``kill_switch``) is pinned in
``tests/services/os_ledger/test_authorize_constructors.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from app.capabilities.types import Principal
from app.core.config import Settings
from app.domain.owner import brain as owner_brain
from app.domain.owner.brain import answer_owner
from app.domain.owner.tasks import OwnerTaskType

REPO = Path(__file__).resolve().parents[2]
# The sealed predeploy eval drives the real model against fake ports only (app/evals/predeploy).
SEALED_EVAL = {("app/evals/predeploy/runner.py", "answer_owner")}
# The calls that open the owner tool loop or build the context its tools read the switch from.
GUARDED_CALLS = ("answer_owner", "ToolContext")


class AgentStarted(Exception):
    pass


def _start(*_args, **_kwargs):
    raise AgentStarted


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(Settings, "brain_ready", lambda self: True)
    monkeypatch.setattr(owner_brain, "build_agent_client", _start)
    monkeypatch.setattr(owner_brain, "run_owner_agent", _start)


def turn(kill_switch: bool):
    return answer_owner(
        principal=Principal.owner(source="telegram", actor_id="123"), store=None, brain=None,
        settings=Settings(_env_file=None, kill_switch=kill_switch,
                          telegram_owner_user_ids="123"),
        task_type=OwnerTaskType.NOTE, owner_text="תציע משימה חדשה ב-Assaf OS", history=(),
        fallback_text="fallback", kill_switch=kill_switch, demo_active=False,
    )


def test_the_kill_switch_stops_the_owner_turn_before_the_agent_or_any_tool(ready):
    result = turn(True)
    assert result.used_agent is False
    assert result.tools_used == ()
    assert result.fallback_reason == "kill_switch_or_disabled"
    assert result.text == "fallback"


def test_without_the_kill_switch_the_same_turn_reaches_the_agent(ready):
    with pytest.raises(AgentStarted):
        turn(False)


def _calls(name: str):
    for path in sorted((REPO / "app").rglob("*.py")):
        relative = path.relative_to(REPO).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if called == name:
                    yield relative, node


@pytest.mark.parametrize("name", GUARDED_CALLS)
def test_every_entry_into_the_owner_loop_forwards_the_live_kill_switch(name):
    sites = list(_calls(name))
    assert sites, name
    for relative, node in sites:
        keywords = {k.arg: k.value for k in node.keywords}
        assert "kill_switch" in keywords, (relative, node.lineno)
        value = keywords["kill_switch"]
        literal_false = isinstance(value, ast.Constant) and value.value is False
        if (relative, name) in SEALED_EVAL:
            continue
        assert not literal_false, (relative, node.lineno)


def test_the_owner_surface_passes_settings_kill_switch_to_the_loop():
    sites = [node for relative, node in _calls("answer_owner")
             if relative == "app/surfaces/owner.py"]
    assert len(sites) == 1
    value = {k.arg: k.value for k in sites[0].keywords}["kill_switch"]
    assert ast.unparse(value) == "settings.kill_switch"
