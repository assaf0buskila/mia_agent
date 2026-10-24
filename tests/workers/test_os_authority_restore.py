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


def test_fence_evidence_failure_still_contains_and_blocks_restore(tmp_path, monkeypatch):
    w = World(tmp_path)
    assert w.run("init").code == 0

    def broken(*args):
        raise cli.Refusal("evidence_unwritable")

    with monkeypatch.context() as patch:
        patch.setattr(w.deps.evidence, "write", broken)
        result = w.run("fence")
        assert result.code == 0 and result.out["evidence_written"] is False
        assert result.out["warnings"] == ["evidence_unwritable"]
    assert w.state().states["core"] == "fenced"
    assert codes(step(w, "restore-begin")) == ["restore_evidence_invalid"]


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
        result = w.run("fence")
        assert result.code == 0 and result.out["evidence_written"] is False
        assert result.out["warnings"] == ["evidence_unwritable"]
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
    assert codes(result) == ["main_role_unsafe"]
    assert {"code": "main_role_unsafe", "status": "fail", "detail": "probe_error"} in (
        result.out["checks"])
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


def test_rollback_race_records_incident_for_restore(tmp_path, monkeypatch):
    w = World(tmp_path)
    assert w.run("init").code == 0
    w.env.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    calls = iter([0, 1])  # a business receipt lands just before the fence
    monkeypatch.setattr(w.probes, "non_import_receipts", lambda *args: next(calls))
    assert codes(w.run("rollback")) == ["rollback_after_first_write", "core_left_fenced"]
    record = w.get(cli.LEDGER_FENCE)
    assert record["fence_started_at"] == record["fenced_at"] == stamp(NOW)
    assert w.run("fence").code == 0
    assert step(w, "restore-begin").code == 0


def test_post_restore_first_fence_stops_and_metrics_are_not_applicable(tmp_path):
    w = World(tmp_path)
    w.deps.restore_complete = lambda: True
    assert w.run("init").code == 0
    w.env.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    w.put("restore-attempt.json", {"v": 1, "pre_restore_fence_record_present": False})
    w.clock[0] = NOW + timedelta(minutes=90)
    result = w.run("fence")
    assert result.code == 0 and result.out["fence_record_minted_after_restore"] is True
    record = w.get(cli.LEDGER_FENCE)
    assert record["fence_record_minted_after_restore"] is True
    assert codes(step(w, "restore-begin")) == ["restore_evidence_invalid"]
    assert w.state().states["core"] == "fenced"
    # Even if an owner-run procedure has moved core to restoring, the CLI does not bypass it.
    w.put(cli.RESTORE_SESSION, record)
    w.put(cli.RESTORE, {"v": 1, "workspace": WORKSPACE, "epoch": 1, "release_sha": SHA,
                        "dump_created_at": stamp(NOW - timedelta(minutes=5))})
    w.env.transition(WORKSPACE, "core", "restoring", expected_epoch=1, actor_owner_id=OWNER_ID)
    finish = step(w, "restore-finish")
    assert codes(finish) == ["restore_evidence_invalid"]
    assert finish.out["fence_record_minted_after_restore"] is True
    assert finish.out["rto_within_limit"] is None and finish.out["rpo_within_limit"] is None
    assert w.state().epoch == 1


def test_second_fence_preserves_higher_epoch_and_begin_reports_mismatch(recovery):
    w = recovery
    original = {**w.get(cli.LEDGER_FENCE), "epoch": 2}
    w.put(cli.LEDGER_FENCE, original)
    # Rebuild the restored dump at E0=1, active, with evidence from incident E=2.
    other = World(w.dir.parent / "older-dump")
    assert other.run("init").code == 0
    other.env.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    other.put(cli.LEDGER_FENCE, original)
    other.clock[0] = NOW + timedelta(minutes=30)
    assert other.run("fence").code == 0
    assert other.state().states["core"] == "fenced"
    assert other.get(cli.LEDGER_FENCE) == original
    assert codes(step(other, "restore-begin")) == ["restore_epoch_mismatch"]


def test_fix_forward_deploy_between_fence_begin_and_finish(recovery):
    w = recovery
    w.deps.build_sha = OTHER_SHA
    options = {"approved": OTHER_SHA, "build": OTHER_SHA}
    assert step(w, "restore-begin", **options).code == 0
    # Dump attestation remains bound to the currently approved release.
    assert codes(step(w, "restore-finish", **options)) == ["restore_evidence_invalid"]
    w.put(cli.RESTORE, {**w.get(cli.RESTORE), "release_sha": OTHER_SHA})
    assert step(w, "restore-finish", **options).code == 0
    assert w.state().epoch == 2


