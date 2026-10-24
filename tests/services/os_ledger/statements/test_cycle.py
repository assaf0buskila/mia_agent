"""V2-26 one statement per account per cycle (contract §22 E31, §12.4).

The ledger's key is (account, charge_date), but a cycle holds one statement: a second charge date in
the same cycle would book the same charges twice. The batch of that second statement is blocked
(``cycle_has_statement``), so approve stays disabled and refuses, and the review says why.
"""

from __future__ import annotations

import pytest
from app.domain.statements import BLOCK_CYCLE_HAS_STATEMENT
from app.services.os_ledger import api

from tests.services.os_ledger.statements.support import batch, line, three_lines

WEEK = 7 * 24 * 3600
BLOCKED = ["cycle_has_statement"]


def refused(code: str):
    return pytest.raises(api.LedgerError, match=f"^{code}$")


def shop_lines():
    return [line("DEMO SHOP", 10000, day="2026-09-20"), line("OTHER SHOP", 2500, day="2026-09-21")]


def paste(world, number, charge_date, *, label="Synthetic Business Card", lines=None):
    return world.paste(batch(lines or shop_lines(), number=number, charge_date=charge_date,
                             label=label))


def test_the_code_is_the_contract_name():
    assert BLOCK_CYCLE_HAS_STATEMENT == "cycle_has_statement"


def test_a_second_statement_in_an_imported_cycle_is_blocked_and_refused(world):
    first = paste(world, 1, "2026-10-15")
    world.approve(first.proposal_id)
    second = paste(world, 2, "2026-10-16")
    assert second.statement_id != first.statement_id and second.conflict is False
    assert world.draft(second.proposal_id)["blocking"] == BLOCKED
    review = world.review(second.proposal_id)  # after the draft, so the digest is the current one
    assert review["blocking"] == BLOCKED and review["status"] == "pending"
    before, expenses = world.everything(), world.expenses()
    assert len(expenses) == 2
    with refused("cycle_has_statement"):
        world.service.approve(world.owner, second.proposal_id, review["approval_request_id"],
                              review["review_digest"])
    assert world.everything() == before and world.expenses() == expenses


def test_two_pending_statements_in_one_cycle_block_each_other_until_one_is_rejected(world):
    """The reviewer's repro: the same two lines under two charge dates of one cycle."""
    first = paste(world, 1, "2026-10-15")
    second = paste(world, 2, "2026-10-16")
    assert world.review(first.proposal_id)["blocking"] == BLOCKED
    assert world.review(second.proposal_id)["blocking"] == BLOCKED
    before = world.everything()
    for pid in (first.proposal_id, second.proposal_id):
        review = world.review(pid)
        with refused("cycle_has_statement"):
            world.service.approve(world.owner, pid, review["approval_request_id"],
                                  review["review_digest"])
    assert world.everything() == before and world.expenses() == []
    world.reject(first.proposal_id)
    assert world.review(second.proposal_id)["blocking"] == []
    world.approve(second.proposal_id)
    assert len(world.expenses()) == 2  # booked once, not twice


def test_the_blocking_reason_is_part_of_the_digest(world):
    """Approving what was reviewed: once the cycle is taken, the old digest is stale."""
    first = paste(world, 1, "2026-10-15")
    second = paste(world, 2, "2026-10-16")
    world.reject(first.proposal_id)
    clear = world.review(second.proposal_id)
    assert clear["blocking"] == []
    third = paste(world, 3, "2026-10-17")
    assert world.review(third.proposal_id)["blocking"] == BLOCKED
    taken = world.review(second.proposal_id)
    assert taken["blocking"] == BLOCKED and taken["review_digest"] != clear["review_digest"]
    with refused("review_stale"):
        world.service.approve(world.owner, second.proposal_id, clear["approval_request_id"],
                              clear["review_digest"])


def test_a_decided_or_overdue_statement_does_not_hold_the_cycle(world):
    rejected = paste(world, 1, "2026-10-15")
    world.reject(rejected.proposal_id)
    overdue = paste(world, 2, "2026-10-16")
    world.advance(WEEK + 1)  # the batch is past its window, expiry has not run yet
    later = paste(world, 3, "2026-10-17")
    assert world.review(later.proposal_id)["blocking"] == []
    world.service.expire_due(world.worker)
    assert world.review(overdue.proposal_id)["status"] == "expired"
    assert world.review(later.proposal_id)["blocking"] == []
    world.approve(later.proposal_id)


def test_another_account_another_cycle_and_a_reimport_do_not_collide(world):
    first = paste(world, 1, "2026-10-15")
    world.approve(first.proposal_id)
    other_account = paste(world, 2, "2026-10-16", label="Second Card")
    next_cycle = paste(world, 3, "2026-11-15")
    reimport = paste(world, 4, "2026-10-15", lines=three_lines())
    for pid in (other_account.proposal_id, next_cycle.proposal_id, reimport.proposal_id):
        assert world.review(pid)["blocking"] == []
