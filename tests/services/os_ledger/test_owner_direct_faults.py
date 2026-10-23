"""V2-24 fault injection and a deterministic race, on the FakeLedger only (§6 test hooks).

``inject``/``pause`` are fake-only by design (``LedgerEnv`` exposes neither), so these cases use the
fake directly. The PG variant of the race is ``test_owner_direct_undo.py``'s threaded case.
"""

from __future__ import annotations

import threading

import pytest
from app.services.os_ledger import api, owner_direct

from tests.fakes.os_ledger_builders import commit_direct, current, rid, task
from tests.services.os_ledger.owner_direct_support import (
    TASK_DONE,
    TASK_OPEN,
    KnobSettings,
    ledger_case,
    owner_ctx,
    single,
    state,
)


@pytest.fixture
def env():
    return ledger_case("fake")


def fake_of(env):
    return env._impl


def commit(env, body):
    return owner_direct.commit(env, owner_ctx(), body, settings=KnobSettings(env))


def undo(env, request_id, undo_request_id=None):
    return owner_direct.undo(env, owner_ctx(), request_id,
                             {"undo_request_id": undo_request_id or rid()},
                             settings=KnobSettings(env))


def abandon(env, request_id):
    return owner_direct.abandon(env, owner_ctx(), request_id, {}, settings=KnobSettings(env))


class Boom(Exception):
    pass


@pytest.mark.parametrize("point", ["after_apply", "after_receipt", "before_commit"])
def test_a_failure_after_apply_leaves_no_partial_rows_events_or_outbox(env, point):
    commit_direct(env, owner_ctx(), [task("TAS_t1")])
    snapshot = state(env)
    fake_of(env).inject(point, Boom())
    with pytest.raises(api.LedgerError) as caught:
        commit(env, single("complete", record_id="TAS_t1", revision=1))
    assert caught.value.code == "ledger_unavailable"
    assert state(env) == snapshot
    assert current(env, "tasks", "TAS_t1")["status"] == TASK_OPEN


@pytest.mark.parametrize("point", ["after_apply", "after_receipt", "before_commit"])
def test_a_failure_inside_an_undo_leaves_nothing(env, point):
    commit_direct(env, owner_ctx(), [task("TAS_t1")])
    original = commit(env, single("complete", record_id="TAS_t1", revision=1))
    snapshot = state(env)
    fake_of(env).inject(point, Boom())
    with pytest.raises(api.LedgerError):
        undo(env, original["request_id"])
    assert state(env) == snapshot
    assert current(env, "tasks", "TAS_t1")["status"] == TASK_DONE


@pytest.mark.parametrize("point", ["after_receipt", "before_commit"])
def test_a_failure_inside_an_abandon_leaves_nothing(env, point):
    snapshot = state(env)
    fake_of(env).inject(point, Boom())
    with pytest.raises(api.LedgerError):
        abandon(env, rid())
    assert state(env) == snapshot


def test_a_ledger_error_after_apply_propagates_unchanged_and_writes_nothing(env):
    commit_direct(env, owner_ctx(), [task("TAS_t1")])
    snapshot = state(env)
    fake_of(env).inject("after_apply", api.LedgerError("ledger_integrity_failed"))
    with pytest.raises(api.LedgerError) as caught:
        commit(env, single("complete", record_id="TAS_t1", revision=1))
    assert caught.value.code == "ledger_integrity_failed"
    assert state(env) == snapshot


def test_an_unknown_commit_outcome_is_recovered_by_a_read(env):
    commit_direct(env, owner_ctx(), [task("TAS_t1")])
    request_id = rid()
    fake_of(env).inject("commit_unknown", Boom())
    with pytest.raises(api.LedgerError) as caught:
        commit(env, single("complete", record_id="TAS_t1", revision=1, request_id=request_id))
    assert (caught.value.code, caught.value.write_outcome) == ("unknown", "unknown")
    found = env.receipt(owner_ctx(), request_id)
    assert found.found and found.receipt.status == "committed"
    replay = commit(env, single("complete", record_id="TAS_t1", revision=1,
                                request_id=request_id))
    assert replay["status"] == "committed"


def test_a_commit_and_an_undo_serialize_through_the_workspace_lock(env):
    commit_direct(env, owner_ctx(), [task("TAS_t1")])
    original = commit(env, single("complete", record_id="TAS_t1", revision=1))
    gate = fake_of(env).pause("after_apply")
    results = {}

    def edit():
        results["edit"] = commit(env, single("update", record_id="TAS_t1", revision=2,
                                             patch={"title": "First"}))["status"]

    def late_undo():
        try:
            results["undo"] = undo(env, original["request_id"])["status"]
        except api.LedgerError as exc:
            results["undo"] = exc.code

    first = threading.Thread(target=edit)
    first.start()
    assert gate.reached.wait(10)
    second = threading.Thread(target=late_undo)
    second.start()
    second.join(0.3)
    assert second.is_alive(), "the undo must wait for the edit's workspace lock"
    gate.set()
    first.join(10)
    second.join(10)
    assert results == {"edit": "committed", "undo": "undo_conflict"}
    final = current(env, "tasks", "TAS_t1")
    assert (final["title"], final["status"], final["revision"]) == ("First", TASK_DONE, 3)
