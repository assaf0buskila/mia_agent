"""Proposals and decisions (§5.3): replay table, terminal statuses, keys, races, keyspace."""

import threading
from datetime import timedelta

import pytest
from app.services.os_ledger import api

from tests.fakes.os_ledger_builders import (
    TASK_OPEN,
    abandon_draft,
    commit_direct,
    commit_import,
    counts,
    ctx,
    current,
    direct_draft,
    fresh_workspace,
    propose_task,
    rejection_of,
    rid,
    task,
    update,
    window_s,
    workspace_row,
)
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE

TERMINAL = ("committed", "rejected", "expired", "target_changed", "superseded", "abandoned")


def decide(env, proposal, decision="approve", **kw):
    ident = proposal.approval_id if decision == "approve" else proposal.rejection_id
    return env.decide(kw.pop("context", ctx()), proposal.proposal_id, kw.pop("decision_id", ident),
                      decision)


def test_propose_creates_a_pending_proposal_and_moves_since_but_not_version(ledger):
    head = ledger.head(ctx())
    proposal = propose_task(ledger)
    after = ledger.head(ctx())
    assert (proposal.status, proposal.kind, proposal.source) == ("pending", "proposal", "OS")
    assert (proposal.epoch, proposal.owner_id, proposal.workspace_id) == (1, OWNER_ID, WORKSPACE)
    assert proposal.approval_id != proposal.rejection_id
    assert proposal.expires_at - ledger.now() <= timedelta(minutes=10)
    assert after.observed_version == head.observed_version
    assert after.pending_seq == head.pending_seq + 1
    assert api.since_token(after) != api.since_token(head)
    assert ledger.version() == 0


def test_propose_replay_with_an_equal_digest_returns_the_stored_proposal(ledger):
    request = api.ProposeInput(rid(), "tasks", None, 0, {"title": "A", "status": TASK_OPEN})
    first = ledger.propose(ctx(), request)
    assert ledger.propose(ctx(), request) == first
    assert len(ledger.rows("os_proposals")) == 1
    assert workspace_row(ledger)["pending_seq"] == 1


@pytest.mark.parametrize("change", ("input", "channel"))
def test_propose_replay_with_a_different_digest_is_idempotency_mismatch(ledger, change):
    request_id = rid()
    ledger.propose(ctx(), api.ProposeInput(request_id, "tasks", None, 0,
                                           {"title": "A", "status": TASK_OPEN}))
    before = counts(ledger)
    other = {"title": "B" if change == "input" else "A", "status": TASK_OPEN}
    with pytest.raises(api.LedgerError, match="^idempotency_mismatch$"):
        ledger.propose(ctx("MIA" if change == "channel" else "OS"),
                       api.ProposeInput(request_id, "tasks", None, 0, other))
    assert counts(ledger) == before


def test_propose_by_the_telegram_approval_channel_is_denied(ledger):
    with pytest.raises(api.LedgerError, match="^channel_operation_denied$"):
        propose_task(ledger, ctx("TELEGRAM_APPROVAL"))
    assert ledger.rows("os_proposals") == []


def test_decide_approve_commits_one_receipt_one_event_one_outbox_row(ledger):
    proposal = propose_task(ledger)
    result = decide(ledger, proposal)
    receipt = result.receipt
    assert result.found and receipt.status == "committed" and receipt.kind == "proposal"
    assert receipt.request_id == proposal.approval_id and receipt.commit_version == 1
    assert receipt.records[0]["entity"] == "tasks"
    assert receipt.records[0]["record_id"] == proposal.record_id
    assert receipt.records[0]["revision"] == 1 and receipt.undo_until is None
    assert [e for e, _ in result.records] == ["tasks"]
    assert ledger.version() == 1
    assert (len(ledger.rows("os_receipts")), len(ledger.rows("os_events"))) == (1, 1)
    outbox = ledger.rows("os_outbox")
    assert len(outbox) == 1
    assert outbox[0]["body"] == {"authority_epoch": 1, "refs": [
        {"entity": "tasks", "record_id": proposal.record_id, "revision": 1}]}
    assert ledger.page(ctx(), "tasks", after=None, limit=50).total == 1


def test_decide_reject_commits_nothing_and_bumps_the_version(ledger):
    proposal = propose_task(ledger)
    result = decide(ledger, proposal, "reject")
    assert result.receipt.status == "rejected" and result.receipt.records == ()
    assert result.receipt.request_id == proposal.rejection_id
    assert ledger.version() == 1
    assert len(ledger.rows("os_events")) == 1 and ledger.rows("os_outbox") == []
    assert ledger.page(ctx(), "tasks", after=None, limit=50).total == 0


