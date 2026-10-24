"""V2-26 expiry, credential revocation and the pepper pin on every write (contract §12.2, §5.3)."""

from __future__ import annotations

import pytest
from app.services.os_ledger import api, authorize
from app.services.os_ledger.statements import pepper_fingerprint

from tests.services.os_ledger.statements.support import (
    CLAUDE,
    CRED,
    CRED_B,
    GPT,
    batch,
    three_lines,
)

WEEK = 7 * 24 * 3600


def refused(code: str):
    return pytest.raises(api.LedgerError, match=f"^{code}$")


def statuses(world):
    return {p["proposal_id"]: p["status"] for p in world.rows("os_proposals")}


def purged(world, proposal_id):
    rows = [r for k, r in world.tables()["batch_lines"].items() if k[0] == proposal_id]
    return bool(rows) and all(r.detail is None and r.purged_at is not None for r in rows)


# ---- expire_due ---------------------------------------------------------------------------------


def test_expire_due_finishes_overdue_batches_and_purges_their_detail(world):
    old = world.paste(batch(three_lines(), number=1))
    world.advance(WEEK - 10)
    fresh = world.paste(batch(three_lines(), number=2, charge_date="2026-11-15"))
    world.advance(20)
    version = world.version()
    assert world.service.expire_due(world.worker) == 1
    assert statuses(world) == {old.proposal_id: "expired", fresh.proposal_id: "pending"}
    receipt = next(r for r in world.rows("os_receipts") if r["proposal_id"] == old.proposal_id)
    proposal = next(p for p in world.rows("os_proposals") if p["proposal_id"] == old.proposal_id)
    assert (receipt["status"], receipt["request_id"], receipt["reason_codes"]) == (
        "expired", proposal["rejection_id"], ())
    assert receipt["kind"] == "statement_batch" and world.version() == version + 1
    assert purged(world, old.proposal_id) and not purged(world, fresh.proposal_id)
    assert world.tables()["statements"][old.statement_id].current_proposal_id is None
    assert world.expenses() == [] and world.rows("os_outbox") == []
    assert world.service.expire_due(world.worker) == 0  # a second run finds nothing


def test_a_pending_batch_with_no_registry_row_still_expires_and_purges(world):
    """The registry is an index, not the authority: a batch that holds detail is found by it."""
    old = world.paste(batch(three_lines(), number=1))
    fresh = world.paste(batch(three_lines(), number=2, charge_date="2026-11-15"))
    world.drop_drafts()
    world.advance(WEEK + 1)
    assert world.service.expire_due(world.worker) == 2
    assert statuses(world) == {old.proposal_id: "expired", fresh.proposal_id: "expired"}
    assert purged(world, old.proposal_id) and purged(world, fresh.proposal_id)
    assert sorted(r["status"] for r in world.rows("os_receipts")) == ["expired", "expired"]
    assert world.service.expire_due(world.worker) == 0


def test_a_current_batch_with_no_registry_row_is_left_alone(world):
    submitted = world.paste(batch(three_lines()))
    world.drop_drafts()
    assert world.service.expire_due(world.worker) == 0
    assert statuses(world) == {submitted.proposal_id: "pending"}


@pytest.mark.parametrize("knob,value", [
    ("kill_switch", True), ("assaf_os_backend", "sheets"), ("assaf_os_ledger_enabled", False),
    ("assaf_os_freeze", True), ("assaf_os_statements_enabled", False)])
def test_expiry_runs_under_every_switch(world, knob, value):
    submitted = world.paste(batch(three_lines()))
    world.advance(WEEK + 1)
    setattr(world.config, knob, value)
    assert world.service.expire_due(world.worker) == 1
    assert statuses(world) == {submitted.proposal_id: "expired"}
    assert purged(world, submitted.proposal_id)


def test_expiry_runs_while_the_statements_domain_is_fenced(world):
    submitted = world.paste(batch(three_lines()))
    world.fence_statements()
    world.advance(WEEK + 1)
    assert world.service.expire_due(world.worker) == 1
    assert statuses(world) == {submitted.proposal_id: "expired"}


def test_a_batch_without_lines_finishes_as_restored_without_detail(world):
    submitted = world.paste(batch(three_lines()))
    world.drop_batch_lines()  # a restore excludes the lines
    assert world.service.expire_due(world.worker) == 1
    receipt = world.rows("os_receipts")[0]
    assert (receipt["status"], receipt["reason_codes"]) == ("expired", ("restored_without_detail",))
    assert statuses(world) == {submitted.proposal_id: "expired"}


def test_a_batch_from_an_older_epoch_expires_with_epoch_changed(world):
    submitted = world.paste(batch(three_lines()))
    world.bump_epoch()
    ctx = authorize.for_worker("retention", world.config)  # minted at the bumped epoch, 2
    assert world.service.expire_due(ctx) == 1
    receipt = world.rows("os_receipts")[0]
    assert (receipt["status"], receipt["reason_codes"], receipt["epoch"]) == (
        "expired", ("epoch_changed",), 1)
    assert purged(world, submitted.proposal_id)


def test_a_current_batch_with_lines_is_left_alone(world):
    submitted = world.paste(batch(three_lines()))
    assert world.service.expire_due(world.worker) == 0
    assert statuses(world) == {submitted.proposal_id: "pending"}


