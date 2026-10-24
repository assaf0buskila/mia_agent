"""WP4 restore commands, using the same fake/optional disposable-PG world as cutover."""

from datetime import timedelta

import pytest
from app.services.os_ledger import api
from app.workers import os_authority as cli

from tests.fakes.os_ledger_builders import commit_direct, ctx, task
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE
from tests.workers.test_os_authority import (
    CASES,
    NOW,
    OTHER_SHA,
    SHA,
    World,
    codes,
    stamp,
)


@pytest.fixture(params=CASES)
def recovery(request, tmp_path):
    world = World(tmp_path, request.param)
    world.deps.restore_complete = lambda: not (tmp_path / "restore-incomplete").exists()
    assert world.run("init").code == 0
    assert world.run("fence").code == 0
    world.put("restore.json", {
        "v": 1, "workspace": WORKSPACE, "epoch": 1, "release_sha": SHA,
        "dump_created_at": stamp(NOW - timedelta(minutes=15)),
    })
    return world


def step(world, command, epoch=1, **kwargs):
    return world.run(command, "--expected-epoch", str(epoch), **kwargs)


def test_legal_restore_and_all_retries_bump_exactly_once(recovery):
    w = recovery
    assert step(w, "restore-begin").out["outcome"] == "restoring"
    assert w.state().states == {"core": "restoring", "statements": "shadow"}
    assert step(w, "restore-begin").out["outcome"] == "already_restoring"
    done = step(w, "restore-finish")
    assert done.code == 0, done.err
    assert done.out["rpo_within_limit"] is True
    assert done.out["rto_within_limit"] is True
    assert w.state().epoch == 2 and w.state().states["core"] == "active"
    for command in ("restore-begin", "restore-finish", "restore-finish"):
        again = step(w, command)
        assert again.code == 0 and again.out["outcome"] == "already_restored"
        assert again.out["epoch"] == 2
    assert step(w, "restore-finish", epoch=2).code == 1
    assert w.state().epoch == 2
    # After the explicit settings reload, normal commits feed the unchanged outbox at E+1.
    w.env.knobs.settings_epoch = 2
    commit_direct(w.env, ctx(epoch=2), [task("TAS_recovered")])
    assert w.env.rows("os_outbox")[-1]["body"]["authority_epoch"] == 2
    with pytest.raises(api.LedgerError):
        commit_direct(w.env, ctx(epoch=1), [task("TAS_stale")])


@pytest.mark.parametrize("command", ["restore-begin", "restore-finish"])
@pytest.mark.parametrize("state", ["shadow", "active"])
def test_wrong_states_and_no_direct_fenced_to_active(tmp_path, command, state):
    w = World(tmp_path)
    assert w.run("init").code == 0
    if state == "active":
        w.env.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    assert codes(step(w, command)) == ["authority_state_invalid"]


def test_fenced_cannot_finish_or_activate(recovery):
    assert codes(step(recovery, "restore-finish")) == ["authority_state_invalid"]
    assert codes(recovery.run("activate")) == ["authority_state_invalid"]
    with pytest.raises(api.LedgerError):
        recovery.env.transition(
            WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)


@pytest.mark.parametrize("command", ["restore-begin", "restore-finish"])
def test_sha_and_epoch_refusals_leave_state_untouched(recovery, command):
    w = recovery
    before = w.state()
    assert codes(step(w, command, approved=OTHER_SHA)) == ["release_sha_mismatch"]
    assert codes(step(w, command, epoch=9)) == ["restore_epoch_mismatch"]
    assert w.state() == before


@pytest.mark.parametrize("probe,reason", [
    ("main", "main_role_unsafe"), ("ledger", "ledger_role_unsafe"),
])
def test_posture_failures_refuse_finish(recovery, probe, reason):
    w = recovery
    assert step(w, "restore-begin").code == 0
    setattr(w.probes, probe, False)
    assert reason in codes(step(w, "restore-finish"))
    assert w.state().epoch == 1 and w.state().states["core"] == "restoring"


def test_incomplete_restore_marker_blocks_finish(recovery):
    w = recovery
    assert step(w, "restore-begin").code == 0
    (w.dir.parent / "restore-incomplete").touch()
    assert "restore_incomplete" in codes(step(w, "restore-finish"))
    assert w.state().epoch == 1


@pytest.mark.parametrize("minutes,reason", [
    (15 + 1 / 60, "restore_rpo_exceeded"), (-1, "restore_evidence_invalid"),
])
def test_dump_timestamp_limits_fail_closed(recovery, minutes, reason):
    w = recovery
    assert step(w, "restore-begin").code == 0
    document = w.get("restore.json")
    document["dump_created_at"] = stamp(NOW - timedelta(minutes=minutes))
    w.put("restore.json", document)
    run = step(w, "restore-finish")
    assert reason in codes(run)
    assert w.state().epoch == 1


@pytest.mark.parametrize("key", ["v", "workspace", "epoch", "release_sha", "dump_created_at"])
def test_missing_restore_evidence_refuses(recovery, key):
    w = recovery
    assert step(w, "restore-begin").code == 0
    doc = w.get("restore.json")
    del doc[key]
    w.put("restore.json", doc)
    assert "restore_evidence_invalid" in codes(step(w, "restore-finish"))