@pytest.mark.parametrize("failure", ["read", "write", "oversize", "malformed", "symlink"])
def test_fence_bookkeeping_failure_never_blocks_containment(tmp_path, monkeypatch, failure):
    w = World(tmp_path)
    assert w.run("init").code == 0
    w.env.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    with monkeypatch.context() as patch:
        if failure == "read":
            def unreadable(*args):
                raise cli.EvidenceError("SECRET")
            patch.setattr(w.deps.evidence, "read", unreadable)
        elif failure == "write":
            def unwritable(*args, **kwargs):
                raise PermissionError("SECRET")
            patch.setattr(cli.tempfile, "mkstemp", unwritable)
        elif failure == "symlink":
            is_symlink = type(w.dir).is_symlink
            patch.setattr(type(w.dir), "is_symlink", lambda path: (
                path == w.dir / cli.LEDGER_FENCE or is_symlink(path)))
        else:
            w.dir.mkdir(parents=True, exist_ok=True)
            (w.dir / cli.LEDGER_FENCE).write_text(
                "x" * (cli.MAX_EVIDENCE_BYTES + 1) if failure == "oversize" else "[]")
        result = w.run("fence")
    assert result.code == 0 and w.state().states["core"] == "fenced"
    assert result.out["evidence_written"] is False
    assert result.out["warnings"] == [
        "evidence_unwritable" if failure == "write" else "restore_evidence_invalid"]
    assert codes(step(w, "restore-begin")) == ["restore_evidence_invalid"]


@pytest.mark.parametrize("old_activation", [None, 1])
def test_retry_repairs_activation_and_retains_rto_warning(recovery, old_activation, caplog):
    w = recovery
    assert step(w, "restore-begin").code == 0
    if old_activation:
        w.put(cli.ACTIVATION, {"v": 1, "epoch": old_activation, "release_sha": SHA})
    w.clock[0] = NOW + timedelta(hours=3)
    w.env.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    with caplog.at_level("INFO", logger="mia.os_authority"):
        result = step(w, "restore-finish")
    assert result.code == 0 and result.out["outcome"] == "already_restored"
    assert w.get(cli.ACTIVATION)["epoch"] == 2
    assert result.out["warnings"] == ["restore_rto_exceeded"]
    assert w.state().epoch == 2


@pytest.mark.parametrize("probe,reason", [("main", "main_role_unsafe"),
                                          ("ledger", "ledger_role_unsafe")])
def test_posture_probe_exceptions_use_preflight_codes(recovery, probe, reason, caplog):
    w = recovery
    assert step(w, "restore-begin").code == 0
    w.probes.broken.add(probe)
    result = step(w, "restore-finish")
    assert codes(result) == [reason]
    assert {"code": reason, "status": "fail", "detail": "probe_error"} in result.out["checks"]
    assert "SECRET" not in result.err + str(result.out) + caplog.text
    assert w.state().epoch == 1 and w.state().states["core"] == "restoring"


def test_rto_warning_reaches_reason_log(recovery, caplog):
    assert step(recovery, "restore-begin").code == 0
    recovery.clock[0] = NOW + timedelta(hours=3)
    with caplog.at_level("INFO"):
        assert step(recovery, "restore-finish").code == 0
    assert "op=restore-finish outcome=ok reason=restore_rto_exceeded" in caplog.text


def test_post_restore_stale_lower_epoch_record_also_takes_exception_path(recovery):
    w = recovery
    w.env.transition(WORKSPACE, "core", "shadow", expected_epoch=1, actor_owner_id=OWNER_ID)
    w.env.transition(WORKSPACE, "core", "active", expected_epoch=2, actor_owner_id=OWNER_ID)
    w.put(cli.RESTORE_ATTEMPT, {"v": 1, "pre_restore_fence_record_present": True})
    w.clock[0] = NOW + timedelta(minutes=90)
    result = w.run("fence")
    assert result.code == 0 and result.out["fence_record_minted_after_restore"] is True
    assert codes(step(w, "restore-begin", epoch=2)) == ["restore_evidence_invalid"]


def test_completed_restore_consumes_attempt_before_the_next_incident(recovery):
    w = recovery
    w.put(cli.RESTORE_ATTEMPT, {"v": 1, "pre_restore_fence_record_present": True})
    assert step(w, "restore-begin").code == 0
    assert step(w, "restore-finish").code == 0
    assert not (w.dir / cli.RESTORE_ATTEMPT).exists()
    assert list(w.dir.glob("restore-attempt-superseded-*.json"))
    w.clock[0] = NOW + timedelta(days=1)
    result = w.run("fence")
    assert result.code == 0 and result.out["fence_record_minted_after_restore"] is False
    assert step(w, "restore-begin", epoch=2).code == 0


def test_retry_repairs_attempt_archive_without_bumping_again(recovery, monkeypatch):
    w = recovery
    w.put(cli.RESTORE_ATTEMPT, {"v": 1, "pre_restore_fence_record_present": True})
    assert step(w, "restore-begin").code == 0
    with monkeypatch.context() as patch:
        def unwritable(*args):
            raise cli.Refusal("evidence_unwritable")
        patch.setattr(w.deps.evidence, "set_aside", unwritable)
        result = step(w, "restore-finish")
    assert result.code == 0 and result.out["evidence_written"] is False
    assert result.out["warnings"] == ["evidence_unwritable"]
    assert w.state().epoch == 2 and (w.dir / cli.RESTORE_ATTEMPT).exists()
    again = step(w, "restore-finish")
    assert again.code == 0 and again.out["outcome"] == "already_restored"
    assert w.state().epoch == 2 and not (w.dir / cli.RESTORE_ATTEMPT).exists()
