"""Fault injection and races on the fake (contract §6 test hooks; fake-only by nature).

``inject(point, exc)`` is one-shot; ``commit_unknown`` commits and then raises ``unknown``;
``pause(point)`` blocks a transaction at a point so a race can be staged deterministically.
"""

import threading
from datetime import timedelta

import pytest
from app.services.os_ledger import api

from tests.fakes.os_ledger_builders import (
    commit_direct,
    counts,
    ctx,
    propose_task,
    rid,
    task,
)
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE, build_fake_env

POINTS = ("after_apply", "after_receipt", "before_commit")
TABLES = ("os_proposals", "os_receipts", "os_events", "os_outbox", "os_tasks")


@pytest.fixture
def env():
    result = build_fake_env()
    result.ensure_workspace(WORKSPACE, OWNER_ID, 1)
    result.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    return result


@pytest.fixture
def fake(env):
    return env._impl


def snapshot(env):
    return {t: env.rows(t) for t in TABLES} | {"version": env.version(),
                                                "pending_seq": counts(env)["pending_seq"]}


@pytest.mark.parametrize("point", POINTS)
def test_a_failure_at_each_point_leaves_zero_partial_rows_in_a_direct_commit(env, fake, point):
    before = snapshot(env)
    fake.inject(point, api.LedgerError("ledger_unavailable"))
    with pytest.raises(api.LedgerError, match="^ledger_unavailable$"):
        commit_direct(env, ctx(), [task()])
    assert snapshot(env) == before
    commit_direct(env, ctx(), [task()])  # one-shot: the retry goes through
    assert env.version() == 1 and len(env.rows("os_tasks")) == 1


@pytest.mark.parametrize("point", POINTS)
def test_a_failure_at_each_point_leaves_a_decide_undecided_and_retryable(env, fake, point):
    proposal = propose_task(env)
    before = snapshot(env)
    fake.inject(point, api.LedgerError("ledger_unavailable"))
    with pytest.raises(api.LedgerError, match="^ledger_unavailable$"):
        env.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    assert snapshot(env) == before
    assert [r["status"] for r in env.rows("os_proposals")] == ["pending"]
    assert not env.receipt(ctx(), proposal.approval_id).found
    retried = env.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    assert retried.receipt.status == "committed" and env.version() == 1


def test_a_failure_during_propose_writes_nothing(env, fake):
    fake.inject("before_commit", api.LedgerError("ledger_unavailable"))
    before = snapshot(env)
    with pytest.raises(api.LedgerError, match="^ledger_unavailable$"):
        propose_task(env)
    assert snapshot(env) == before


def test_any_other_exception_becomes_ledger_unavailable_and_rolls_back(env, fake):
    before = snapshot(env)
    fake.inject("after_receipt", RuntimeError("synthetic"))
    with pytest.raises(api.LedgerError) as caught:
        commit_direct(env, ctx(), [task()])
    assert caught.value.code == "ledger_unavailable" and caught.value.write_outcome == "not_started"
    assert snapshot(env) == before


def test_an_assertion_passes_through_unchanged_after_the_rollback(env, fake):
    before = snapshot(env)
    fake.inject("after_apply", AssertionError("caller bug"))
    with pytest.raises(AssertionError, match="^caller bug$"):
        commit_direct(env, ctx(), [task()])
    assert snapshot(env) == before


def test_commit_unknown_commits_and_then_raises_unknown(env, fake):
    fake.inject("commit_unknown", RuntimeError("ignored: the outcome is unknown"))
    request_id = rid()
    with pytest.raises(api.LedgerError) as caught:
        commit_direct(env, ctx(), [task()], request_id)
    assert caught.value.code == "unknown" and caught.value.write_outcome == "unknown"
    assert env.version() == 1 and len(env.rows("os_tasks")) == 1
    found = env.receipt(ctx(), request_id)
    assert found.found and found.receipt.status == "committed"
    assert env.creation_request(ctx(), request_id).found
    commit_direct(env, ctx(), [task("TAS_t2")])  # one-shot
    assert env.version() == 2


