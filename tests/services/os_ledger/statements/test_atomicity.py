"""V2-26 all-or-nothing, concurrency and the edges of an approval (contract §12.2, §5.3, §20).

The approval writes the expense ops, the statement and line rows, the confirmed rules, the purge
and the receipt in ONE ``LedgerTx``. These tests break the transaction at each statement-table
write, on both ledgers, and prove nothing of it survives.
"""

from __future__ import annotations

import json
import threading

import pytest
from app.services.os_ledger import api
from app.services.os_ledger.statement_tables import MemoryTables, PgTables

from tests.services.os_ledger.statements.support import (
    PEPPER,
    batch,
    encode,
    line,
    three_lines,
)

WEEK = 7 * 24 * 3600


def refused(code: str):
    return pytest.raises(api.LedgerError, match=f"^{code}$")


def table_class(world):
    return MemoryTables if world.kind == "fake" else PgTables


def ready_with_rule(world):
    """A batch whose approval writes an expense, line rows, a rule and a purge."""
    submitted = world.paste(batch(three_lines()))
    key = next(i["line_key"] for i in world.lines(submitted.proposal_id)
               if i["merchant_display"] == "DEMO SHOP")
    world.draft(submitted.proposal_id, [{"line_key": key, "tag": "business", "category": "Tools",
                                         "make_rule": True}])
    return submitted, world.review(submitted.proposal_id)


@pytest.mark.parametrize("method", ["insert_line", "insert_rule", "update_statement",
                                    "purge_batch_lines", "put_draft"])
def test_a_failure_while_writing_the_statement_rolls_everything_back(world, monkeypatch, method):
    submitted, review = ready_with_rule(world)
    before = (world.version(), world.everything())

    def boom(self, *args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(table_class(world), method, boom)
    with refused("ledger_unavailable"):
        world.service.approve(world.owner, submitted.proposal_id, review["approval_request_id"],
                              review["review_digest"])
    monkeypatch.undo()
    assert (world.version(), world.everything()) == before
    assert world.review(submitted.proposal_id)["status"] == "pending"
    assert world.rows("os_receipts") == [] and world.expenses() == []
    # and the same approval still goes through afterwards
    result = world.service.approve(world.owner, submitted.proposal_id,
                                   review["approval_request_id"], review["review_digest"])
    assert result.receipt.status == "committed" and len(world.expenses()) == 1


def test_a_failure_while_submitting_leaves_no_proposal(world, monkeypatch):
    def boom(self, *args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(table_class(world), "insert_batch_lines", boom)
    with refused("ledger_unavailable"):
        world.paste(batch(three_lines()))
    monkeypatch.undo()
    assert world.rows("os_proposals") == [] and not any(world.tables().values())


def test_two_concurrent_approvals_commit_once(world):
    submitted = world.paste(batch(three_lines()))
    review = world.review(submitted.proposal_id)
    results, errors = [], []

    def approve():
        try:
            results.append(world.service.approve(
                world.owner, submitted.proposal_id, review["approval_request_id"],
                review["review_digest"]))
        except Exception as exc:  # pragma: no cover - reported by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=approve) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors and len(results) == 4
    assert {r.receipt.request_id for r in results} == {review["approval_request_id"]}
    assert len(world.rows("os_receipts")) == 1 and len(world.rows("os_events")) == 1
    assert len(world.rows("os_outbox")) == 1 and len(world.expenses()) == 1


def test_a_concurrent_approve_and_reject_decide_exactly_once(world):
    submitted = world.paste(batch(three_lines()))
    review = world.review(submitted.proposal_id)
    outcomes = []

    def run(call):
        try:
            outcomes.append(call().receipt.status)
        except api.LedgerError as exc:
            outcomes.append(exc.code)

    calls = [
        lambda: world.service.approve(world.owner, submitted.proposal_id,
                                      review["approval_request_id"], review["review_digest"]),
        lambda: world.service.reject(world.owner, submitted.proposal_id,
                                     review["rejection_request_id"]),
    ]
    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert len(world.rows("os_receipts")) == 1
    assert sorted(outcomes) in (["already_decided", "committed"], ["already_decided", "rejected"])


def test_an_expense_key_collision_at_apply_records_target_changed_and_purges(world):
    submitted = world.paste(batch([line("DEMO SHOP", 10000)]))
    review = world.review(submitted.proposal_id)
    key = world.lines(submitted.proposal_id)[0]["line_key"]
    # A hand entry already holds the key the statement would book under.
    world.hand_expense("EXP_squatter", vendor="Other", amount=5, day="2026-09-01",
                       source_key=f"stmt:{review['statement_id']}:{key}")
    result = world.service.approve(world.owner, submitted.proposal_id,
                                   review["approval_request_id"], review["review_digest"])
    assert result.receipt.status == "target_changed"
    assert result.receipt.request_id == review["approval_request_id"]
    assert [e["expense_id"] for e in world.expenses()] == ["EXP_squatter"]
    assert all(r.detail is None for r in world.tables()["batch_lines"].values())
    assert world.tables()["lines"] == {}
    assert world.review(submitted.proposal_id)["status"] == "target_changed"


def test_a_300_line_batch_in_compact_utf8_fits_the_cap_and_round_trips(world):
    lines = [line(f"ספק בע\"מ מספר {n:03d} - שירותי תוכנה ופרסום"[:80], 1000 + n,
                  suggested_category="תוכנה ושירותים") for n in range(300)]
    body = batch(lines)
    compact = encode(body)
    assert len(compact) <= 128 * 1024 < len(json.dumps(body).encode())  # E4.4: why it matters
    submitted = world.paste(compact)
    assert submitted.summary["line_count"] == 300
    review = world.review(submitted.proposal_id)
    assert review["line_count"] == 300 and review["blocking"] == []
    assert len(world.lines(submitted.proposal_id)) == 300
    result = world.approve(submitted.proposal_id)
    assert result.receipt.status == "committed" and len(result.receipt.records) == 300
    assert len(world.expenses()) == 300
    assert world.tables()["statements"][submitted.statement_id].line_count == 300


def test_a_statement_never_stores_the_pepper(world):
    submitted = world.paste(batch(three_lines()))
    world.approve(submitted.proposal_id)
    assert PEPPER.decode("ascii") not in world.everything()