def test_rto_breach_is_reported_but_does_not_prevent_recovery(recovery):
    w = recovery
    assert step(w, "restore-begin").code == 0
    w.clock[0] = NOW + timedelta(hours=2, seconds=1)
    result = step(w, "restore-finish")
    assert result.code == 0 and result.out["rto_within_limit"] is False
    assert result.out["warnings"] == ["restore_rto_exceeded"]


def test_second_fence_after_dump_restore_preserves_incident_time(recovery):
    w = recovery
    original = w.get("ledger-fence.json")
    # A restored dump can contain active authority. No real restore or VM involved.
    w.env.transition(WORKSPACE, "core", "restoring", expected_epoch=1, actor_owner_id=OWNER_ID)
    # Simulate the dump's authority rows by rebuilding the disposable world, not a forbidden edge.
    other = World(w.dir.parent / "restored", w.case)
    assert other.run("init").code == 0
    other.env.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    other.put("ledger-fence.json", original)
    other.clock[0] = NOW + timedelta(minutes=20)
    assert other.run("fence").code == 0
    assert other.get("ledger-fence.json") == original


@pytest.mark.parametrize("statements", ["active", "restoring"])
def test_unsafe_statements_domain_refuses_begin(recovery, statements):
    w = recovery
    path = ["active"] if statements == "active" else ["fenced", "restoring"]
    for state in path:
        w.env.transition(WORKSPACE, "statements", state,
                         expected_epoch=1, actor_owner_id=OWNER_ID)
    assert codes(step(w, "restore-begin")) == ["authority_state_invalid"]
    assert w.state().states["core"] == "fenced"


def test_active_statements_are_fenced_then_remain_fenced_after_core_recovery(recovery):
    w = recovery
    w.env.transition(WORKSPACE, "statements", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    assert w.run("fence").code == 0
    assert step(w, "restore-begin").code == 0
    assert step(w, "restore-finish").code == 0
    assert w.state().states == {"core": "active", "statements": "fenced"}


def test_missing_epoch_is_a_fixed_refusal(recovery):
    assert codes(recovery.run("restore-begin")) == ["restore_epoch_required"]


def test_fence_evidence_failure_prevents_initial_transition(tmp_path, monkeypatch):
    w = World(tmp_path)
    assert w.run("init").code == 0

    def broken(*args):
        raise cli.Refusal("evidence_unwritable")

    with monkeypatch.context() as patch:
        patch.setattr(w.deps.evidence, "write", broken)
        assert codes(w.run("fence")) == ["evidence_unwritable"]
    assert w.state().states["core"] == "shadow"


def test_crash_after_fence_preserves_start_and_uses_conservative_rpo(tmp_path, monkeypatch):
    w = World(tmp_path)
    assert w.run("init").code == 0
    write = w.deps.evidence.write

    def fail_completion(name, document):
        if name == cli.LEDGER_FENCE and document.get("fenced_at") is not None:
            raise cli.Refusal("evidence_unwritable")
        write(name, document)

    with monkeypatch.context() as patch:
        patch.setattr(w.deps.evidence, "write", fail_completion)
        assert codes(w.run("fence")) == ["evidence_unwritable"]
    assert w.state().states["core"] == "fenced"
    assert w.get(cli.LEDGER_FENCE)["fence_started_at"] == stamp(NOW)
    w.clock[0] = NOW + timedelta(hours=3)
    assert w.run("fence").code == 0
    record = w.get(cli.LEDGER_FENCE)
    assert record["fence_started_at"] == stamp(NOW)
    assert record["fenced_at"] == stamp(w.clock[0])


def test_missing_incident_time_is_never_invented_for_already_fenced(recovery):
    recovery.drop(cli.LEDGER_FENCE)
    assert recovery.run("fence").code == 0  # retain the existing fence no-op after rollback
    assert not (recovery.dir / cli.LEDGER_FENCE).exists()
    assert codes(step(recovery, "restore-begin")) == ["restore_evidence_invalid"]


def test_unreadable_evidence_and_provider_errors_never_leak(recovery, caplog):
    w = recovery
    assert step(w, "restore-begin").code == 0
    w.probes.broken.add("main")
    result = step(w, "restore-finish")
    assert codes(result) == ["internal_error"]
    assert "SECRET" not in result.err + str(result.out) + caplog.text
    w.probes.broken.clear()
    (w.dir / cli.RESTORE).write_text("invalid secret data", encoding="utf-8")
    assert codes(step(w, "restore-finish")) == ["restore_evidence_invalid"]


def test_failed_session_write_leaves_core_fenced(recovery, monkeypatch):
    def broken(*args):
        raise cli.Refusal("evidence_unwritable")

    monkeypatch.setattr(recovery.deps.evidence, "write", broken)
    assert codes(step(recovery, "restore-begin")) == ["evidence_unwritable"]
    assert recovery.state().states["core"] == "fenced"


def test_activation_evidence_failure_cannot_cause_a_second_bump(recovery, monkeypatch):
    w = recovery
    assert step(w, "restore-begin").code == 0
    write = w.deps.evidence.write

    def broken(name, document):
        if name == cli.ACTIVATION:
            raise cli.Refusal("evidence_unwritable")
        write(name, document)

    monkeypatch.setattr(w.deps.evidence, "write", broken)
    done = step(w, "restore-finish")
    assert done.code == 0 and done.out["evidence_written"] is False
    assert step(w, "restore-finish").out["epoch"] == 2
