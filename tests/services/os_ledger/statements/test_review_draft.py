"""V2-26 review, lines and the server-side draft (contract §12.2, §12.4)."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from app.domain.statements import normalize_merchant
from app.services.os_ledger import api

from tests.services.os_ledger.statements.support import (
    CLAUDE,
    batch,
    line,
    three_lines,
)

ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
REVIEW_KEYS = {
    "api_version", "source", "workspace_id", "authority_epoch", "observed_version",
    "proposal_id", "statement_id", "cycle", "account_label", "charge_date", "actor_source",
    "credential_label", "submitted_at", "expires_at", "status", "approval_request_id",
    "rejection_request_id", "conflict_with", "draft_revision", "review_digest", "counts",
    "merges", "blocking", "totals", "line_count"}
LINE_KEYS = {
    "line_key", "txn_date", "merchant_display", "amount_minor", "kind", "currency",
    "original_amount_minor", "original_currency", "installment", "suggested_tag", "tag",
    "category", "path", "is_subscription", "rule_id", "disagreement", "confidence", "action",
    "flags", "candidate_expense_ids"}


def refused(code: str):
    return pytest.raises(api.LedgerError, match=f"^{code}$")


def test_a_fresh_review_has_exactly_the_contract_keys(world):
    world.credential()
    submitted = world.paste(batch(three_lines()), ctx=world.assistant(), source=CLAUDE)
    review = world.review(submitted.proposal_id)
    assert set(review) == REVIEW_KEYS
    assert (review["api_version"], review["source"], review["workspace_id"],
            review["authority_epoch"]) == ("assaf-os.v2", "postgresql", "test", 1)
    assert review["proposal_id"] == submitted.proposal_id
    assert review["statement_id"] == submitted.statement_id
    assert (review["cycle"], review["account_label"], review["charge_date"]) == (
        "2026-10", "Synthetic Business Card", "2026-10-15")
    assert (review["actor_source"], review["credential_label"]) == (CLAUDE, "Claude desktop")
    assert review["status"] == "pending" and review["draft_revision"] == 0
    assert review["approval_request_id"] != review["rejection_request_id"]
    assert ISO.fullmatch(review["submitted_at"]) and ISO.fullmatch(review["expires_at"])
    assert review["conflict_with"] == [] and review["line_count"] == 3
    assert review["counts"] == {"add": 3, "keep": 0, "update": 0, "void": 0,
                                "owner_edited_kept": 0, "kept_earlier_choice": 0}
    assert review["merges"] == [] and review["blocking"] == []
    assert review["totals"] == {
        "business_charge_minor": 10000, "business_credit_minor": 1500,
        "business_total_minor": 8500, "business_line_count": 2, "personal_count": 1,
        "unassigned_count": 0}
    assert re.fullmatch(r"[0-9a-f]{64}", review["review_digest"])


def test_an_owner_paste_has_no_credential_label(world):
    submitted = world.paste(batch(three_lines()))
    review = world.review(submitted.proposal_id)
    assert review["actor_source"] == "ASSISTANT_PASTE" and review["credential_label"] is None


def test_unassigned_lines_block_and_are_reported(world):
    submitted = world.paste(batch([line("MYSTERY", 100, tag=None), line("SHOP", 200)]))
    review = world.review(submitted.proposal_id)
    assert review["blocking"] == ["unassigned_lines"]
    assert review["totals"]["unassigned_count"] == 1


def test_a_read_never_changes_the_ledger(world):
    submitted = world.paste(batch(three_lines()))
    before = (world.version(), world.everything())
    world.review(submitted.proposal_id)
    world.lines(submitted.proposal_id)
    assert (world.version(), world.everything()) == before


def test_the_review_digest_is_stable_and_moves_with_the_draft(world):
    submitted = world.paste(batch(three_lines()))
    first = world.review(submitted.proposal_id)["review_digest"]
    assert world.review(submitted.proposal_id)["review_digest"] == first
    key = world.lines(submitted.proposal_id)[0]["line_key"]
    saved = world.draft(submitted.proposal_id, [{"line_key": key, "category": "Tools"}])
    assert saved["draft_revision"] == 1 and saved["review_digest"] != first
    assert world.review(submitted.proposal_id)["review_digest"] == saved["review_digest"]


# ---- lines ------------------------------------------------------------------------------------


def test_lines_have_the_contract_keys_and_the_resolved_values(world):
    submitted = world.paste(batch([
        line("OVERSEAS SAAS", 7500, original_amount_minor=2000, original_currency="USD",
             suggested_category="כלי AI", is_subscription=True, confidence=0.5),
        line("SHOP INSTALL", 900, installment={"n": 2, "of": 6}, tag=None)]))
    lines = world.lines(submitted.proposal_id)
    assert len(lines) == 2 and all(set(item) == LINE_KEYS for item in lines)
    saas = next(i for i in lines if i["merchant_display"] == "OVERSEAS SAAS")
    assert saas["amount_minor"] == 7500 and saas["kind"] == "charge" and saas["currency"] == "ILS"
    assert (saas["original_amount_minor"], saas["original_currency"]) == (2000, "USD")
    assert saas["installment"] is None and saas["suggested_tag"] == "business"
    assert (saas["tag"], saas["category"], saas["is_subscription"]) == ("business", "כלי AI", True)
    assert saas["rule_id"] is None and saas["disagreement"] is False and saas["confidence"] == 0.5
    assert saas["action"] == "add" and "low_confidence" in saas["flags"]
    assert saas["candidate_expense_ids"] == []
    installment = next(i for i in lines if i["merchant_display"] == "SHOP INSTALL")
    assert installment["installment"] == {"n": 2, "of": 6}
    assert installment["tag"] is None and "unassigned" in installment["flags"]


def test_lines_are_ordered_by_merchant_key_then_date_then_line_key(world):
    rows = [line("ZETA", 100, day="2026-09-02"), line("alpha", 100, day="2026-09-05"),
            line("ALPHA", 100, day="2026-09-03"), line("Mid", 300), line("mid", 300),
            line("ALPHA", 100, day="2026-09-03")]
    submitted = world.paste(batch(rows))
    lines = world.lines(submitted.proposal_id)
    keys = [(normalize_merchant(i["merchant_display"]), i["txn_date"], i["line_key"])
            for i in lines]
    assert keys == sorted(keys) and len(lines) == 6


def test_lines_page_with_an_opaque_cursor(world):
    submitted = world.paste(batch([line(f"SHOP {n:03d}", 100 + n) for n in range(250)]))
    first = world.service.lines(world.reader, submitted.proposal_id, after=None, limit=100)
    assert len(first["lines"]) == 100 and isinstance(first["next_cursor"], str)
    assert set(first) == {"api_version", "source", "workspace_id", "authority_epoch",
                          "observed_version", "lines", "next_cursor"}
    second = world.service.lines(world.reader, submitted.proposal_id,
                                 after=first["next_cursor"], limit=100)
    third = world.service.lines(world.reader, submitted.proposal_id,
                                after=second["next_cursor"], limit=100)
    assert len(second["lines"]) == 100 and len(third["lines"]) == 50
    assert third["next_cursor"] is None
    seen = [i["line_key"] for page in (first, second, third) for i in page["lines"]]
    assert len(set(seen)) == 250


def test_a_forged_cursor_and_a_bad_limit_are_refused(world):
    submitted = world.paste(batch(three_lines()))
    for cursor in ("garbage", "c1." + "0" * 64 + "." + "0" * 22, "x" * 600):
        with refused("invalid_cursor"):
            world.service.lines(world.reader, submitted.proposal_id, after=cursor, limit=10)
    for limit in (0, 101, -1):
        with refused("invalid_query"):
            world.service.lines(world.reader, submitted.proposal_id, after=None, limit=limit)


def test_a_cursor_from_another_batch_is_refused(world):
    first = world.paste(batch([line(f"SHOP {n:03d}", 100 + n) for n in range(150)], number=1))
    page = world.service.lines(world.reader, first.proposal_id, after=None, limit=100)
    world.config.assaf_os_freeze = False
    second = world.paste(batch([line(f"SHOP {n:03d}", 100 + n) for n in range(150)],
                               number=2, charge_date="2026-11-15"))
    with refused("invalid_cursor"):
        world.service.lines(world.reader, second.proposal_id, after=page["next_cursor"], limit=100)


def test_unknown_batches_are_404(world):
    for call in (lambda: world.service.review(world.reader, "PRO_" + "0" * 32),
                 lambda: world.service.lines(world.reader, "PRO_" + "0" * 32, after=None, limit=5),
                 lambda: world.service.review(world.reader, "not-an-id")):
        with refused("batch_not_found"):
            call()


def test_a_non_batch_proposal_is_not_a_batch(world):
    proposal = world.ledger.propose(
        world.owner, api.ProposeInput(world.new_id(), "tasks", None, 0,
                                      {"title": "Task", "status": "לביצוע"}))
    with refused("batch_not_found"):
        world.service.review(world.reader, proposal.proposal_id)


# ---- the draft --------------------------------------------------------------------------------


def test_a_draft_bumps_the_revision_and_reports_blocking(world):
    submitted = world.paste(batch([line("MYSTERY", 100, tag=None), line("SHOP", 200)]))
    key = next(i["line_key"] for i in world.lines(submitted.proposal_id) if i["tag"] is None)
    saved = world.draft(submitted.proposal_id, [{"line_key": key, "tag": "business"}])
    assert set(saved) == {"api_version", "source", "workspace_id", "authority_epoch",
                          "observed_version", "draft_revision", "review_digest", "blocking"}
    assert saved["draft_revision"] == 1 and saved["blocking"] == []
    assert world.review(submitted.proposal_id)["blocking"] == []
    version = world.version()
    again = world.draft(submitted.proposal_id, [])
    assert again["draft_revision"] == 2 and world.version() == version  # a draft is not a commit


def test_a_stale_draft_revision_is_a_draft_conflict_and_changes_nothing(world):
    submitted = world.paste(batch(three_lines()))
    key = world.lines(submitted.proposal_id)[0]["line_key"]
    world.draft(submitted.proposal_id, [{"line_key": key, "category": "Tools"}])
    before = world.saved_drafts()
    with refused("draft_conflict"):
        world.service.save_draft(world.owner, submitted.proposal_id, 0,
                                 [{"line_key": key, "category": "Other"}], {})
    assert world.saved_drafts() == before


def test_draft_deltas_merge_per_line_and_remove_with_all_null(world):
    submitted = world.paste(batch([line(f"SHOP {n:03d}", 100 + n, tag=None) for n in range(250)]))
    keys = [i["line_key"] for i in world.lines(submitted.proposal_id)]
    assert len(keys) == 250
    for start in (0, 100, 200):  # more than 100 corrections, over several POSTs of <= 100
        chunk = keys[start:start + 100]
        saved = world.draft(submitted.proposal_id, [{"line_key": k, "tag": "business"}
                                                    for k in chunk])
    assert saved["draft_revision"] == 3 and saved["blocking"] == []
    stored = world.tables()["drafts"][submitted.proposal_id].overrides
    assert len(stored) == 250 and all(v == {"tag": "business"} for v in stored.values())
    # an entry replaces that line's stored override whole, so the old tag is gone
    world.draft(submitted.proposal_id, [{"line_key": keys[0], "category": "Tools"}])
    assert world.tables()["drafts"][submitted.proposal_id].overrides[keys[0]] == {
        "category": "Tools"}
    # an entry with every optional field null removes it
    saved = world.draft(submitted.proposal_id, [{"line_key": keys[1], "tag": None}])
    assert keys[1] not in world.tables()["drafts"][submitted.proposal_id].overrides
    assert saved["blocking"] == ["unassigned_lines"]


def test_more_than_100_overrides_in_one_post_are_refused(world):
    submitted = world.paste(batch([line(f"SHOP {n:03d}", 100 + n, tag=None) for n in range(150)]))
    keys = [i["line_key"] for i in world.lines(submitted.proposal_id)]
    with pytest.raises(api.LedgerError) as caught:
        world.draft(submitted.proposal_id, [{"line_key": k, "tag": "business"} for k in keys[:101]])
    assert caught.value.code == "validation_error"
    assert caught.value.details == {"reason": "too_many_overrides"}
    assert world.saved_drafts() == {}


@pytest.mark.parametrize("entry,reason", [
    ({"line_key": "0" * 64, "tag": "business"}, "unknown_line"),
    ({"line_key": "nope", "tag": "business"}, "invalid_line_key"),
    ({"tag": "business"}, "invalid_override"),
    ({"line_key": "KEY", "tag": "mine"}, "invalid_override"),
    ({"line_key": "KEY", "path": "elsewhere"}, "invalid_override"),
    ({"line_key": "KEY", "is_subscription": "yes"}, "invalid_override"),
    ({"line_key": "KEY", "extra": 1}, "invalid_override"),
    ({"line_key": "KEY", "make_rule": True}, "rule_without_tag"),
])
def test_invalid_draft_entries_are_refused_with_a_reason(world, entry, reason):
    submitted = world.paste(batch(three_lines()))
    key = world.lines(submitted.proposal_id)[0]["line_key"]
    entry = {k: (key if v == "KEY" else v) for k, v in entry.items()}
    with pytest.raises(api.LedgerError) as caught:
        world.draft(submitted.proposal_id, [entry])
    assert (caught.value.code, caught.value.details) == ("validation_error", {"reason": reason})
    assert world.saved_drafts() == {}


def test_choices_are_validated_and_merge_by_key(world):
    world.hand_expense("EXP_coffee_a", vendor="Coffee Cart", amount=1800, day="2026-09-19")
    world.hand_expense("EXP_coffee_b", vendor="coffee cart", amount=1800, day="2026-09-20")
    submitted = world.paste(batch([line("COFFEE CART", 1800, day="2026-09-20")]))
    item = world.lines(submitted.proposal_id)[0]
    assert item["candidate_expense_ids"] == ["EXP_coffee_a", "EXP_coffee_b"]
    assert "merge_choice_required" in item["flags"]
    assert world.review(submitted.proposal_id)["blocking"] == ["merge_choice_required"]
    with pytest.raises(api.LedgerError) as caught:
        world.draft(submitted.proposal_id, [], {"merge": {item["line_key"]: "EXP_other"}})
    assert caught.value.details == {"reason": "merge_choice_invalid"}
    saved = world.draft(submitted.proposal_id, [],
                        {"merge": {item["line_key"]: "EXP_coffee_b"}})
    assert saved["blocking"] == []
    review = world.review(submitted.proposal_id)
    assert review["merges"] == [{"line_key": item["line_key"], "expense_id": "EXP_coffee_b",
                                 "state": "proposed",
                                 "candidate_expense_ids": ["EXP_coffee_a", "EXP_coffee_b"]}]
    # a later POST merges by key and keeps the earlier choice
    again = world.draft(submitted.proposal_id, [], {"owner_edited": {}})
    assert again["blocking"] == []
    assert world.tables()["drafts"][submitted.proposal_id].choices["merge"] == {
        item["line_key"]: "EXP_coffee_b"}


def test_a_stale_merge_choice_is_dropped_before_the_recompute(world):
    world.hand_expense("EXP_coffee_a", vendor="Coffee Cart", amount=1800, day="2026-09-19")
    world.hand_expense("EXP_coffee_b", vendor="coffee cart", amount=1800, day="2026-09-20")
    submitted = world.paste(batch([line("COFFEE CART", 1800, day="2026-09-20")]))
    key = world.lines(submitted.proposal_id)[0]["line_key"]
    world.draft(submitted.proposal_id, [], {"merge": {key: "EXP_coffee_b"}})
    digest = world.review(submitted.proposal_id)["review_digest"]
    world.edit_expense("EXP_coffee_b", payment_status="בוטל")  # the chosen candidate is cancelled
    review = world.review(submitted.proposal_id)
    # E4.2: the review is not stuck on merge_choice_invalid, it simply reads the line again
    assert review["review_digest"] != digest
    assert world.lines(submitted.proposal_id)[0]["candidate_expense_ids"] == ["EXP_coffee_a"]
    assert review["merges"][0]["expense_id"] == "EXP_coffee_a"


def test_a_pending_batch_past_its_expiry_reads_as_expired(world):
    submitted = world.paste(batch(three_lines()))
    world.advance(7 * 24 * 3600 + 1)
    review = world.review(submitted.proposal_id)
    assert review["status"] == "expired" and review["line_count"] == 3
    assert [p["status"] for p in world.rows("os_proposals")] == ["pending"]  # reads never write
    with refused("batch_not_found"):
        world.service.save_draft(world.owner, "PRO_" + "1" * 32, 0, [], {})


def test_a_draft_after_expiry_is_refused_and_writes_nothing(world):
    submitted = world.paste(batch(three_lines()))
    key = world.lines(submitted.proposal_id)[0]["line_key"]
    world.advance(7 * 24 * 3600 + 1)
    with refused("review_stale"):
        world.service.save_draft(world.owner, submitted.proposal_id, 0,
                                 [{"line_key": key, "tag": "personal"}], {})
    assert world.saved_drafts() == {}


def test_the_draft_needs_the_approve_scope_and_a_verified_mutation(world):
    submitted = world.paste(batch(three_lines()))
    with refused("scope_denied"):
        world.service.save_draft(world.ctx("OS", scopes={"assaf-os:read"}),
                                 submitted.proposal_id, 0, [], {})
    with refused("browser_mutation_verification_required"):
        world.service.save_draft(world.reader, submitted.proposal_id, 0, [], {})
    for channel in ("MIA", "TELEGRAM_APPROVAL"):
        with refused("channel_operation_denied"):
            world.service.save_draft(world.ctx(channel), submitted.proposal_id, 0, [], {})


def test_the_whole_cycle_does_not_log_statement_text(world, caplog):
    with caplog.at_level("DEBUG"):
        submitted = world.paste(batch(three_lines()))
        world.review(submitted.proposal_id)
        world.lines(submitted.proposal_id)
    text = caplog.text
    for secret in ("DEMO SHOP", "PERSONAL CAFE", "BIZ REFUND", "10000", "421731", "1500"):
        assert secret not in text
    assert timedelta  # keep the import honest


def test_a_decided_batch_reviews_without_detail_and_refuses_a_draft(world):
    submitted = world.paste(batch(three_lines()))
    world.reject(submitted.proposal_id)
    review = world.review(submitted.proposal_id)
    assert review["status"] == "rejected" and review["blocking"] == [] and review["merges"] == []
    assert review["counts"] == {"add": 0, "keep": 0, "update": 0, "void": 0,
                                "owner_edited_kept": 0, "kept_earlier_choice": 0}
    assert review["totals"]["business_line_count"] == 0 and review["conflict_with"] == []
    assert world.lines(submitted.proposal_id) == []
    with refused("already_decided"):
        world.service.save_draft(world.owner, submitted.proposal_id, 0, [], {})


def test_a_restored_batch_without_detail_reads_as_expired_and_refuses_a_draft(world):
    submitted = world.paste(batch(three_lines()))
    world.drop_batch_lines()
    review = world.review(submitted.proposal_id)
    assert review["status"] == "expired" and review["line_count"] == 0
    assert world.lines(submitted.proposal_id) == []
    with refused("review_stale"):
        world.service.save_draft(world.owner, submitted.proposal_id, 0, [], {})