@pytest.mark.parametrize("decision", ("approve", "reject"))
def test_late_decision_finishes_expired_under_the_supplied_id(ledger, decision):
    proposal = propose_task(ledger)
    ledger.advance(601)
    result = decide(ledger, proposal, decision)
    supplied = proposal.approval_id if decision == "approve" else proposal.rejection_id
    assert result.receipt.status == "expired" and result.receipt.request_id == supplied
    assert ledger.page(ctx(), "tasks", after=None, limit=50).total == 0
    assert ledger.rows("os_outbox") == []


@pytest.mark.parametrize("decision", ("approve", "reject"))
def test_a_decision_id_that_does_not_match_the_operation_is_refused(ledger, decision):
    proposal = propose_task(ledger)
    wrong = proposal.rejection_id if decision == "approve" else proposal.approval_id
    before = counts(ledger)
    with pytest.raises(api.LedgerError, match="^decision_request_mismatch$"):
        decide(ledger, proposal, decision, decision_id=wrong)
    with pytest.raises(api.LedgerError, match="^decision_request_mismatch$"):
        decide(ledger, proposal, decision, decision_id=rid())
    assert counts(ledger) == before


@pytest.mark.parametrize("first,second", (("approve", "reject"), ("reject", "approve")))
def test_a_second_different_decision_is_already_decided(ledger, first, second):
    proposal = propose_task(ledger)
    decide(ledger, proposal, first)
    before = counts(ledger)
    with pytest.raises(api.LedgerError, match="^already_decided$"):
        decide(ledger, proposal, second)
    assert counts(ledger) == before


def test_replaying_the_same_decision_returns_the_stored_receipt(ledger):
    proposal = propose_task(ledger)
    first = decide(ledger, proposal)
    before = counts(ledger)
    again = decide(ledger, proposal)
    assert again.receipt == first.receipt
    assert counts(ledger) == before


def test_decide_channel_rules(ledger):
    proposal = propose_task(ledger)
    with pytest.raises(api.LedgerError, match="^owner_approval_required$"):
        decide(ledger, proposal, context=ctx("MIA"))
    assert ledger.rows("os_receipts") == []
    result = decide(ledger, proposal, context=ctx("TELEGRAM_APPROVAL"))
    assert result.receipt.status == "committed"


def test_decide_on_an_unknown_proposal_is_404(ledger):
    with pytest.raises(api.LedgerError, match="^proposal_not_found$"):
        ledger.decide(ctx(), "PRO_missing", rid(), "approve")