def test_commit_unknown_on_a_decide_is_recoverable_by_reading_the_receipt(env, fake):
    proposal = propose_task(env)
    fake.inject("commit_unknown", RuntimeError("x"))
    with pytest.raises(api.LedgerError, match="^unknown$"):
        env.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    recovered = env.receipt(ctx(), proposal.approval_id)
    assert recovered.found and recovered.receipt.status == "committed"
    # Recovery is a read: replaying the same decision returns the same receipt, nothing new.
    again = env.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    assert again.receipt == recovered.receipt and env.version() == 1


def test_an_unknown_injection_point_is_refused(fake):
    with pytest.raises(ValueError, match="invalid_injection_point"):
        fake.inject("nowhere", RuntimeError())
    with pytest.raises(ValueError, match="invalid_injection_point"):
        fake.pause("nowhere")


def test_an_injection_is_consumed_by_the_first_transaction_that_reaches_it(env, fake):
    fake.inject("after_apply", api.LedgerError("ledger_unavailable"))
    env.head(ctx())  # a read never reaches after_apply
    with pytest.raises(api.LedgerError):
        commit_direct(env, ctx(), [task()])
    commit_direct(env, ctx(), [task()])
    assert len(env.rows("os_tasks")) == 1


# --- pause --------------------------------------------------------------------------------


def start(target):
    box = {}

    def run():
        try:
            box["value"] = target()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


def test_a_paused_transaction_holds_the_workspace_lock_until_released(env, fake):
    release = fake.pause("before_commit")
    first, first_box = start(lambda: commit_direct(env, ctx(), [task("TAS_a")]))
    assert release.reached.wait(10)
    second, second_box = start(lambda: commit_direct(env, ctx(), [task("TAS_b")]))
    second.join(0.3)
    assert second.is_alive()  # blocked behind the first transaction's lock
    assert "value" not in second_box
    release.set()
    first.join(10)
    second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert "error" not in first_box and "error" not in second_box
    assert env.version() == 2
    assert sorted(r["record_id"] for r in env.rows("os_tasks")) == ["TAS_a", "TAS_b"]


def test_a_pause_staged_approve_race_has_one_winner_and_a_deterministic_loser(env, fake):
    proposal = propose_task(env)
    release = fake.pause("after_receipt")
    approve = lambda: env.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")  # noqa: E731
    reject = lambda: env.decide(ctx(), proposal.proposal_id, proposal.rejection_id, "reject")  # noqa: E731
    winner, winner_box = start(approve)
    assert release.reached.wait(10)
    loser, loser_box = start(reject)
    loser.join(0.3)
    assert loser.is_alive()
    release.set()
    winner.join(10)
    loser.join(10)
    assert winner_box["value"].receipt.status == "committed"
    assert isinstance(loser_box["error"], api.LedgerError)
    assert loser_box["error"].code == "already_decided"
    assert (len(env.rows("os_receipts")), len(env.rows("os_events")),
            len(env.rows("os_outbox"))) == (1, 1, 1)


def test_a_paused_transaction_that_fails_after_release_still_rolls_back(env, fake):
    release = fake.pause("after_receipt")
    fake.inject("before_commit", api.LedgerError("ledger_unavailable"))
    thread, box = start(lambda: commit_direct(env, ctx(), [task()]))
    assert release.reached.wait(10)
    release.set()
    thread.join(10)
    assert box["error"].code == "ledger_unavailable"
    assert env.version() == 0 and env.rows("os_tasks") == []


def test_the_clock_hooks_move_every_deadline(env):
    proposal = propose_task(env)
    env.advance(599)
    assert env.pending(ctx(), after=None, limit=10).items == (proposal,)
    env.advance(2)
    assert env.pending(ctx(), after=None, limit=10).items == ()
    assert env.now() - proposal.created_at == timedelta(seconds=601)
