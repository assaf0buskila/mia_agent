"""Exactly-once on PostgreSQL (V2-16, I1, §5.3): races, rollback injection and COMMIT outcomes.

Every write is one transaction under the ``os_workspaces`` row lock: a race has one winner, an
injected failure at any point leaves zero partial rows, a swallowed database error never commits,
and a failure during COMMIT is ``unknown`` (recovery is a read, never a re-POST).
"""

import os
import threading
import time

import pytest
from app.services.os_ledger import api, core
from app.services.os_ledger.tx import PgLedgerTx
from sqlalchemy import text

from tests.fakes.os_ledger_builders import (
    client,
    commit_direct,
    counts,
    ctx,
    direct_draft,
    payment,
    propose_task,
    rid,
    task,
    window_s,
    work,
)
from tests.fakes.os_ledger_env import WORKSPACE
from tests.services.os_ledger.conftest import PG_URL_ENV, _prepare, build_pg_env, pg_enabled


@pytest.fixture
def pg(request):
    if not pg_enabled():
        pytest.skip("disposable MIA_OS_LEDGER_TEST_POSTGRES_URL required")
    return _prepare(build_pg_env(os.environ[PG_URL_ENV]))


def everything(env):
    return counts(env) | {
        name: len(env.rows(name)) for name in ("os_summary_monthly", "os_import_batches")
    } | {"workspace": {k: v for k, v in env.rows("os_workspaces")[0].items()
                       if k in {"version", "pending_seq", "summaries"}}}


def race(*calls):
    barrier = threading.Barrier(len(calls))
    outcomes = [None] * len(calls)

    def run(index, call):
        barrier.wait(10)
        try:
            outcomes[index] = ("ok", call())
        except api.LedgerError as exc:
            outcomes[index] = ("error", exc.code)

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
        assert not thread.is_alive()
    return outcomes


def test_six_channels_approving_at_once_commit_exactly_once(pg):
    proposal = propose_task(pg)
    contexts = [ctx("OS"), ctx("TELEGRAM_APPROVAL")] * 3
    outcomes = race(*[
        (lambda c=c: pg.decide(c, proposal.proposal_id, proposal.approval_id, "approve"))
        for c in contexts])
    receipts = [value.receipt for kind, value in outcomes if kind == "ok"]
    assert [kind for kind, _ in outcomes] == ["ok"] * 6
    assert all(receipt == receipts[0] for receipt in receipts)
    assert (len(pg.rows("os_receipts")), len(pg.rows("os_events")),
            len(pg.rows("os_outbox")), len(pg.rows("os_tasks")), pg.version()) == (1, 1, 1, 1, 1)


def test_approve_and_reject_racing_have_one_terminal_result(pg):
    proposal = propose_task(pg)
    outcomes = race(
        lambda: pg.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve"),
        lambda: pg.decide(ctx("TELEGRAM_APPROVAL"), proposal.proposal_id,
                          proposal.rejection_id, "reject"),
        lambda: pg.decide(ctx(), proposal.proposal_id, proposal.rejection_id, "reject"))
    statuses = {value.receipt.status for kind, value in outcomes if kind == "ok"}
    assert len(statuses) == 1 and len(pg.rows("os_receipts")) == 1 and pg.version() == 1
    errors = [value for kind, value in outcomes if kind == "error"]
    assert set(errors) <= {"already_decided"}


def test_the_same_direct_request_from_two_threads_commits_once(pg):
    request_id = rid()

    def commit():
        return commit_direct(pg, ctx(), [task()], request_id)[1].status

    outcomes = race(commit, commit)
    assert sorted(outcomes, key=str) == sorted(
        [("ok", "committed"), ("error", "idempotency_mismatch")], key=str)
    assert len(pg.rows("os_proposals")) == 1 and len(pg.rows("os_receipts")) == 1


def test_one_propose_replay_race_creates_one_proposal(pg):
    request = api.ProposeInput(rid(), "tasks", None, 0, {"title": "Once", "status": "לביצוע"})
    outcomes = race(*[(lambda: pg.propose(ctx("MIA"), request)) for _ in range(4)])
    assert {value.proposal_id for _, value in outcomes} and all(k == "ok" for k, _ in outcomes)
    assert len({value.proposal_id for _, value in outcomes}) == 1
    assert len(pg.rows("os_proposals")) == 1


@pytest.mark.parametrize("statement", [
    "INSERT INTO assaf_os.os_receipts", "INSERT INTO assaf_os.os_events",
    "INSERT INTO assaf_os.os_outbox", "UPDATE assaf_os.os_proposals",
    "INSERT INTO assaf_os.os_summary_monthly",
])
def test_a_failure_at_any_write_leaves_zero_partial_rows(pg, monkeypatch, statement):
    commit_direct(pg, ctx(), [client(), work()])
    values = {k: v for k, v in payment().after.items() if k not in {"payment_id", "revision"}}
    proposal = pg.propose(ctx(), api.ProposeInput(rid(), "payments", None, 0, values))
    before = everything(pg)
    real = PgLedgerTx._exec

    def failing(self, sql, params=None):
        if sql.startswith(statement):
            raise api.LedgerError("ledger_unavailable", reason="injected")
        return real(self, sql, params)

    monkeypatch.setattr(PgLedgerTx, "_exec", failing)
    with pytest.raises(api.LedgerError, match="^ledger_unavailable$"):
        pg.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    monkeypatch.undo()
    assert everything(pg) == before
    assert pg.decide(ctx(), proposal.proposal_id, proposal.approval_id,
                     "approve").receipt.status == "committed"