def test_expire_due_only_accepts_the_retention_worker(world):
    world.paste(batch(three_lines()))
    world.advance(WEEK + 1)
    for ctx in (world.owner, world.reader, world.ctx("MIA"),
                authorize.for_worker("mirror", world.config)):
        with pytest.raises(api.LedgerError) as caught:
            world.service.expire_due(ctx)
        assert caught.value.code in {"channel_operation_denied", "scope_denied"}
    assert statuses(world) and set(statuses(world).values()) == {"pending"}


def test_expiry_leaves_the_credential_of_a_batch_untouched(world):
    world.credential()
    submitted = world.paste(batch(three_lines()), ctx=world.assistant(), source=CLAUDE)
    world.advance(WEEK + 1)
    world.service.expire_due(world.worker)
    assert statuses(world) == {submitted.proposal_id: "expired"}
    assert world.tables()["credentials"][CRED].revoked_at is None


def test_the_current_pointer_moves_to_the_remaining_pending_batch(world):
    world.credential()
    mine = world.paste(batch(three_lines(), number=1))
    theirs = world.paste(batch(three_lines(), number=2), ctx=world.assistant(), source=CLAUDE)
    assert world.tables()["statements"][mine.statement_id].current_proposal_id == theirs.proposal_id
    world.reject(theirs.proposal_id)
    assert world.tables()["statements"][mine.statement_id].current_proposal_id == mine.proposal_id


# ---- revoke_pending_for_credential --------------------------------------------------------------


def test_revoke_rejects_exactly_that_keys_pending_batches_inside_the_callers_transaction(world):
    world.credential(CRED)
    world.credential(CRED_B, label="GPT")
    a = world.paste(batch(three_lines(), number=1), ctx=world.assistant(), source=CLAUDE)
    b = world.paste(batch(three_lines(), number=2, charge_date="2026-11-15"),
                    ctx=world.assistant("gpt", CRED_B), source=GPT)
    mine = world.paste(batch(three_lines(), number=3, charge_date="2026-12-15"))
    version = world.version()
    with world.ledger.transaction(world.owner, domain="statements",
                                  kind="statement_batch") as tx:
        # a nested transaction would trip the fake's non-reentrant lock: it must use `tx`
        assert world.service.revoke_pending_for_credential(tx, CRED) == 1
    assert statuses(world) == {a.proposal_id: "rejected", b.proposal_id: "pending",
                               mine.proposal_id: "pending"}
    receipt = next(r for r in world.rows("os_receipts") if r["proposal_id"] == a.proposal_id)
    assert (receipt["status"], receipt["reason_codes"]) == ("rejected", ("credential_revoked",))
    assert receipt["request_id"] == next(
        p["rejection_id"] for p in world.rows("os_proposals") if p["proposal_id"] == a.proposal_id)
    assert purged(world, a.proposal_id) and not purged(world, b.proposal_id)
    assert world.version() == version + 1


def test_revoke_rolls_back_with_the_callers_transaction(world):
    world.credential()
    a = world.paste(batch(three_lines()), ctx=world.assistant(), source=CLAUDE)
    with refused("ledger_unavailable"):  # the ledger maps a failure it did not raise itself
        with world.ledger.transaction(world.owner, domain="statements",
                                      kind="statement_batch") as tx:
            world.service.revoke_pending_for_credential(tx, CRED)
            raise RuntimeError("caller failed after revoking")
    assert statuses(world) == {a.proposal_id: "pending"} and not purged(world, a.proposal_id)


def test_revoke_with_nothing_pending_returns_zero(world):
    with world.ledger.transaction(world.owner, domain="statements",
                                  kind="statement_batch") as tx:
        assert world.service.revoke_pending_for_credential(tx, CRED) == 0


# ---- the pepper pin on every write ---------------------------------------------------------------


@pytest.mark.parametrize("how", ["unpinned", "mismatch"])
def test_every_statement_write_refuses_without_a_matching_pin(world, how):
    submitted = world.paste(batch(three_lines()))
    review = world.review(submitted.proposal_id)
    key = world.lines(submitted.proposal_id)[0]["line_key"]
    world.advance(WEEK + 1)
    world.set_pin(None if how == "unpinned"
                  else pepper_fingerprint(b"another-pepper-0123456789abcdef"))
    before = (world.version(), world.everything())
    calls = [
        lambda: world.paste(batch(three_lines(), number=2)),
        lambda: world.service.save_draft(world.owner, submitted.proposal_id, 0,
                                         [{"line_key": key, "tag": "personal"}], {}),
        lambda: world.service.approve(world.owner, submitted.proposal_id,
                                      review["approval_request_id"], review["review_digest"]),
        lambda: world.service.reject(world.owner, submitted.proposal_id,
                                     review["rejection_request_id"]),
        lambda: world.service.expire_due(world.worker),
    ]
    for call in calls:
        with refused("ledger_unavailable"):
            call()
    assert (world.version(), world.everything()) == before


def test_the_fingerprint_is_the_documented_hmac():
    import hashlib
    import hmac
    pepper = b"synthetic-test-pepper-0123456789abcdef"
    expected = hmac.new(pepper, b"assaf-os-statement-pepper-fingerprint.v1",
                        hashlib.sha256).hexdigest()
    assert pepper_fingerprint(pepper) == expected and len(expected) == 64
