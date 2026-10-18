"""``apply`` is a strict compare-and-set: ``before`` must equal the stored record (§22 E22).

The revision alone is not enough. ``IncrementalSummaries`` reads the old month and amount from the
``before`` content, so a mutation that carries the current revision but stale content would move a
payment into a new month and leave the old month counting it until the verifier flags
``summary_drift``. An update, tombstone or restore whose ``before`` differs from the stored record
in any field (the canonical record digest, not just the revision) refuses with
``ledger_integrity_failed`` and writes nothing. A stale REVISION is still the ordinary
``target_changed``; a create has no before-image to compare. One case per write kind.
"""

import pytest
from app.services.os_ledger import api

from tests.fakes.os_ledger_builders import (
    WRITE_KINDS,
    WriteLane,
    client,
    direct_draft,
    expense,
    lead,
    payment,
    restore,
    rid,
    snapshot,
    task,
    time_entry,
    tombstone,
    update,
    window_s,
    work,
)

OPS = ("update", "tombstone", "restore")


def stale_content(op, stored):
    """A mutation carrying the CURRENT revision but not the stored content."""
    forged = {**stored, "title": "Forged"}
    if op == "update":
        return update(forged, title="Next")
    return (tombstone if op == "tombstone" else restore)("tasks", forged)


def refused_integrity(lane, mutations):
    with pytest.raises(api.LedgerError) as caught:
        lane.attempt(mutations)
    assert caught.value.code == "ledger_integrity_failed"
    assert (caught.value.status, caught.value.write_outcome) == (503, "not_started")


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("kind", WRITE_KINDS)
def test_a_before_image_with_the_current_revision_but_other_content_is_refused(ledger, kind, op):
    lane = WriteLane(ledger, kind)
    lane.write([task()])
    if op == "restore":
        lane.write([tombstone("tasks", lane.get("tasks", "TAS_t1"))])
    stored = lane.get("tasks", "TAS_t1", include_deleted=True)
    before = snapshot(ledger)
    refused_integrity(lane, [stale_content(op, stored)])
    assert snapshot(ledger) == before
    # The same mutation built from the stored row is applied, so the refusal was the content.
    good = {"update": update, "tombstone": lambda r: tombstone("tasks", r),
            "restore": lambda r: restore("tasks", r)}[op]
    lane.write([good(stored)])
    assert lane.get("tasks", "TAS_t1", include_deleted=True)["revision"] == stored["revision"] + 1


@pytest.mark.parametrize("forge", ("changed", "dropped", "extra", "retyped"))
def test_any_field_difference_counts_not_only_a_changed_value(ledger, forge):
    lane = WriteLane(ledger, "owner_direct")
    lane.write([task(due_date="2026-11-01")])
    stored = lane.get("tasks", "TAS_t1")
    forged = dict(stored)
    if forge == "changed":
        forged["title"] = "Other"
    elif forge == "dropped":
        del forged["due_date"]
    elif forge == "extra":
        forged["notes"] = "added"
    else:
        forged["due_date"] = ["2026-11-01"]
    before = snapshot(ledger)
    refused_integrity(lane, [update(forged, title="Next")])
    assert snapshot(ledger) == before


def test_a_stale_revision_is_still_target_changed_not_an_integrity_failure(ledger):
    lane = WriteLane(ledger, "owner_direct")
    lane.write([task()])
    first = lane.get("tasks", "TAS_t1")
    lane.write([update(first, title="Second")])
    with pytest.raises(api.LedgerError, match="^target_changed$"):
        lane.attempt([update({**first, "title": "Forged"}, title="Stale")])


def test_one_forged_before_image_in_a_batch_writes_nothing_of_the_batch(ledger):
    lane = WriteLane(ledger, "owner_direct")
    lane.write([task("TAS_a"), task("TAS_b")])
    good = update(lane.get("tasks", "TAS_a"), title="Fine")
    bad = update({**lane.get("tasks", "TAS_b"), "title": "Forged"}, title="Next")
    before = snapshot(ledger)
    refused_integrity(lane, [good, bad])
    assert snapshot(ledger) == before
    assert lane.get("tasks", "TAS_a")["title"] != "Fine"


def test_the_transaction_stays_usable_after_the_refusal_for_a_terminal_receipt(ledger):
    lane = WriteLane(ledger, "owner_direct")
    lane.write([task()])
    stored = lane.get("tasks", "TAS_t1")
    request_id = rid()
    forged = update({**stored, "title": "Forged"}, title="Next")
    with ledger.transaction(lane.context, domain="core", kind="owner_direct") as tx:
        proposal = tx.insert_proposal(direct_draft(tx, [forged], request_id, window_s(ledger)))
        with pytest.raises(api.LedgerError, match="^ledger_integrity_failed$"):
            tx.apply([forged], source="OS_DIRECT", request_id=request_id)
        tx.finish(proposal, status="target_changed", receipt_request_id=request_id)
    assert ledger.receipt(lane.context, request_id).receipt.status == "target_changed"
    assert lane.get("tasks", "TAS_t1") == stored


def test_the_stored_before_image_is_accepted_for_every_entity(ledger):
    """The comparison is over the exact wire record, so each entity's own shape must round-trip
    (a lead carries its ``person_id``; amounts and dates survive the database)."""
    lane = WriteLane(ledger, "owner_direct")
    lane.write([client(), work(agreed_amount_ils=5000), payment(), time_entry(),
                task(due_date="2026-11-01"), expense(), lead()])
    edits = {"clients": {"name": "Renamed"}, "works": {"title": "Retitled"},
             "payments": {"actual_amount_ils": 900}, "time": {"minutes": 45},
             "tasks": {"title": "Retitled"}, "expenses": {"category": "Tools"},
             "leads": {"name": "Renamed lead"}}
    ids = {"clients": "PER_c1", "works": "WOR_w1", "payments": "PAY_p1", "time": "TIM_t1",
           "tasks": "TAS_t1", "expenses": "EXP_e1", "leads": "LEA_l1"}
    for entity, changes in edits.items():
        lane.write([update(lane.get(entity, ids[entity]), **changes)])
        assert lane.get(entity, ids[entity])["revision"] == 2
