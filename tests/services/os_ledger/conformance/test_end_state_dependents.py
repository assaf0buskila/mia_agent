"""A write that lands a record in its FORK-DELETE end state meets ``has_dependents``, on every kind
(§22 E20).

The rule lives in the shared ledger ``apply``, so it holds whatever the kind (``proposal``,
``owner_direct`` or ``import``) and whoever built the mutation. V2-24's own pre-check stays as
defense in depth. One case per entity that has dependents (clients, works, payments) and per kind.

* "Lands" means a transition INTO the end state: an update whose ``after`` is in it and whose
  ``before`` was not (a tombstone counts, as the task end state and a delete everywhere else). A
  create is not an update (a first import of real data may hold a cancelled work with live
  payments), and an update that stays in the end state is not a new landing.
* Dependents are judged after the whole batch, so a batch that cancels a work together with its
  payments is fine in either order.
"""

import pytest
from app.services.os_ledger import api

from tests.fakes.os_ledger_builders import (
    CLIENT_INACTIVE,
    PAYMENT_CANCELLED,
    PAYMENT_REFUND,
    TIME_CANCELLED,
    WORK_CANCELLED,
    WRITE_KINDS,
    WriteLane,
    client,
    ctx,
    payment,
    rid,
    snapshot,
    task,
    time_entry,
    tombstone,
    update,
    work,
)

ENTITIES = ("clients", "works", "payments")
RECORD = {"clients": "PER_c1", "works": "WOR_w1", "payments": "PAY_p1"}
# What each entity needs to exist, what depends on it, and the patch that lands it in its end state.
SEED = {
    "clients": lambda: [client()],
    "works": lambda: [client(), work()],
    "payments": lambda: [client(), work(), payment()],
}
BLOCKERS = {
    "clients": lambda: [work()],
    "works": lambda: [payment(), time_entry()],
    "payments": lambda: [payment("PAY_r1", original_payment_id="PAY_p1", kind=PAYMENT_REFUND,
                                 actual_amount_ils=100)],
}
END_PATCH = {
    "clients": {"status": CLIENT_INACTIVE},
    "works": {"status": WORK_CANCELLED},
    "payments": {"kind": PAYMENT_CANCELLED},
}
FOUND = {
    "clients": [("works", "WOR_w1")],
    "works": [("payments", "PAY_p1"), ("time", "TIM_t1")],
    "payments": [("payments", "PAY_r1")],
}


def clear_blockers(lane, entity):
    """Mutations that cancel what depends on ``entity``, so nothing live is left."""
    return {
        "clients": lambda: [update(lane.get("works", "WOR_w1"), status=WORK_CANCELLED)],
        "works": lambda: [update(lane.get("payments", "PAY_p1"), kind=PAYMENT_CANCELLED),
                          update(lane.get("time", "TIM_t1"), status=TIME_CANCELLED)],
        "payments": lambda: [update(lane.get("payments", "PAY_r1"), kind=PAYMENT_CANCELLED,
                                    original_payment_id=None)],
    }[entity]()


def refs(pairs):
    return [{"entity": entity, "record_id": record_id} for entity, record_id in pairs]


def has_dependents(call):
    with pytest.raises(api.LedgerError) as caught:
        call()
    assert caught.value.code == "has_dependents"
    assert (caught.value.status, caught.value.write_outcome) == (409, "not_started")
    return caught.value


def arranged(ledger, kind, entity):
    lane = WriteLane(ledger, kind)
    lane.write(SEED[entity]())
    lane.write(BLOCKERS[entity]())
    return lane


@pytest.mark.parametrize("kind", WRITE_KINDS)
@pytest.mark.parametrize("entity", ENTITIES)
def test_an_update_into_the_end_state_with_live_dependents_is_has_dependents(ledger, kind, entity):
    lane = arranged(ledger, kind, entity)
    before = snapshot(ledger)
    error = has_dependents(lambda: lane.attempt(
        [update(lane.get(entity, RECORD[entity]), **END_PATCH[entity])]))
    assert dict(error.details) == {"dependents": refs(FOUND[entity])}
    assert snapshot(ledger) == before  # nothing written: no row, receipt, event or version bump
    column, value = api.FORK_DELETE_END_STATE[entity]
    assert lane.get(entity, RECORD[entity])[column] != value


@pytest.mark.parametrize("entity", ENTITIES)
def test_the_same_update_commits_once_nothing_live_depends_on_the_record(ledger, entity):
    lane = arranged(ledger, "owner_direct", entity)
    has_dependents(lambda: lane.attempt(
        [update(lane.get(entity, RECORD[entity]), **END_PATCH[entity])]))
    lane.write(clear_blockers(lane, entity))  # cancelled and done referrers never block
    lane.write([update(lane.get(entity, RECORD[entity]), **END_PATCH[entity])])
    column, value = api.FORK_DELETE_END_STATE[entity]
    assert lane.get(entity, RECORD[entity])[column] == value


