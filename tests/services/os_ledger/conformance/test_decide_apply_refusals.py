"""§22 E26: an apply-time ``has_dependents`` in ``decide`` ends the proposal ``target_changed``.

A pending ``kind="proposal"`` that lands a record in its FORK-DELETE end state still matches its
dependency digest when a live referrer was added after it was proposed: the digest covers the
record and what it names, not the records that name it. ``apply`` then refuses ``has_dependents``
(§22 E20). ``decide`` finishes the proposal ``target_changed`` with the reason code
``has_dependents``: the terminal receipt under the approval id, its one event, the version and
``pending_seq`` bumps, and nothing else (no record, outbox or summary row changes). The proposal
leaves ``pending()``, the same decision replays the receipt, and another decision is
``already_decided``.

Every other apply refusal keeps its behaviour: an apply-time ``target_changed`` is still a
``target_changed`` receipt with no reason code (``ledger_integrity_failed`` and the rest are
re-raised; ``tests/services/os_ledger/test_core_decide_apply_refusals.py`` pins those by injection,
which conformance cannot do on PG).
"""

import pytest
from app.services.os_ledger import api

from tests.fakes.os_ledger_builders import (
    CLIENT_INACTIVE,
    WORK_CANCELLED,
    WriteLane,
    client,
    ctx,
    expense,
    payment,
    rid,
    snapshot,
    time_entry,
    tombstone,
    work,
    workspace_row,
)

RECORD = {"clients": "PER_c1", "works": "WOR_w1"}
SEED = {"clients": lambda: [client()], "works": lambda: [client(), work()]}
# Live referrers that the dependency digest does not cover (they name the record).
BLOCKERS = {"clients": lambda: [work()], "works": lambda: [payment(), time_entry()]}
END_PATCH = {"clients": {"status": CLIENT_INACTIVE}, "works": {"status": WORK_CANCELLED}}
UNCHANGED = (*("os_" + entity for entity in api.ENTITIES), "os_outbox", "os_summary_monthly")


def added(before: list[dict], after: list[dict]) -> list[dict]:
    return [row for row in after if row not in before]


def pending_ids(env) -> set[str]:
    return {p.proposal_id for p in env.pending(ctx(), after=None, limit=200).items}


@pytest.mark.parametrize("entity", sorted(RECORD))
def test_an_apply_time_has_dependents_in_decide_finishes_the_proposal_target_changed(
        ledger, entity):
    lane = WriteLane(ledger, "owner_direct")
    lane.write(SEED[entity]())
    record = lane.get(entity, RECORD[entity])
    proposal = ledger.propose(ctx(), api.ProposeInput(
        rid(), entity, RECORD[entity], record["revision"], END_PATCH[entity]))
    lane.write(BLOCKERS[entity]())
    assert proposal.proposal_id in pending_ids(ledger)
    before, workspace = snapshot(ledger), workspace_row(ledger)

    result = ledger.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")

    receipt = result.receipt
    assert (receipt.status, receipt.reason_codes, receipt.records) == (
        "target_changed", ("has_dependents",), ())
    assert (receipt.request_id, receipt.kind, receipt.proposal_id) == (
        proposal.approval_id, "proposal", proposal.proposal_id)
    after = snapshot(ledger)
    for table in UNCHANGED:  # nothing applied: no record, outbox or summary row changed
        assert after[table] == before[table], table
    assert lane.get(entity, RECORD[entity]) == record
    assert [(r["request_id"], r["status"], list(r["reason_codes"]))
            for r in added(before["os_receipts"], after["os_receipts"])] == [
        (proposal.approval_id, "target_changed", ["has_dependents"])]
    events = added(before["os_events"], after["os_events"])
    assert [(e["decision_id"], e["body"]) for e in events] == [
        (proposal.approval_id, {"status": "target_changed", "kind": "proposal", "effects": []})]
    moved = workspace_row(ledger)
    assert (moved["version"], moved["pending_seq"]) == (
        workspace["version"] + 1, workspace["pending_seq"] + 1)
    assert receipt.commit_version == moved["version"]
    assert {p["proposal_id"]: p["status"] for p in after["os_proposals"]}[
        proposal.proposal_id] == "target_changed"
    assert proposal.proposal_id not in pending_ids(ledger)

    # A terminal receipt: the same decision replays it, another is already_decided.
    settled = snapshot(ledger)
    replay = ledger.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    assert replay.receipt == receipt
    with pytest.raises(api.LedgerError) as caught:
        ledger.decide(ctx(), proposal.proposal_id, proposal.rejection_id, "reject")
    assert caught.value.code == "already_decided"
    assert snapshot(ledger) == settled
    stored = ledger.receipt(ctx(), proposal.approval_id)
    assert (stored.receipt.status, stored.receipt.reason_codes) == (
        "target_changed", ("has_dependents",))
    assert stored.undo.state == "none"


def test_an_apply_time_target_changed_in_decide_keeps_no_reason_code(ledger):
    """Only has_dependents gains a reason code. A source key that a since-tombstoned expense used
    leaves the live sources (and so the digest) unchanged, and apply's unique-index check refuses
    it as target_changed, exactly as before §22 E26."""
    lane = WriteLane(ledger, "owner_direct")
    values = {k: v for k, v in expense("EXP_x", source_key="shared").after.items()
              if k not in {"expense_id", "revision"}}
    proposal = ledger.propose(ctx(), api.ProposeInput(rid(), "expenses", None, 0, values))
    lane.write([expense("EXP_e1", source_key="shared")])
    lane.write([tombstone("expenses", lane.get("expenses", "EXP_e1"))])
    before = snapshot(ledger)

    result = ledger.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")

    assert (result.receipt.status, result.receipt.reason_codes) == ("target_changed", ())
    after = snapshot(ledger)
    for table in UNCHANGED:
        assert after[table] == before[table], table
    receipts = added(before["os_receipts"], after["os_receipts"])
    assert [list(r["reason_codes"]) for r in receipts] == [[]]