def statement_batch_proposal(env):
    env.transition(WORKSPACE, "statements", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    with env.transaction(ctx(), domain="statements", kind="statement_batch") as tx:
        return tx.insert_proposal(api.ProposalDraft(
            kind="statement_batch", source="ASSISTANT_PASTE", request_id=rid(),
            request_digest=api.digest({"b": 1}), entity="statements", record_id="STM_" + "1" * 32,
            expected_revision=0, mutations=(), dependency_digest=api.digest([]),
            expires_at=tx.now + timedelta(days=1)))


def import_proposal_in_active_workspace(env):
    fresh_workspace(env, "ws-i")
    proposal, _ = commit_import(env, ctx(workspace="ws-i"), [], api.digest({"m": 1}))
    env.transition("ws-i", "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    return proposal, ctx(workspace="ws-i")


@pytest.mark.parametrize("kind", ("statement_batch", "owner_direct", "import"))
def test_decide_on_a_non_proposal_kind_is_404_with_zero_new_rows(ledger, kind):
    context = ctx()
    if kind == "statement_batch":
        target = statement_batch_proposal(ledger)
    elif kind == "owner_direct":
        target, _ = commit_direct(ledger, ctx(), [task()])
    else:
        target, context = import_proposal_in_active_workspace(ledger)
    before = counts(ledger)
    for decision in ("approve", "reject"):
        ident = target.approval_id if decision == "approve" else target.rejection_id
        with pytest.raises(api.LedgerError, match="^proposal_not_found$"):
            ledger.decide(context, target.proposal_id, ident, decision)
    assert counts(ledger) == before


def test_a_dependency_change_gives_a_target_changed_receipt(ledger):
    commit_direct(ledger, ctx(), [task()])
    original = current(ledger, "tasks", "TAS_t1")
    proposal = ledger.propose(ctx(), api.ProposeInput(
        rid(), "tasks", "TAS_t1", 1, {"title": "Proposed", "status": TASK_OPEN}))
    commit_direct(ledger, ctx(), [update(original, title="Changed meanwhile")])
    before = counts(ledger)
    result = decide(ledger, proposal)
    assert result.receipt.status == "target_changed" and result.receipt.records == ()
    assert result.receipt.request_id == proposal.approval_id
    assert current(ledger, "tasks", "TAS_t1")["title"] == "Changed meanwhile"
    after = counts(ledger)
    assert after["version"] == before["version"] + 1
    assert after["os_events"] == before["os_events"] + 1
    assert after["os_outbox"] == before["os_outbox"]


def run_in_threads(*calls):
    barrier = threading.Barrier(len(calls))
    outcomes = [None] * len(calls)

    def runner(index, call):
        barrier.wait(timeout=10)
        try:
            outcomes[index] = ("ok", call())
        except api.LedgerError as exc:
            outcomes[index] = ("error", exc.code)

    threads = [threading.Thread(target=runner, args=(i, c)) for i, c in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    return outcomes


def test_two_thread_approve_race_gives_one_receipt_one_event_one_outbox_row(ledger):
    proposal = propose_task(ledger)
    call = lambda: decide(ledger, proposal)  # noqa: E731
    outcomes = run_in_threads(call, call)
    for kind, value in outcomes:
        assert (kind == "ok" and value.receipt.status == "committed") or (
            kind == "error" and value == "already_decided")
    assert any(kind == "ok" for kind, _ in outcomes)
    assert ledger.version() == 1
    assert len(ledger.rows("os_receipts")) == 1
    assert len(ledger.rows("os_events")) == 1
    assert len(ledger.rows("os_outbox")) == 1
    assert len(ledger.rows("os_tasks")) == 1


def test_two_thread_approve_versus_reject_race_has_exactly_one_winner(ledger):
    proposal = propose_task(ledger)
    outcomes = run_in_threads(lambda: decide(ledger, proposal),
                              lambda: decide(ledger, proposal, "reject"))
    kinds = sorted(kind for kind, _ in outcomes)
    assert kinds == ["error", "ok"]
    assert [v for k, v in outcomes if k == "error"] == ["already_decided"]
    assert ledger.version() == 1
    assert (len(ledger.rows("os_receipts")), len(ledger.rows("os_events"))) == (1, 1)


def terminal_step(env, status):
    """Do any setup, then return a callable that performs only the terminal step.

    The callable returns ``(proposal, receipt_key)``.
    """
    if status == "abandoned":
        request_id = rid()
        with env.transaction(ctx(), domain="core", kind="owner_direct") as tx:
            proposal = tx.insert_proposal(abandon_draft(tx, request_id, window_s(env)))

        def step():
            with env.transaction(ctx(), domain="core", kind="owner_direct") as tx:
                tx.finish(tx.proposal(proposal.proposal_id), status="abandoned",
                          receipt_request_id=request_id)
            return proposal, request_id

        return step
    if status == "target_changed":
        commit_direct(env, ctx(), [task("TAS_k")])
        proposal = env.propose(ctx(), api.ProposeInput(
            rid(), "tasks", "TAS_k", 1, {"title": "P", "status": TASK_OPEN}))
        commit_direct(env, ctx(), [update(current(env, "tasks", "TAS_k"), title="Moved")])

        def step():
            decide(env, proposal)
            return proposal, proposal.approval_id

        return step
    proposal = propose_task(env)
    if status == "superseded":
        def step():
            with env.transaction(ctx(), domain="core", kind="proposal") as tx:
                tx.finish(tx.proposal(proposal.proposal_id), status="superseded",
                          receipt_request_id=proposal.rejection_id)
            return proposal, proposal.rejection_id

        return step
    if status == "expired":
        env.advance(601)
    decision = "reject" if status == "rejected" else "approve"
    key = proposal.rejection_id if decision == "reject" else proposal.approval_id

    def step():
        decide(env, proposal, decision)
        return proposal, key

    return step


@pytest.mark.parametrize("status", TERMINAL)
def test_every_terminal_status_bumps_version_and_pending_seq_by_exactly_one(ledger, status):
    step = terminal_step(ledger, status)
    before = counts(ledger)
    step()
    after = counts(ledger)
    assert after["version"] == before["version"] + 1
    assert after["pending_seq"] == before["pending_seq"] + 1
    assert after["os_receipts"] == before["os_receipts"] + 1
    assert after["os_events"] == before["os_events"] + 1
    assert after["os_outbox"] == before["os_outbox"] + (1 if status == "committed" else 0)
    latest = max(ledger.rows("os_receipts"), key=lambda r: r["commit_version"])
    assert (latest["status"], latest["commit_version"]) == (status, after["version"])


@pytest.mark.parametrize("status", TERMINAL)
def test_every_receipt_is_stored_under_the_key_of_the_receipt_key_table(ledger, status):
    proposal, key = terminal_step(ledger, status)()
    stored = [r for r in ledger.rows("os_receipts") if r["proposal_id"] == proposal.proposal_id]
    assert [r["request_id"] for r in stored] == [key]
    assert [r["status"] for r in stored] == [status]
    found = ledger.receipt(ctx(), key)
    assert found.found and found.receipt.status == status
    for other in {proposal.approval_id, proposal.rejection_id, proposal.request_id} - {key}:
        assert not ledger.receipt(ctx(), other).found


def fused_draft_for(tx, kind, request_id, window):
    common = dict(request_digest=api.digest({kind: request_id}), expected_revision=0,
                  mutations=(), dependency_digest=api.digest([]),
                  expires_at=tx.now + timedelta(seconds=window))
    if kind == "import":
        return api.ProposalDraft(kind="import", source="IMPORT", request_id=request_id,
                                 entity="import", record_id=rid(), **common)
    return api.ProposalDraft(kind=kind, source="OS_DIRECT", request_id=request_id,
                             entity="tasks", record_id="TAS_fused", **common)


def minted_proposal(env, context, window):
    """A server-minted ``kind="proposal"`` row, created in a transaction of its own kind."""
    with env.transaction(context, domain="core", kind="proposal") as tx:
        return tx.insert_proposal(server_minted_draft(tx, window))


def server_minted_draft(tx, window):
    return api.ProposalDraft(
        kind="proposal", source="OS", request_id=rid(), request_digest=api.digest({"p": 1}),
        entity="tasks", record_id="TAS_p", expected_revision=0, mutations=(),
        dependency_digest=api.digest([]), expires_at=tx.now + timedelta(seconds=window))


@pytest.mark.parametrize("source", ("fused_approval", "fused_rejection", "proposal_approval",
                                    "proposal_rejection"))
@pytest.mark.parametrize("kind", ("owner_direct", "owner_undo", "import"))
def test_each_keyspace_collision_is_idempotency_mismatch_and_writes_nothing(ledger, kind, source):
    workspace = "ws-k" if kind == "import" else WORKSPACE
    window = window_s(ledger)
    if kind == "import":
        # An import needs core=shadow, but a proposal needs core=active: create the minted row
        # while active, then fenced -> shadow (epoch 2; no non-import receipt exists yet).
        fresh_workspace(ledger, workspace, core="active")
        minted = minted_proposal(ledger, ctx(workspace=workspace), window)
        for state in ("fenced", "shadow"):
            ledger.transition(workspace, "core", state, expected_epoch=1, actor_owner_id=OWNER_ID)
        ledger.knobs.settings_epoch = 2
        context = ctx(workspace=workspace, epoch=2)
    else:
        context = ctx(workspace=workspace)
        minted = minted_proposal(ledger, context, window)
    with ledger.transaction(context, domain="core", kind=kind) as tx:
        base_id = rid()
        base = tx.insert_proposal(fused_draft_for(tx, kind, base_id, window))
    assert base.approval_id == base_id and base.rejection_id == rejection_of(base_id)
    colliding = {
        "fused_approval": base.approval_id,
        "fused_rejection": base.rejection_id,
        "proposal_approval": minted.approval_id,
        "proposal_rejection": minted.rejection_id,
    }[source]
    before = {"proposals": len(ledger.rows("os_proposals")),
              "pending_seq": workspace_row(ledger, workspace)["pending_seq"],
              "version": ledger.version(workspace)}
    with ledger.transaction(context, domain="core", kind=kind) as tx:
        with pytest.raises(api.LedgerError, match="^idempotency_mismatch$"):
            tx.insert_proposal(fused_draft_for(tx, kind, colliding, window))
        assert tx.proposal_by_request(colliding) in (None, base)
    assert len(ledger.rows("os_proposals")) == before["proposals"]
    assert workspace_row(ledger, workspace)["pending_seq"] == before["pending_seq"]
    assert ledger.version(workspace) == before["version"]


@pytest.mark.parametrize("decision", ("approve", "reject"))
def test_a_receipt_key_of_a_decided_proposal_blocks_a_fused_id(ledger, decision):
    window = window_s(ledger)
    proposal = propose_task(ledger)
    receipt = decide(ledger, proposal, decision).receipt
    assert receipt.kind == "proposal" and ledger.rows("os_receipts")[0]["request_id"] == (
        receipt.request_id)
    before = counts(ledger)
    with ledger.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        with pytest.raises(api.LedgerError, match="^idempotency_mismatch$"):
            tx.insert_proposal(fused_draft_for(tx, "owner_direct", receipt.request_id, window))
    assert counts(ledger) == before


def test_a_fresh_fused_id_does_not_collide(ledger):
    with ledger.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        first = tx.insert_proposal(direct_draft(tx, [task()], rid(), window_s(ledger)))
        second = tx.insert_proposal(direct_draft(tx, [task("TAS_t2")], rid(), window_s(ledger)))
    assert first.proposal_id != second.proposal_id
    assert workspace_row(ledger)["pending_seq"] == 2
