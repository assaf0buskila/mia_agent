"""§22 E26: which apply refusals ``decide`` turns into a receipt, on the fake and on PG.

Only three codes end the proposal: ``has_dependents`` (E26, reason code ``has_dependents``) and the
two it already mapped, ``target_changed`` and ``lead_already_exists`` (no reason code). Every other
refusal, ``ledger_integrity_failed`` above all (E22, §5.2), is re-raised: the transaction rolls
back, nothing is written and the proposal stays pending. Most of these cannot be provoked through a
``kind="proposal"`` (the dependency digest catches the change first), so each implementation's
``apply`` is replaced by one that refuses with the code under test. Not a conformance case: it
patches implementation classes, which the shared suite never does.
"""

import pytest
from app.services.os_ledger import api

from tests.fakes import os_ledger as fake_module
from tests.fakes.os_ledger_builders import (
    TASK_OPEN,
    commit_direct,
    ctx,
    rid,
    snapshot,
    task,
)

MAPPED = {
    "has_dependents": ("target_changed", ("has_dependents",)),
    "target_changed": ("target_changed", ()),
    "lead_already_exists": ("target_changed", ()),
}
RE_RAISED = ("ledger_integrity_failed", "ledger_unavailable", "validation_error",
             "authority_changed")


def refusing_apply(monkeypatch, env, code):
    if env.kind == "fake":
        owner = fake_module._Tx
    else:
        from app.services.os_ledger.tx import PgLedgerTx

        owner = PgLedgerTx

    def refuse(self, mutations, *, source, request_id):
        raise api.LedgerError(code)

    monkeypatch.setattr(owner, "apply", refuse)


def pending_proposal(env):
    commit_direct(env, ctx(), [task()])
    return env.propose(ctx(), api.ProposeInput(
        rid(), "tasks", "TAS_t1", 1, {"title": "Proposed", "status": TASK_OPEN}))


def status_of(env, proposal):
    return {p["proposal_id"]: p["status"] for p in env.rows("os_proposals")}[
        proposal.proposal_id]


@pytest.mark.parametrize("code", sorted(MAPPED))
def test_decide_finishes_target_changed_for_exactly_its_mapped_apply_refusals(
        ledger, monkeypatch, code):
    proposal = pending_proposal(ledger)
    refusing_apply(monkeypatch, ledger, code)
    before = snapshot(ledger)
    result = ledger.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    assert (result.receipt.status, result.receipt.reason_codes) == MAPPED[code]
    assert result.receipt.request_id == proposal.approval_id
    after = snapshot(ledger)
    assert after["os_tasks"] == before["os_tasks"] and after["os_outbox"] == before["os_outbox"]
    assert len(after["os_receipts"]) == len(before["os_receipts"]) + 1
    assert len(after["os_events"]) == len(before["os_events"]) + 1
    assert status_of(ledger, proposal) == "target_changed"


@pytest.mark.parametrize("code", RE_RAISED)
def test_decide_re_raises_every_other_apply_refusal_and_writes_nothing(
        ledger, monkeypatch, code):
    proposal = pending_proposal(ledger)
    refusing_apply(monkeypatch, ledger, code)
    before = snapshot(ledger)
    with pytest.raises(api.LedgerError) as caught:
        ledger.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    assert caught.value.code == code
    assert snapshot(ledger) == before
    assert status_of(ledger, proposal) == "pending"
