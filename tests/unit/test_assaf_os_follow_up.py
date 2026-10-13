"""Owner follow-up evidence, isolation, bounds and exact-write completion checks."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

import pytest
from app.capabilities.types import Principal
from app.domain.owner.assaf_os_follow_up import FOLLOW_UP_MAX_CHARS
from app.graph.owner_agent import run_owner_agent
from app.services.assaf_os import RECORD_ENTITIES, AssafOsError
from app.tools.registries.owner_tools import execute_tool, tool_definitions

from tests.unit.test_assaf_os_tools import OWNER, REQUEST, settings
from tests.unit.test_assaf_os_tools import context as base_context
from tests.unit.test_brain_agent import _assistant_text, _assistant_tool_call, _client


def context(store, **changes):
    changes.setdefault("now", datetime(2026, 10, 13, 10, tzinfo=UTC))
    return base_context(store, **changes)


def snapshot():
    return {
        "source": "google_sheets",
        "mode": "TEST",
        "workspace_id": "workspace-test",
        "workbook_id": "workbook_12345678901234567890",
        "observed_at": "2026-10-13T10:00:00Z",
        "coverage": "complete",
        "entity_coverage": dict.fromkeys(RECORD_ENTITIES, "complete"),
        "records": {entity: [] for entity in RECORD_ENTITIES},
        "issues": [],
    }


def task(status="לביצוע", **changes):
    row = {
        "task_id": "T_123",
        "title": "Prepare the agreed output",
        "status": status,
        "revision": 2,
    }
    row.update(changes)
    return row


def lead(number=0, **changes):
    row = {
        "lead_id": f"L_{number:03}",
        "name": "Learning lead",
        "status": "חדש",
        "revision": 1,
        "phone": "PRIVATE_PHONE",
        "source_note": "UNTRUSTED_NOTE",
    }
    row.update(changes)
    return row


def receipt(status="committed", **changes):
    value = {
        "source": "google_sheets",
        "mode": "TEST",
        "workspace_id": "workspace-test",
        "workbook_id": "workbook_12345678901234567890",
        "actor_source": "MIA",
        "request_id": REQUEST,
        "status": status,
        "proposal_id": "PR_123",
        "entity": "tasks",
        "record_id": "T_123",
        "revision": 3,
        "committed_at": "2026-10-13T09:59:00Z",
    }
    value.update(changes)
    return value


def bind_port(monkeypatch, *, view=None, proof=None, error=None):
    calls = []

    class ReadOnlyPort:
        def __init__(self, bound_settings, owner_id, *, actor_source):
            assert owner_id == OWNER and actor_source == "MIA"
            assert bound_settings.assaf_os_workspace_id == "workspace-test"

        def snapshot(self):
            calls.append(("snapshot",))
            if error:
                raise AssafOsError(error)
            return copy.deepcopy(view)

        def receipt(self, request_id):
            calls.append(("receipt", request_id))
            if error:
                raise AssafOsError(error)
            return copy.deepcopy(proof)

        def __getattr__(self, name):
            raise AssertionError(f"unexpected provider effect: {name}")

    monkeypatch.setattr("app.tools.owner.assaf_os.AssafOsPort", ReadOnlyPort)
    return calls


def check_follow_up(monkeypatch, view):
    calls = bind_port(monkeypatch, view=view)
    result = execute_tool("assaf_os_follow_up", {}, context(None))
    assert calls == [("snapshot",)]
    assert result.ok and not result.approval_id
    # Exercise the payload consumed by the model, including its final size slice.
    payload = result.payload()
    return json.loads(payload["result"]), payload


def test_current_business_shape_uses_five_new_leads_without_inventing_finance(monkeypatch):
    view = snapshot()
    view["records"]["leads"] = [lead(n) for n in range(5)]
    view["records"]["works"] = [{"work_id": "W_123", "agreed_amount_ils": 50_000}]
    original = copy.deepcopy(view)
    check, payload = check_follow_up(monkeypatch, view)
    assert len(check["items"]) == 5 and check["state"] == "open"
    assert check["entities"]["tasks"]["follow_up_count"] == 0
    assert check["source"]["observed_at"] == view["observed_at"]
    assert [item["record_id"] for item in check["items"]] == [f"L_{n:03}" for n in range(5)]
    assert all(item["revision"] == 1 for item in check["items"])
    assert all(item["recommendation"] == "review_new_lead_with_owner" for item in check["items"])
    assert check["recommendations_are_actions_taken"] is False
    assert "PRIVATE_PHONE" not in payload["result"] and "UNTRUSTED_NOTE" not in payload["result"]
    assert "50_000" not in payload["result"] and "payments" not in check["entities"]
    assert view == original


@pytest.mark.parametrize(
    "status,state", [("לביצוע", "open"), ("בתהליך", "open"), ("ממתין", "blocked")]
)
def test_open_and_waiting_task_states_are_source_backed(monkeypatch, status, state):
    view = snapshot()
    view["records"]["tasks"] = [task(status)]
    check, _ = check_follow_up(monkeypatch, view)
    item = check["items"][0]
    assert item["record_id"] == "T_123" and item["revision"] == 2
    assert item["recorded_status"] == status and item["state"] == state
    if state == "blocked":
        assert item["recommendation"] == "ask_owner_what_is_needed_to_unblock_task"


@pytest.mark.parametrize("rows", [[], [task("בוצע")], [task("בוטל")]])
def test_no_recorded_follow_up_never_proves_all_business_done(monkeypatch, rows):
    view = snapshot()
    view["records"]["tasks"] = rows
    check, _ = check_follow_up(monkeypatch, view)
    assert check["state"] == "no_recorded_follow_up" and check["items"] == []
    assert check["scope"] == "shared_sheet_new_leads_and_open_tasks"
    assert check["next_action"] == "ask_owner_about_unrecorded_work_if_needed"


@pytest.mark.parametrize("coverage", ["partial", "unknown"])
def test_incomplete_empty_source_is_unknown_not_zero(monkeypatch, coverage):
    view = snapshot()
    view["entity_coverage"]["tasks"] = coverage
    check, payload = check_follow_up(monkeypatch, view)
    assert check["state"] == "unknown" and payload["outcome"] == "partial"
    assert check["entities"]["tasks"]["follow_up_count"] is None
    assert check["entities"]["tasks"]["omitted_count"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"task_id": None},
        {"task_id": "bad id"},
        {"revision": True},
        {"revision": 0},
        {"revision": "2"},
        {"status": None},
        {"status": []},
        {"status": "DONE_FROM_MEMORY"},
        {"title": ""},
        {"title": None},
    ],
)
def test_signed_but_malformed_row_is_not_actionable_evidence(monkeypatch, changes):
    view = snapshot()
    view["records"]["tasks"] = [task(**changes)]
    check, _ = check_follow_up(monkeypatch, view)
    assert check["state"] == "unknown" and check["items"] == []
    assert check["entities"]["tasks"]["follow_up_count"] is None


def test_duplicate_ids_and_relevant_source_issues_fail_unknown(monkeypatch):
    view = snapshot()
    view["records"]["tasks"] = [task(), task("בוצע")]
    check, _ = check_follow_up(monkeypatch, view)
    assert check["state"] == "unknown" and not check["items"]
    view["records"]["tasks"] = [task()]
    view["issues"] = [{"entity": "tasks", "code": "native_conflict"}]
    check, _ = check_follow_up(monkeypatch, view)
    assert check["state"] == "unknown" and not check["items"]


def test_unrelated_partial_finance_does_not_hide_valid_task_evidence(monkeypatch):
    view = snapshot()
    view["coverage"] = "partial"
    view["entity_coverage"]["payments"] = "unknown"
    view["issues"] = [{"entity": "payments", "code": "missing_date"}]
    view["records"]["tasks"] = [task()]
    check, _ = check_follow_up(monkeypatch, view)
    assert check["coverage"] == "partial" and check["items"][0]["state"] == "open"
    assert check["scope"] == "shared_sheet_new_leads_and_open_tasks"


def test_large_unicode_and_escaped_labels_keep_complete_json_and_omission_counts(monkeypatch):
    view = snapshot()
    view["records"]["tasks"] = [task(task_id=f"T_{n:03}", title="\x00" * 2000) for n in range(1000)]
    view["records"]["leads"] = [lead(n, name="א" * 2000) for n in range(1000)]
    check, payload = check_follow_up(monkeypatch, view)
    assert len(check["items"]) == 10
    assert all(group["omitted_count"] == 995 for group in check["entities"].values())
    assert len(payload["result"]) <= FOLLOW_UP_MAX_CHARS
    assert payload["result"].endswith("}")


@pytest.mark.parametrize(
    "tool,args", [("assaf_os_follow_up", {}), ("assaf_os_receipt", {"request_id": REQUEST})]
)
@pytest.mark.parametrize(
    "override",
    [
        {"private_owner_chat": False},
        {"principal": Principal.client(source="website", actor_id=OWNER)},
        {"principal": Principal.owner(source="telegram", actor_id="999999")},
        {"principal": Principal.owner(source="telegram", actor_id="@assaf")},
        {"principal": Principal.owner(source="owner_os", actor_id=OWNER)},
        {"settings": settings(assaf_os_enabled=False)},
    ],
)
def test_private_numeric_owner_gate_precedes_any_provider_read(monkeypatch, tool, args, override):
    def trap(*_args, **_kwargs):
        raise AssertionError("provider accessed by unauthorized principal")

    monkeypatch.setattr("app.tools.owner.assaf_os.AssafOsPort", trap)
    result = execute_tool(tool, args, context(None, **override))
    assert not result.ok and not result.approval_id


@pytest.mark.parametrize(
    "status,state",
    [
        ("committed", "completed"),
        ("unknown", "unknown"),
        ("rejected", "blocked"),
        ("expired", "blocked"),
        ("target_changed", "blocked"),
    ],
)
def test_exact_receipt_state_and_next_action_never_execute_a_mutation(monkeypatch, status, state):
    calls = bind_port(monkeypatch, proof=receipt(status))
    result = execute_tool("assaf_os_receipt", {"request_id": REQUEST}, context(None))
    check = json.loads(result.payload()["result"])
    assert calls == [("receipt", REQUEST)] and not result.approval_id
    assert check["state"] == state and check["scope"] == "exact_historical_write_only"
    assert check["source"]["request_id"] == REQUEST
    assert check["source"]["mode"] == "TEST"
    assert check["source"]["workspace_id"] == "workspace-test"
    assert check["source"]["workbook_id"] == "workbook_12345678901234567890"
    if state == "completed":
        assert check["source"]["record_id"] == "T_123"
        assert check["next_action"] == "read_current_record_if_checking_business_completion"
    elif state == "unknown":
        assert result.payload()["outcome"] == "partial"


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": "different-original-request"},
        {"proposal_id": None},
        {"record_id": ""},
        {"entity": "unknown_entity"},
        {"revision": True},
        {"revision": 0},
        {"committed_at": None},
        {"committed_at": "yesterday"},
        {"committed_at": "2026-10-13T09:59:00"},
        {"status": []},
        {"committed_at": "2026-10-13T09:59:00." + "1" * 5000 + "Z"},
        {"committed_at": "2099-01-01T00:00:00Z"},
        {"mode": None},
        {"workbook_id": None},
        {"workspace_id": ""},
    ],
)
def test_committed_word_alone_is_insufficient_completion_evidence(monkeypatch, changes):
    bind_port(monkeypatch, proof=receipt(**changes))
    result = execute_tool("assaf_os_receipt", {"request_id": REQUEST}, context(None))
    check = json.loads(result.payload()["result"])
    assert check["state"] == "unknown" and result.payload()["outcome"] == "partial"


@pytest.mark.parametrize(
    "tool,args", [("assaf_os_follow_up", {}), ("assaf_os_receipt", {"request_id": REQUEST})]
)
def test_source_failure_returns_explicit_unknown_without_fake_success(monkeypatch, tool, args):
    bind_port(monkeypatch, error="source_unavailable")
    result = execute_tool(tool, args, context(None))
    assert not result.ok and result.error == "source_unavailable"
    check = json.loads(result.payload()["result"])
    assert check["state"] == "unknown"
    if tool == "assaf_os_receipt":
        assert check["source"]["request_id"] == REQUEST
        assert check["source"]["kind"] == "assaf_os_receipt_read_failed"


def test_unexpected_arguments_are_rejected_before_provider_construction(monkeypatch):
    def trap(*_args, **_kwargs):
        raise AssertionError("invalid arguments reached provider")

    monkeypatch.setattr("app.tools.owner.assaf_os.AssafOsPort", trap)
    assert not execute_tool("assaf_os_follow_up", {"owner_id": OWNER}, context(None)).ok
    assert not execute_tool("assaf_os_receipt", {"request_id": "bad"}, context(None)).ok


def test_tool_is_opt_in_and_scripted_owner_loop_receives_grounded_result(monkeypatch):
    assert "assaf_os_follow_up" not in {item["function"]["name"] for item in tool_definitions()}
    client, transport = _client(
        [
            _assistant_tool_call("read1", "assaf_os_follow_up", {}),
            _assistant_text("No tasks recorded; review the new lead."),
        ]
    )
    view = snapshot()
    view["records"]["leads"] = [lead()]
    calls = bind_port(monkeypatch, view=view)
    outcome = run_owner_agent(
        client=client, ctx=context(None), owner_message="What needs attention?"
    )
    assert outcome.completed and outcome.tools_used == ("assaf_os_follow_up",)
    assert not outcome.approval_ids and calls == [("snapshot",)]
    tool_message = next(
        message for message in transport.requests[-1]["messages"] if message.get("role") == "tool"
    )
    check = json.loads(json.loads(tool_message["content"])["result"])
    assert check["items"][0]["record_id"] == "L_000"
    assert check["source"]["observed_at"] == view["observed_at"]
