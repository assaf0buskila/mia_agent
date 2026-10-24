"""V2-26 approve and reject (contract §12.2, §5.3, §20 V2-26): all-or-nothing, digest-bound."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from app.domain.statements import LINKED_EXPENSE_CHANGED
from app.services.os_ledger import api
from app.services.os_ledger.statement_tables import RuleRow

from tests.services.os_ledger.statements.support import (
    CLAUDE,
    CRED,
    HEX64,
    batch,
    line,
    three_lines,
)

STM_KEY = re.compile(r"stmt:STM_[0-9a-f]{32}:[0-9a-f]{64}")


def refused(code: str):
    return pytest.raises(api.LedgerError, match=f"^{code}$")


def row_of(world, pid, merchant):
    for item in world.lines(pid):
        if item["merchant_display"] == merchant:
            return item
    raise AssertionError(merchant)


def stored(world, statement_id):
    return {key[1]: row for key, row in world.tables()["lines"].items() if key[0] == statement_id}


def test_approve_commits_expenses_lines_statement_and_purge_in_one_transaction(world):
    submitted = world.paste(batch(three_lines()))
    version = world.version()
    result = world.approve(submitted.proposal_id)
    receipt = result.receipt
    assert result.found and receipt.status == "committed" and receipt.kind == "statement_batch"
    proposal = next(p for p in world.rows("os_proposals")
                    if p["proposal_id"] == submitted.proposal_id)
    assert receipt.request_id == proposal["approval_id"] and proposal["status"] == "committed"
    assert receipt.commit_version == version + 1 == world.version()
    assert receipt.reason_codes == () and receipt.undo_until is None and receipt.undo_of is None
    assert result.undo.state == "none" and result.observed_version == receipt.commit_version

    created = world.expenses()
    assert len(created) == 1
    expense = created[0]
    assert STM_KEY.fullmatch(expense["source_key"]) and expense["revision"] == 1
    assert (expense["vendor"], expense["amount_ils"], expense["date"]) == (
        "DEMO SHOP", 10000, "2026-10-15")
    assert (expense["category"], expense["payment_status"]) == ("כלי AI", "שולם")
    assert [r["record_id"] for r in receipt.records] == [expense["expense_id"]]
    assert [(e, r["expense_id"]) for e, r in result.records] == [("expenses",
                                                                  expense["expense_id"])]

    rows = stored(world, submitted.statement_id)
    assert len(rows) == 3
    shop = next(r for r in rows.values() if r.merchant_display == "DEMO SHOP")
    assert (shop.tag, shop.state, shop.expense_id, shop.expense_revision_at_import,
            shop.revision) == ("business", "active", expense["expense_id"], 1, 1)
    credit = next(r for r in rows.values() if r.merchant_display == "BIZ REFUND")
    assert credit.kind == "credit" and credit.expense_id is None
    personal = next(r for r in rows.values() if r.tag == "personal")
    assert [v for k, v in vars(personal).items()
            if k not in {"statement_id", "line_key", "tag", "state", "revision"}
            and v is not None] == []

    statement = world.tables()["statements"][submitted.statement_id]
    assert (statement.line_count, statement.approved_proposal_id) == (3, submitted.proposal_id)
    assert statement.imported_version == receipt.commit_version
    assert statement.current_proposal_id is None and HEX64.fullmatch(statement.lines_digest)

    kept = world.tables()["batch_lines"]
    assert len(kept) == 3 and all(r.detail is None and r.purged_at is not None
                                  for r in kept.values())
    outbox = world.rows("os_outbox")
    assert len(outbox) == 1 and outbox[0]["commit_version"] == receipt.commit_version
    assert [e["body"]["status"] for e in world.rows("os_events")] == ["committed"]


def test_a_stale_digest_is_review_stale_with_no_receipt_and_no_write(world):
    submitted = world.paste(batch(three_lines()))
    review = world.review(submitted.proposal_id)
    key = world.lines(submitted.proposal_id)[0]["line_key"]
    world.draft(submitted.proposal_id, [{"line_key": key, "category": "Tools"}])
    before = (world.version(), world.everything())
    with refused("review_stale"):
        world.service.approve(world.owner, submitted.proposal_id,
                              review["approval_request_id"], review["review_digest"])
    assert (world.version(), world.everything()) == before
    assert world.rows("os_receipts") == []


def test_a_hand_entry_change_after_the_review_makes_it_stale(world):
    world.hand_expense("EXP_h1", vendor="Demo Shop", amount=10000, day="2026-09-20")
    submitted = world.paste(batch([line("DEMO SHOP", 10000)]))
    review = world.review(submitted.proposal_id)
    world.edit_expense("EXP_h1", payment_status="בוטל")  # the candidate disappears
    with refused("review_stale"):
        world.service.approve(world.owner, submitted.proposal_id,
                              review["approval_request_id"], review["review_digest"])


def test_unassigned_lines_block_the_approval(world):
    submitted = world.paste(batch([line("MYSTERY", 100, tag=None), line("SHOP", 200)]))
    review = world.review(submitted.proposal_id)
    before = world.everything()
    with refused("unassigned_lines"):
        world.service.approve(world.owner, submitted.proposal_id,
                              review["approval_request_id"], review["review_digest"])
    assert world.everything() == before


def test_a_merge_choice_blocks_until_the_owner_decides(world):
    world.hand_expense("EXP_a", vendor="Coffee Cart", amount=1800, day="2026-09-19")
    world.hand_expense("EXP_b", vendor="coffee cart", amount=1800, day="2026-09-20")
    submitted = world.paste(batch([line("COFFEE CART", 1800, day="2026-09-20")]))
    review = world.review(submitted.proposal_id)
    with refused("merge_choice_required"):
        world.service.approve(world.owner, submitted.proposal_id,
                              review["approval_request_id"], review["review_digest"])
    key = world.lines(submitted.proposal_id)[0]["line_key"]
    world.draft(submitted.proposal_id, [], {"merge": {key: "EXP_b"}})
    result = world.approve(submitted.proposal_id)
    assert result.receipt.status == "committed" and world.expenses() == []  # linked, not booked
    row = stored(world, submitted.statement_id)[key]
    assert row.matched_expense_id == "EXP_b" and row.expense_id is None
    assert world.expense_record("EXP_b")["revision"] == 1  # the hand entry is never modified


def test_choosing_none_books_the_charge_on_its_own(world):
    world.hand_expense("EXP_a", vendor="Coffee Cart", amount=1800, day="2026-09-19")
    world.hand_expense("EXP_b", vendor="coffee cart", amount=1800, day="2026-09-20")
    submitted = world.paste(batch([line("COFFEE CART", 1800, day="2026-09-20")]))
    key = world.lines(submitted.proposal_id)[0]["line_key"]
    world.draft(submitted.proposal_id, [], {"merge": {key: None}})
    world.approve(submitted.proposal_id)
    assert len(world.expenses()) == 1
    assert stored(world, submitted.statement_id)[key].matched_expense_id is None


def test_the_decision_ids_replay_and_cannot_be_mixed(world):
    submitted = world.paste(batch(three_lines()))
    review = world.review(submitted.proposal_id)
    first = world.service.approve(world.owner, submitted.proposal_id,
                                  review["approval_request_id"], review["review_digest"])
    state = (world.version(), world.everything())
    again = world.service.approve(world.owner, submitted.proposal_id,
                                  review["approval_request_id"], review["review_digest"])
    assert again.receipt == first.receipt and again.records == first.records
    assert (world.version(), world.everything()) == state
    with refused("decision_request_mismatch"):
        world.service.approve(world.owner, submitted.proposal_id,
                              review["rejection_request_id"], review["review_digest"])
    with refused("already_decided"):
        world.service.reject(world.owner, submitted.proposal_id, review["rejection_request_id"])
    with refused("decision_request_mismatch"):
        world.service.reject(world.owner, submitted.proposal_id, review["approval_request_id"])
    with refused("invalid_request_id"):
        world.service.approve(world.owner, submitted.proposal_id, "not-a-uuid", "0" * 64)
    assert (world.version(), world.everything()) == state


def test_approve_after_reject_is_already_decided(world):
    submitted = world.paste(batch(three_lines()))
    review = world.review(submitted.proposal_id)
    world.service.reject(world.owner, submitted.proposal_id, review["rejection_request_id"])
    with refused("already_decided"):
        world.service.approve(world.owner, submitted.proposal_id,
                              review["approval_request_id"], review["review_digest"])


def test_unknown_batches_cannot_be_decided(world):
    with refused("batch_not_found"):
        world.service.approve(world.owner, "PRO_" + "9" * 32, world.new_id(), "0" * 64)
    with refused("batch_not_found"):
        world.service.reject(world.owner, "PRO_" + "9" * 32, world.new_id())


@pytest.mark.parametrize("channel,code", [
    ("MIA", "channel_operation_denied"), ("TELEGRAM_APPROVAL", "channel_operation_denied"),
    ("ASSISTANT_CLAUDE", "channel_operation_denied"), ("ASSISTANT_GPT", "channel_operation_denied"),
    ("WORKER", "channel_operation_denied")])
def test_only_the_os_session_decides(world, channel, code):
    world.credential()
    submitted = world.paste(batch(three_lines()))
    review = world.review(submitted.proposal_id)
    extra = {"credential_id": CRED} if channel.startswith("ASSISTANT") else {}
    ctx = world.ctx(channel, **extra)
    before = world.everything()
    with refused(code):
        world.service.approve(ctx, submitted.proposal_id, review["approval_request_id"],
                              review["review_digest"])
    with refused(code):
        world.service.reject(ctx, submitted.proposal_id, review["rejection_request_id"])
    assert world.everything() == before


def test_a_decision_needs_the_approve_scope_and_a_verified_mutation(world):
    submitted = world.paste(batch(three_lines()))
    review = world.review(submitted.proposal_id)
    with refused("scope_denied"):
        world.service.approve(world.ctx("OS", scopes={"assaf-os:read"}), submitted.proposal_id,
                              review["approval_request_id"], review["review_digest"])
    with refused("browser_mutation_verification_required"):
        world.service.reject(world.reader, submitted.proposal_id, review["rejection_request_id"])


def test_the_kill_switch_blocks_decisions_and_the_freeze_does_not(world):
    submitted = world.paste(batch(three_lines()))
    review = world.review(submitted.proposal_id)
    world.config.kill_switch = True
    with refused("kill_switch"):
        world.service.approve(world.owner, submitted.proposal_id,
                              review["approval_request_id"], review["review_digest"])
    with refused("kill_switch"):
        world.service.reject(world.owner, submitted.proposal_id, review["rejection_request_id"])
    world.config.kill_switch = False
    world.config.assaf_os_freeze = True  # the drain settles every pending item
    assert world.service.approve(world.owner, submitted.proposal_id,
                                 review["approval_request_id"],
                                 review["review_digest"]).receipt.status == "committed"


@pytest.mark.parametrize("point", ["after_apply", "after_receipt", "before_commit"])
def test_a_failure_at_any_point_leaves_nothing_behind(fake_world, point):
    world = fake_world
    submitted = world.paste(batch(three_lines()))
    review = world.review(submitted.proposal_id)
    before = (world.version(), world.everything())
    world.ledger.inject(point, api.LedgerError("ledger_unavailable"))
    with refused("ledger_unavailable"):
        world.service.approve(world.owner, submitted.proposal_id, review["approval_request_id"],
                              review["review_digest"])
    assert (world.version(), world.everything()) == before
    assert world.review(submitted.proposal_id)["status"] == "pending"
    assert all(r.detail is not None for r in world.tables()["batch_lines"].values())
    # and the same approval still goes through afterwards
    assert world.service.approve(world.owner, submitted.proposal_id,
                                 review["approval_request_id"],
                                 review["review_digest"]).receipt.status == "committed"


def test_an_unknown_commit_recovers_by_replay(fake_world):
    world = fake_world
    submitted = world.paste(batch(three_lines()))
    review = world.review(submitted.proposal_id)
    world.ledger.inject("commit_unknown", RuntimeError("lost"))
    with refused("unknown"):
        world.service.approve(world.owner, submitted.proposal_id, review["approval_request_id"],
                              review["review_digest"])
    again = world.service.approve(world.owner, submitted.proposal_id,
                                  review["approval_request_id"], review["review_digest"])
    assert again.receipt.status == "committed" and len(world.expenses()) == 1


# ---- reject, expiry, revoke, restore ------------------------------------------------------------


def test_reject_finishes_rejected_purges_and_applies_nothing(world):
    submitted = world.paste(batch(three_lines()))
    version = world.version()
    result = world.reject(submitted.proposal_id)
    assert result.receipt.status == "rejected" and result.records == ()
    proposal = world.rows("os_proposals")[0]
    assert result.receipt.request_id == proposal["rejection_id"]
    assert proposal["status"] == "rejected" and world.version() == version + 1
    assert world.expenses() == [] and world.rows("os_outbox") == []
    assert all(r.detail is None for r in world.tables()["batch_lines"].values())
    assert world.tables()["statements"][submitted.statement_id].current_proposal_id is None
    assert world.review(submitted.proposal_id)["status"] == "rejected"
    assert world.lines(submitted.proposal_id) == []


def test_a_late_approve_or_reject_finishes_expired_and_purges(world):
    first = world.paste(batch(three_lines(), number=1))
    second = world.paste(batch(three_lines(), number=2, charge_date="2026-11-15"))
    r1, r2 = world.review(first.proposal_id), world.review(second.proposal_id)
    world.advance(7 * 24 * 3600 + 1)
    late_approve = world.service.approve(world.owner, first.proposal_id,
                                         r1["approval_request_id"], r1["review_digest"])
    assert late_approve.receipt.status == "expired"
    assert late_approve.receipt.request_id == r1["approval_request_id"]
    late_reject = world.service.reject(world.owner, second.proposal_id, r2["rejection_request_id"])
    assert (late_reject.receipt.status, late_reject.receipt.request_id) == (
        "expired", r2["rejection_request_id"])
    assert world.expenses() == [] and world.rows("os_outbox") == []
    assert all(r.detail is None for r in world.tables()["batch_lines"].values())


def test_a_pending_batch_of_an_older_epoch_is_expired_on_a_decision(world):
    submitted = world.paste(batch(three_lines()))
    review = world.review(submitted.proposal_id)
    world.bump_epoch()
    ctx = world.ctx("OS", epoch=2)
    result = world.service.approve(ctx, submitted.proposal_id, review["approval_request_id"],
                                   review["review_digest"])
    assert result.receipt.status == "expired" and result.receipt.reason_codes == ("epoch_changed",)
    assert result.receipt.epoch == 1 and result.receipt.request_id == (
        world.rows("os_proposals")[0]["rejection_id"])
    assert world.expenses() == []


def test_a_revoked_key_can_never_commit(world):
    world.credential()
    submitted = world.paste(batch(three_lines()), ctx=world.assistant(), source=CLAUDE)
    review = world.review(submitted.proposal_id)
    world.revoke()
    result = world.service.approve(world.owner, submitted.proposal_id,
                                   review["approval_request_id"], review["review_digest"])
    assert result.receipt.status == "rejected"
    assert result.receipt.reason_codes == ("credential_revoked",)
    assert result.receipt.request_id == world.rows("os_proposals")[0]["rejection_id"]
    assert world.expenses() == []
    assert all(r.detail is None for r in world.tables()["batch_lines"].values())


def test_a_key_older_than_the_revoke_all_watermark_can_never_commit(world):
    world.credential()
    submitted = world.paste(batch(three_lines()), ctx=world.assistant(), source=CLAUDE)
    review = world.review(submitted.proposal_id)
    world.watermark["value"] = world.now()  # the key was created a day before
    result = world.service.approve(world.owner, submitted.proposal_id,
                                   review["approval_request_id"], review["review_digest"])
    assert (result.receipt.status, result.receipt.reason_codes) == (
        "rejected", ("credential_revoked",))


def test_a_missing_credential_row_fails_closed(fake_world):
    # PostgreSQL cannot reach this state: os_proposals(credential_id) is a foreign key.
    world = fake_world
    submitted = world.paste(batch(three_lines()), ctx=world.assistant(), source=CLAUDE)
    review = world.review(submitted.proposal_id)
    result = world.service.approve(world.owner, submitted.proposal_id,
                                   review["approval_request_id"], review["review_digest"])
    assert result.receipt.reason_codes == ("credential_revoked",)


def test_an_owner_paste_is_not_affected_by_the_watermark(world):
    submitted = world.paste(batch(three_lines()))
    world.watermark["value"] = world.now() + timedelta(days=1)
    assert world.approve(submitted.proposal_id).receipt.status == "committed"


def test_a_restored_batch_without_detail_is_refused_the_same_way(world):
    submitted = world.paste(batch(three_lines()))
    review = world.review(submitted.proposal_id)
    world.drop_batch_lines()
    result = world.service.approve(world.owner, submitted.proposal_id,
                                   review["approval_request_id"], review["review_digest"])
    assert (result.receipt.status, result.receipt.reason_codes) == (
        "expired", ("restored_without_detail",))
    assert world.expenses() == []


# ---- the rules the owner confirms ---------------------------------------------------------------


def test_make_rule_stores_a_business_rule_with_the_domain_id(world):
    import hashlib
    submitted = world.paste(batch(three_lines()))
    key = row_of(world, submitted.proposal_id, "DEMO SHOP")["line_key"]
    world.draft(submitted.proposal_id, [{"line_key": key, "tag": "business",
                                         "category": "Tools", "make_rule": True}])
    receipt = world.approve(submitted.proposal_id).receipt
    (rule,) = [r for r in world.tables()["rules"].values() if r.tag == "business"]
    expected = "MR_" + hashlib.sha256(b"merchant-rule\x1fdemo shop").hexdigest()[:24]
    assert rule.rule_id == expected and rule.merchant_key == "demo shop"
    assert (rule.category, rule.revision, rule.active) == ("Tools", 1, True)
    assert rule.source_proposal_id == submitted.proposal_id and receipt.status == "committed"
    # the next batch is classified by it
    nxt = world.paste(batch([line("DEMO SHOP", 555, tag=None)], number=2,
                            charge_date="2026-11-15"))
    item = world.lines(nxt.proposal_id)[0]
    assert (item["tag"], item["category"], item["rule_id"]) == ("business", "Tools", expected)


def test_a_rule_confirmation_without_a_tag_is_refused_at_approve_and_draft(world):
    submitted = world.paste(batch(three_lines()))
    key = row_of(world, submitted.proposal_id, "DEMO SHOP")["line_key"]
    with pytest.raises(api.LedgerError) as caught:
        world.draft(submitted.proposal_id, [{"line_key": key, "make_rule": True}])
    assert caught.value.details == {"reason": "rule_without_tag"}


CHARGE_DATES = {1: "2026-10-15", 2: "2026-11-15", 3: "2026-12-15", 4: "2027-01-15",
                5: "2027-02-15"}


def confirm_rule(world, number, tag, **extra):
    """A batch with one PERSONAL CAFE line, where the owner picks ``tag`` and ticks "always"."""
    submitted = world.paste(batch([line("PERSONAL CAFE", 900 + number, tag=None)], number=number,
                                  charge_date=CHARGE_DATES[number]))
    key = world.lines(submitted.proposal_id)[0]["line_key"]
    world.draft(submitted.proposal_id, [{"line_key": key, "tag": tag, "make_rule": True, **extra}])
    return submitted.proposal_id, world.approve(submitted.proposal_id).receipt


def only_rule(world):
    (rule,) = world.tables()["rules"].values()
    return rule


def test_a_merchant_rule_goes_personal_business_personal_and_back(world):
    """One live rule per merchant. The business row keeps the personal id, so going back to
    personal must update it in place: retiring it and inserting the same id is a PK collision."""
    _, receipt = confirm_rule(world, 1, "personal")
    rule = only_rule(world)
    assert receipt.status == "committed"
    assert (rule.tag, rule.merchant_key, rule.revision, rule.active) == ("personal", None, 1, True)
    rule_id, key_hmac = rule.rule_id, rule.merchant_key_hmac

    confirm_rule(world, 2, "business", category="Food")
    rule = only_rule(world)
    assert (rule.rule_id, rule.tag, rule.category, rule.revision, rule.active) == (
        rule_id, "business", "Food", 2, True)

    third, receipt = confirm_rule(world, 3, "personal")
    assert receipt.status == "committed"
    assert only_rule(world) == RuleRow(
        rule_id=rule_id, tag="personal", match="exact", merchant_key=None,
        merchant_key_hmac=key_hmac, category=None, path=None, platform=None,
        is_subscription=None, label=None, aliases=(), active=True, revision=3,
        source_proposal_id=third)
    nxt = world.paste(batch([line("PERSONAL CAFE", 777, tag=None)], number=4,
                            charge_date=CHARGE_DATES[4]))
    item = world.lines(nxt.proposal_id)[0]
    assert (item["tag"], item["rule_id"]) == ("personal", rule_id)
    world.approve(nxt.proposal_id)

    _, receipt = confirm_rule(world, 5, "business", category="Food")
    rule = only_rule(world)
    assert receipt.status == "committed"
    assert (rule.rule_id, rule.tag, rule.category, rule.revision, rule.active) == (
        rule_id, "business", "Food", 4, True)


# ---- re-import: replace semantics through the ledger ---------------------------------------------


def reimport(world, lines, number):
    return world.paste(batch(lines, number=number))


def test_a_changed_reimport_updates_voids_and_adds_through_apply(world):
    first = world.paste(batch([line("ALPHA", 1000), line("BRAVO", 2000), line("CHARLIE", 3000)]))
    world.approve(first.proposal_id)
    by_vendor = {e["vendor"]: e for e in world.expenses()}
    second = reimport(world, [line("ALPHA", 1000, suggested_category="Renamed"),
                              line("CHARLIE", 3000), line("DELTA", 4000)], 2)
    assert second.statement_id == first.statement_id
    # What the owner approved last time stands against a newer assistant reading (V2-25) ...
    alpha_key = row_of(world, second.proposal_id, "ALPHA")["line_key"]
    assert row_of(world, second.proposal_id, "ALPHA")["action"] == "kept_earlier_choice"
    # ... until he says so himself.
    world.draft(second.proposal_id, [{"line_key": alpha_key, "category": "Renamed"}])
    actions = {i["merchant_display"]: i["action"] for i in world.lines(second.proposal_id)}
    assert actions == {"ALPHA": "update", "CHARLIE": "keep", "DELTA": "add"}
    review = world.review(second.proposal_id)
    assert review["counts"]["void"] == 1 and review["counts"]["add"] == 1
    version = world.version()
    world.approve(second.proposal_id)
    assert world.version() == version + 1
    now = {e["vendor"]: e for e in world.expenses()}
    assert now["ALPHA"]["category"] == "Renamed" and now["ALPHA"]["revision"] == 2
    assert now["BRAVO"]["payment_status"] == "בוטל" and now["BRAVO"]["revision"] == 2
    assert now["CHARLIE"]["revision"] == 1 and now["DELTA"]["revision"] == 1
    assert by_vendor["ALPHA"]["expense_id"] == now["ALPHA"]["expense_id"]
    rows = stored(world, first.statement_id)
    bravo = next(r for r in rows.values() if r.merchant_display == "BRAVO")
    assert bravo.state == "replaced" and bravo.expense_revision_at_import == 2
    alpha = next(r for r in rows.values() if r.merchant_display == "ALPHA")
    assert alpha.expense_revision_at_import == 2 and alpha.revision == 2


def test_an_unchanged_reimport_is_all_keep_and_writes_no_expense(world):
    first = world.paste(batch([line("ALPHA", 1000), line("BRAVO", 2000)]))
    world.approve(first.proposal_id)
    second = reimport(world, [line("ALPHA", 1000), line("BRAVO", 2000)], 2)
    assert {i["action"] for i in world.lines(second.proposal_id)} == {"keep"}
    result = world.approve(second.proposal_id)
    assert result.receipt.status == "committed" and result.receipt.records == ()
    assert result.records == () and {e["revision"] for e in world.expenses()} == {1}
    assert world.rows("os_outbox")[-1]["body"]["refs"] == []


def test_the_owner_edit_is_kept_unless_he_chooses_to_overwrite(world):
    first = world.paste(batch([line("ALPHA", 1000)]))
    world.approve(first.proposal_id)
    expense = world.expenses()[0]
    world.edit_expense(expense["expense_id"], category="Hand edited")
    second = reimport(world, [line("ALPHA", 1000, suggested_category="From bank")], 2)
    key = world.lines(second.proposal_id)[0]["line_key"]
    world.draft(second.proposal_id, [{"line_key": key, "category": "From bank"}])
    item = world.lines(second.proposal_id)[0]
    assert item["action"] == "owner_edited_kept"
    world.approve(second.proposal_id)
    assert world.expense_record(expense["expense_id"])["category"] == "Hand edited"
    third = reimport(world, [line("ALPHA", 1000, suggested_category="From bank")], 3)
    world.draft(third.proposal_id, [{"line_key": key, "category": "From bank"}],
                {"owner_edited": {key: "overwrite"}})
    world.approve(third.proposal_id)
    assert world.expense_record(expense["expense_id"])["category"] == "From bank"


def test_a_line_that_becomes_personal_and_back_reuses_its_cancelled_expense(world):
    first = world.paste(batch([line("ALPHA", 1000), line("BRAVO", 2000)]))
    world.approve(first.proposal_id)
    alpha = next(e for e in world.expenses() if e["vendor"] == "ALPHA")
    second = reimport(world, [line("ALPHA", 1000), line("BRAVO", 2000)], 2)
    key = row_of(world, second.proposal_id, "ALPHA")["line_key"]
    world.draft(second.proposal_id, [{"line_key": key, "tag": "personal"}])
    world.approve(second.proposal_id)
    assert world.expense_record(alpha["expense_id"])["payment_status"] == "בוטל"
    personal = stored(world, first.statement_id)[key]
    assert personal.tag == "personal" and personal.expense_id is None
    assert personal.merchant_display is None and personal.amount_minor is None
    third = reimport(world, [line("ALPHA", 1000), line("BRAVO", 2000)], 3)
    world.draft(third.proposal_id, [{"line_key": key, "tag": "business"}])
    world.approve(third.proposal_id)
    revived = world.expense_record(alpha["expense_id"])
    assert revived["payment_status"] == "שולם" and revived["revision"] == 3
    assert stored(world, first.statement_id)[key].expense_id == alpha["expense_id"]
    assert len(world.expenses()) == 2


def test_a_hand_entry_that_stops_fitting_blocks_until_the_owner_decides(world):
    world.hand_expense("EXP_h", vendor="Demo Shop", amount=10000, day="2026-09-20")
    first = world.paste(batch([line("DEMO SHOP", 10000)]))
    assert world.lines(first.proposal_id)[0]["candidate_expense_ids"] == ["EXP_h"]
    world.approve(first.proposal_id)
    key = next(iter(stored(world, first.statement_id)))
    assert stored(world, first.statement_id)[key].matched_expense_id == "EXP_h"
    world.edit_expense("EXP_h", amount_ils=9999)  # E4.1: the linked hand entry is passed in
    second = reimport(world, [line("DEMO SHOP", 10000)], 2)
    item = world.lines(second.proposal_id)[0]
    assert LINKED_EXPENSE_CHANGED in item["flags"] and "merge_choice_required" in item["flags"]
    assert world.review(second.proposal_id)["blocking"] == ["merge_choice_required"]
    world.draft(second.proposal_id, [], {"merge": {key: None}})  # unlink: book the bank charge
    world.approve(second.proposal_id)
    assert stored(world, first.statement_id)[key].matched_expense_id is None
    assert [e["vendor"] for e in world.expenses()] == ["DEMO SHOP"]
    assert world.expense_record("EXP_h")["amount_ils"] == 9999  # never modified by the statement


def test_approving_one_batch_supersedes_the_other_pending_batch_of_the_statement(world):
    world.credential()
    mine = world.paste(batch(three_lines(), number=1))
    theirs = world.paste(batch(three_lines(), number=2), ctx=world.assistant(), source=CLAUDE)
    version = world.version()
    world.approve(mine.proposal_id)
    statuses = {p["proposal_id"]: p["status"] for p in world.rows("os_proposals")}
    assert statuses == {mine.proposal_id: "committed", theirs.proposal_id: "superseded"}
    assert world.version() == version + 2
    assert all(r.detail is None for r in world.tables()["batch_lines"].values())
    assert world.tables()["statements"][mine.statement_id].current_proposal_id is None
    receipt = next(r for r in world.rows("os_receipts") if r["proposal_id"] == theirs.proposal_id)
    assert receipt["status"] == "superseded"
    imported = world.tables()["statements"][mine.statement_id].imported_version
    mine_receipt = next(r for r in world.rows("os_receipts")
                        if r["proposal_id"] == mine.proposal_id)
    assert imported == mine_receipt["commit_version"]


def test_the_first_cycle_fixture_round_trips(world):
    from tests.unit.statements.helpers import fixture
    raw = fixture()["october"]
    submitted = world.paste(__import__("json").dumps(raw, ensure_ascii=False).encode())
    review = world.review(submitted.proposal_id)
    assert review["line_count"] == len(raw["lines"])
    if review["blocking"]:
        world.tag_all(submitted.proposal_id, "business")
    world.config.kill_switch = False
    result = world.approve(submitted.proposal_id)
    assert result.receipt.status == "committed"
