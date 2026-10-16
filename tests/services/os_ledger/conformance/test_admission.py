"""Transaction admission (§5.3): order, codes, authority states, epoch layers, expiry admission."""

from datetime import timedelta

import pytest
from app.services.os_ledger import api

from tests.fakes.contexts import for_worker
from tests.fakes.os_ledger_builders import (
    commit_direct,
    counts,
    ctx,
    fresh_workspace,
    propose_task,
    rid,
    task,
)
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE

CORE_STATES = ("shadow", "active", "fenced", "restoring")
ACTIVE_KINDS = ("proposal", "owner_direct", "owner_undo")


def open_and_close(env, context, kind, domain="core", **kw):
    with env.transaction(context, domain=domain, kind=kind, **kw) as tx:
        return tx.version


def refused(code):
    return pytest.raises(api.LedgerError, match=f"^{code}$")


def batch_draft(tx):
    return api.ProposalDraft(
        kind="statement_batch", source="ASSISTANT_PASTE", request_id=rid(),
        request_digest=api.digest({"batch": 1}), entity="statements",
        record_id="STM_" + "0" * 32, expected_revision=0, mutations=(),
        dependency_digest=api.digest([]), expires_at=tx.now + timedelta(days=7))


def pending_batch(env):
    """A pending statement_batch proposal in the standard workspace (statements activated)."""
    env.transition(WORKSPACE, "statements", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    with env.transaction(ctx(), domain="statements", kind="statement_batch") as tx:
        return tx.insert_proposal(batch_draft(tx))


@pytest.mark.parametrize("core", CORE_STATES)
@pytest.mark.parametrize("kind", [*ACTIVE_KINDS, "import"])
def test_core_state_decides_admission_per_kind(ledger, kind, core):
    fresh_workspace(ledger, "ws", core=core)
    admitted = core == ("shadow" if kind == "import" else "active")
    if admitted:
        assert open_and_close(ledger, ctx(workspace="ws"), kind) == 0
    else:
        with refused("authority_not_active"):
            open_and_close(ledger, ctx(workspace="ws"), kind)


@pytest.mark.parametrize("statements", ("shadow", "active", "fenced"))
@pytest.mark.parametrize("core", CORE_STATES)
def test_statement_batch_needs_core_and_statements_active(ledger, core, statements):
    fresh_workspace(ledger, "ws", core=core, statements=statements)
    context = ctx(workspace="ws")
    if (core, statements) == ("active", "active"):
        assert open_and_close(ledger, context, "statement_batch", "statements") == 0
    else:
        with refused("authority_not_active"):
            open_and_close(ledger, context, "statement_batch", "statements")


def test_import_admission_ignores_the_backend_setting(ledger):
    fresh_workspace(ledger, "ws")
    ledger.knobs.backend = "sheets"
    assert open_and_close(ledger, ctx(workspace="ws"), "import") == 0
    with refused("backend_not_postgres"):
        open_and_close(ledger, ctx(), "proposal")


def test_epoch_ctx_must_equal_settings(ledger):
    ledger.knobs.settings_epoch = 2
    with refused("authority_changed"):
        open_and_close(ledger, ctx(epoch=1), "owner_direct")


def test_epoch_settings_and_ctx_must_equal_workspace(ledger):
    ledger.knobs.settings_epoch = 2
    with refused("authority_changed"):
        open_and_close(ledger, ctx(epoch=2), "owner_direct")


def test_epoch_zero_setting_refuses_every_write(ledger):
    ledger.knobs.settings_epoch = 0
    with refused("authority_changed"):
        open_and_close(ledger, ctx(epoch=1), "owner_direct")


def test_owner_mismatch_is_authority_changed(ledger):
    with refused("authority_changed"):
        open_and_close(ledger, ctx(owner=999), "owner_direct")


def test_no_workspace_row_is_ledger_unavailable(ledger):
    with pytest.raises(api.LedgerError) as caught:
        open_and_close(ledger, ctx(workspace="nowhere"), "owner_direct")
    assert caught.value.code == "ledger_unavailable"
    assert caught.value.reason == "workspace_missing"


@pytest.mark.parametrize("operation", ("transaction", "propose", "decide"))
def test_kill_switch_refuses_every_entry_point(ledger, operation):
    proposal = propose_task(ledger)
    ledger.knobs.kill_switch = True
    calls = {
        "transaction": lambda: open_and_close(ledger, ctx(), "owner_direct"),
        "propose": lambda: propose_task(ledger),
        "decide": lambda: ledger.decide(ctx(), proposal.proposal_id, proposal.approval_id,
                                        "approve"),
    }
    with refused("kill_switch"):
        calls[operation]()


def test_ledger_disabled_refuses_with_ledger_unavailable(ledger):
    ledger.knobs.enabled = False
    with refused("ledger_unavailable"):
        open_and_close(ledger, ctx(), "owner_direct")


def test_freeze_blocks_new_intent_but_not_decide_or_transactions(ledger):
    proposal = propose_task(ledger)
    ledger.knobs.freeze = True
    with refused("os_frozen"):
        propose_task(ledger)
    assert open_and_close(ledger, ctx(), "owner_direct") == 0
    result = ledger.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    assert result.receipt.status == "committed"


@pytest.mark.parametrize("breaker,code", [
    ("kill_switch", "kill_switch"),
    ("backend", "backend_not_postgres"),
])
def test_admission_order_switches_come_before_the_workspace_row(ledger, breaker, code):
    ledger.knobs.kill_switch = breaker == "kill_switch"
    ledger.knobs.backend = "sheets" if breaker == "backend" else "postgres"
    with refused(code):
        open_and_close(ledger, ctx(workspace="nowhere"), "owner_direct")


def test_admission_order_kill_switch_comes_before_backend(ledger):
    ledger.knobs.kill_switch = True
    ledger.knobs.backend = "sheets"
    with refused("kill_switch"):
        open_and_close(ledger, ctx(), "owner_direct")


def test_admission_order_workspace_row_comes_before_epoch(ledger):
    ledger.knobs.settings_epoch = 2
    with pytest.raises(api.LedgerError) as caught:
        open_and_close(ledger, ctx(workspace="nowhere", epoch=1), "owner_direct")
    assert caught.value.code == "ledger_unavailable"


def test_admission_order_epoch_comes_before_state(ledger):
    fresh_workspace(ledger, "ws", core="shadow")
    ledger.knobs.settings_epoch = 2
    with refused("authority_changed"):
        open_and_close(ledger, ctx(workspace="ws", epoch=1), "owner_direct")


# --- purpose="expire" ------------------------------------------------------------------------


def worker():
    return for_worker("retention", workspace=WORKSPACE)


def expire_batch(env, batch, *, context=None, **kw):
    with env.transaction(context or worker(), domain="statements", kind="statement_batch",
                         purpose="expire") as tx:
        stored = tx.proposal(batch.proposal_id)
        return tx.finish(stored, status="expired", receipt_request_id=stored.rejection_id, **kw)


def test_expire_admission_runs_under_every_switch(ledger):
    batch = pending_batch(ledger)
    ledger.advance(8 * 86400)
    ledger.knobs.kill_switch = True
    ledger.knobs.backend = "sheets"
    ledger.knobs.enabled = False
    ledger.transition(WORKSPACE, "statements", "fenced", expected_epoch=1, actor_owner_id=OWNER_ID)
    before = ledger.version()
    receipt = expire_batch(ledger, batch)
    assert (receipt.status, receipt.kind) == ("expired", "statement_batch")
    assert receipt.request_id == batch.rejection_id
    assert ledger.version() == before + 1
    assert len(ledger.rows("os_events")) == 1
    assert ledger.rows("os_outbox") == []


def test_expire_admission_still_checks_the_epoch(ledger):
    batch = pending_batch(ledger)
    ledger.advance(8 * 86400)
    ledger.knobs.settings_epoch = 2
    with refused("authority_changed"):
        expire_batch(ledger, batch)


def test_expire_admission_still_needs_the_workspace_row(ledger):
    with pytest.raises(api.LedgerError) as caught:
        open_and_close(ledger, for_worker("retention", workspace="nowhere"),
                       "statement_batch", "statements", purpose="expire")
    assert caught.value.code == "ledger_unavailable"


def test_expire_requires_the_worker_channel(ledger):
    with pytest.raises(AssertionError, match="ledger_invariant:expire_scope"):
        open_and_close(ledger, ctx(), "statement_batch", "statements", purpose="expire")


def test_expire_requires_kind_statement_batch(ledger):
    with pytest.raises(AssertionError, match="ledger_invariant:expire_scope"):
        open_and_close(ledger, worker(), "owner_direct", "statements", purpose="expire")


@pytest.mark.parametrize("call", ("get", "get_many", "find_expenses", "dependents", "receipt",
                                  "proposal_by_request", "insert_proposal", "apply"))
def test_expire_scope_refuses_every_other_call(ledger, call):
    batch = pending_batch(ledger)
    ledger.advance(8 * 86400)
    calls = {
        "get": lambda tx: tx.get("tasks", "TAS_x"),
        "get_many": lambda tx: tx.get_many([api.RecordRef("tasks", "TAS_x")]),
        "find_expenses": lambda tx: tx.find_expenses(ids=["EXP_x"]),
        "dependents": lambda tx: tx.dependents("clients", "PER_x"),
        "receipt": lambda tx: tx.receipt(rid()),
        "proposal_by_request": lambda tx: tx.proposal_by_request(batch.request_id),
        "insert_proposal": lambda tx: tx.insert_proposal(batch_draft(tx)),
        "apply": lambda tx: tx.apply([task()], source="OS_DIRECT", request_id=rid()),
    }
    with pytest.raises(AssertionError, match="ledger_invariant:expire_scope"):
        with ledger.transaction(worker(), domain="statements", kind="statement_batch",
                                purpose="expire") as tx:
            calls[call](tx)


def test_expire_scope_allows_only_the_expired_status(ledger):
    batch = pending_batch(ledger)
    ledger.advance(8 * 86400)
    with pytest.raises(AssertionError, match="ledger_invariant:expire_scope"):
        with ledger.transaction(worker(), domain="statements", kind="statement_batch",
                                purpose="expire") as tx:
            stored = tx.proposal(batch.proposal_id)
            tx.finish(stored, status="rejected", receipt_request_id=stored.rejection_id)


def test_expire_finishes_a_batch_without_detail_lines_before_its_expiry(ledger):
    """§5.3 "or that has no batch lines" / §12.2 ``restored_without_detail``.

    The conformance suite writes no batch lines, so every pending batch here has none. The batch
    is still inside its seven days and in the current epoch.
    """
    batch = pending_batch(ledger)
    before = ledger.version()
    receipt = expire_batch(ledger, batch, reason_codes=("restored_without_detail",))
    assert (receipt.status, receipt.kind) == ("expired", "statement_batch")
    assert receipt.reason_codes == ("restored_without_detail",)
    assert receipt.request_id == batch.rejection_id and receipt.epoch == 1
    assert ledger.version() == before + 1
    assert len(ledger.rows("os_events")) == 1 and ledger.rows("os_outbox") == []
    stored = next(r for r in ledger.rows("os_proposals") if r["proposal_id"] == batch.proposal_id)
    assert stored["status"] == "expired"


def test_expire_finishes_a_batch_of_an_older_epoch(ledger):
    """§12.2: ``expire_due`` also expires a pending batch of an older epoch (``epoch_changed``)."""
    batch = pending_batch(ledger)
    ledger.bump_epoch(WORKSPACE, expected_epoch=1)
    ledger.knobs.settings_epoch = 2
    worker_two = for_worker("retention", workspace=WORKSPACE, epoch=2)
    receipt = expire_batch(ledger, batch, context=worker_two, reason_codes=("epoch_changed",))
    assert receipt.status == "expired" and receipt.reason_codes == ("epoch_changed",)
    assert receipt.epoch == 1 and receipt.request_id == batch.rejection_id
    assert ledger.version() == 1


def test_expire_scope_refuses_a_proposal_that_is_not_a_statement_batch(ledger):
    proposal = propose_task(ledger)
    ledger.advance(8 * 86400)
    with pytest.raises(AssertionError, match="ledger_invariant:expire_scope"):
        with ledger.transaction(worker(), domain="statements", kind="statement_batch",
                                purpose="expire") as tx:
            stored = tx.proposal(proposal.proposal_id)
            tx.finish(stored, status="expired", receipt_request_id=stored.rejection_id)


def test_a_refused_admission_writes_nothing(ledger):
    commit_direct(ledger, ctx(), [task()])
    before = counts(ledger)
    ledger.knobs.kill_switch = True
    with refused("kill_switch"):
        commit_direct(ledger, ctx(), [task("TAS_t2")])
    assert counts(ledger) == before