@pytest.mark.parametrize("entity", ("clients", "works"))
def test_an_approved_proposal_that_gained_a_dependent_since_it_was_made_is_refused(
        ledger, entity):
    """The proposal's dependency digest covers the record and what it references, not its
    referrers, so a work added after the proposal reaches ``apply`` and meets the rule there.
    §22 E26: decide then finishes the proposal ``target_changed`` (reason ``has_dependents``)
    instead of raising and leaving it pending forever; nothing is applied, and once the blockers
    are gone a NEW proposal commits (test_decide_apply_refusals.py pins the receipt itself)."""
    lane = WriteLane(ledger, "owner_direct")
    lane.write(SEED[entity]())
    record = lane.get(entity, RECORD[entity])
    proposal = ledger.propose(ctx(), api.ProposeInput(
        rid(), entity, RECORD[entity], record["revision"], END_PATCH[entity]))
    lane.write(BLOCKERS[entity]())
    result = ledger.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    assert (result.receipt.status, result.receipt.reason_codes) == (
        "target_changed", ("has_dependents",))
    assert lane.get(entity, RECORD[entity]) == record
    assert {p["proposal_id"]: p["status"] for p in ledger.rows("os_proposals")}[
        proposal.proposal_id] == "target_changed"
    lane.write(clear_blockers(lane, entity))
    again = ledger.propose(ctx(), api.ProposeInput(
        rid(), entity, RECORD[entity], record["revision"], END_PATCH[entity]))
    result = ledger.decide(ctx(), again.proposal_id, again.approval_id, "approve")
    assert result.receipt.status == "committed"
    column, value = api.FORK_DELETE_END_STATE[entity]
    assert lane.get(entity, RECORD[entity])[column] == value


def test_a_tombstone_of_a_record_with_live_dependents_is_has_dependents(ledger):
    lane = arranged(ledger, "owner_direct", "clients")
    before = snapshot(ledger)
    error = has_dependents(lambda: lane.attempt(
        [tombstone("clients", lane.get("clients", "PER_c1"))]))
    assert dict(error.details) == {"dependents": refs(FOUND["clients"])}
    assert snapshot(ledger) == before


def test_a_record_that_is_already_in_its_end_state_or_is_created_there_is_not_a_new_landing(ledger):
    lane = WriteLane(ledger, "owner_direct")
    # A create into the end state (an imported cancelled client) with a live work: no update.
    lane.write([client(status=CLIENT_INACTIVE), work()])
    # And an update that keeps it there is not a transition into the end state either.
    lane.write([update(lane.get("clients", "PER_c1"), notes="still inactive")])
    assert lane.get("clients", "PER_c1")["status"] == CLIENT_INACTIVE
    assert lane.get("clients", "PER_c1")["revision"] == 2


@pytest.mark.parametrize("reverse", (False, True))
def test_the_end_state_is_judged_after_the_whole_batch_so_the_order_does_not_matter(
        ledger, reverse):
    lane = arranged(ledger, "owner_direct", "works")
    has_dependents(lambda: lane.attempt(
        [update(lane.get("works", "WOR_w1"), status=WORK_CANCELLED)]))
    mutations = [update(lane.get("works", "WOR_w1"), status=WORK_CANCELLED),
                 *clear_blockers(lane, "works")]
    lane.write(mutations[::-1] if reverse else mutations)
    assert lane.get("works", "WOR_w1")["status"] == WORK_CANCELLED
    assert lane.get("payments", "PAY_p1")["kind"] == PAYMENT_CANCELLED
    assert lane.get("time", "TIM_t1")["status"] == TIME_CANCELLED


@pytest.mark.parametrize("reverse", (False, True))
def test_a_live_record_created_in_the_same_batch_against_the_record_still_blocks(ledger, reverse):
    lane = WriteLane(ledger, "owner_direct")
    lane.write(SEED["works"]())
    mutations = [update(lane.get("works", "WOR_w1"), status=WORK_CANCELLED), payment("PAY_new")]
    before = snapshot(ledger)
    error = has_dependents(lambda: lane.attempt(mutations[::-1] if reverse else mutations))
    assert dict(error.details) == {"dependents": refs([("payments", "PAY_new")])}
    assert snapshot(ledger) == before


def test_has_dependents_lists_at_most_twenty_refs_in_a_stable_order(ledger):
    lane = WriteLane(ledger, "owner_direct")
    lane.write([client()] + [task(f"TAS_k{i:02d}", person_id="PER_c1") for i in range(25)])
    error = has_dependents(lambda: lane.attempt(
        [update(lane.get("clients", "PER_c1"), status=CLIENT_INACTIVE)]))
    listed = error.details["dependents"]
    assert listed == refs(("tasks", f"TAS_k{i:02d}") for i in range(20))


def test_an_update_that_does_not_land_in_the_end_state_is_unaffected(ledger):
    lane = arranged(ledger, "owner_direct", "clients")
    lane.write([update(lane.get("clients", "PER_c1"), name="Renamed")])
    assert lane.get("clients", "PER_c1")["name"] == "Renamed"