def test_a_failure_after_finish_inside_the_block_rolls_everything_back(pg):
    before = everything(pg)
    with pytest.raises(api.LedgerError) as caught:
        with pg.transaction(ctx(), domain="core", kind="owner_direct") as tx:
            request_id = rid()
            proposal = tx.insert_proposal(direct_draft(tx, [task()], request_id, window_s(pg)))
            tx.apply([task()], source="OS_DIRECT", request_id=request_id)
            tx.finish(proposal, status="committed", receipt_request_id=request_id,
                      applied=[task()])
            raise RuntimeError("caller bug after the receipt")
    assert (caught.value.code, caught.value.reason) == ("ledger_unavailable", "unexpected_error")
    assert everything(pg) == before


def test_a_swallowed_database_error_never_commits(pg):
    before = everything(pg)
    with pytest.raises(api.LedgerError, match="^ledger_integrity_failed$"):
        with pg.transaction(ctx(), domain="core", kind="owner_direct") as tx:
            draft = direct_draft(tx, [task()], rid(), window_s(pg))
            bad = api.ProposalDraft(**{**draft.__dict__, "expected_revision": -1})
            try:
                tx.insert_proposal(bad)  # violates a CHECK: SQLSTATE class 23
            except api.LedgerError as exc:
                assert exc.code == "ledger_integrity_failed"
            with pytest.raises(api.LedgerError, match="^ledger_integrity_failed$"):
                tx.get("tasks", "TAS_t1")  # the transaction is poisoned
    assert everything(pg) == before


def test_a_failure_during_commit_is_unknown_and_recovered_by_a_read(pg, monkeypatch):
    proposal = propose_task(pg)

    def commit_then_fail(self, trans):
        trans.commit()
        raise ConnectionError("the COMMIT reply was lost")

    monkeypatch.setattr(core.PgLedger, "_commit", commit_then_fail)
    with pytest.raises(api.LedgerError) as caught:
        pg.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    assert (caught.value.code, caught.value.write_outcome) == ("unknown", "unknown")
    monkeypatch.undo()
    found = pg.receipt(ctx(), proposal.approval_id)
    assert found.found and found.receipt.status == "committed"
    assert len(pg.rows("os_receipts")) == 1


def test_a_commit_that_never_reached_the_server_is_unknown_until_read(pg, monkeypatch):
    proposal = propose_task(pg)

    def fail_before(self, trans):
        raise ConnectionError("never sent")

    monkeypatch.setattr(core.PgLedger, "_commit", fail_before)
    with pytest.raises(api.LedgerError, match="^unknown$"):
        pg.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    monkeypatch.undo()
    assert not pg.receipt(ctx(), proposal.approval_id).found  # unknown stays a read away


def test_reads_never_wait_for_the_workspace_lock(pg):
    proposal = propose_task(pg)
    entered, release = threading.Event(), threading.Event()

    def holder():
        with pg.transaction(ctx(), domain="core", kind="owner_direct"):
            entered.set()
            release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert entered.wait(10)
        started = time.monotonic()
        found = pg.creation_request(ctx(), proposal.request_id)
        head = pg.head(ctx())
        assert time.monotonic() - started < 2
        assert found.found and head.observed_version == 0
    finally:
        release.set()
        thread.join(10)


def test_reads_are_repeatable_read_read_only(pg):
    ledger = pg.transaction.__self__
    with ledger._read(ctx()) as tx:
        assert tx._exec("SHOW transaction_isolation").scalar() == "repeatable read"
        assert tx._exec("SHOW transaction_read_only").scalar() == "on"
    with ledger.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        assert tx._exec("SHOW transaction_isolation").scalar() == "read committed"


def test_a_tampered_proposal_is_never_decided(pg):
    proposal = propose_task(pg, title="Owner approved this")
    admin = pg.ensure_workspace.__self__._engine
    with admin.begin() as c:
        c.execute(text(
            "UPDATE assaf_os.os_proposals SET mutations = jsonb_set(mutations, "
            "'{0,after,title}', '\"Something else\"') WHERE workspace_id = :ws"),
            {"ws": WORKSPACE})
    before = everything(pg)
    with pytest.raises(api.LedgerError) as caught:
        pg.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    assert (caught.value.code, caught.value.reason) == (
        "ledger_integrity_failed", "envelope_mismatch")
    assert everything(pg) == before
