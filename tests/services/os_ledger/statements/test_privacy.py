"""V2-26 privacy (I7, I11, FORK-PERSONAL-MIN): personal detail, personal rule ids and logs."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from tests.services.os_ledger.statements.support import batch, line, three_lines
from tests.unit.statements.helpers import PEPPER

SECRETS = ("PERSONAL CAFE", "personal cafe", "421731", "ZZ-PRIVATE-SHOP")


def derived_sha256_id(merchant_key: str) -> str:
    return "MR_" + hashlib.sha256(("merchant-rule\x1f" + merchant_key).encode()).hexdigest()[:24]


def hmac_id(merchant_key: str) -> str:
    digest = hmac.new(PEPPER, ("merchant-rule\x1f" + merchant_key).encode(), hashlib.sha256)
    return "MR_" + digest.hexdigest()[:24]


def confirm_personal(world, lines, number=1):
    submitted = world.paste(batch(lines, number=number))
    key = next(i["line_key"] for i in world.lines(submitted.proposal_id)
               if i["merchant_display"] == "PERSONAL CAFE")
    world.draft(submitted.proposal_id, [{"line_key": key, "tag": "personal", "make_rule": True}])
    return submitted, key


def test_a_personal_rule_id_is_hmac_derived_never_the_sha256_id(world):
    submitted, _ = confirm_personal(world, three_lines())
    world.approve(submitted.proposal_id)
    personal = [r for r in world.tables()["rules"].values() if r.tag == "personal"]
    assert len(personal) == 1
    rule = personal[0]
    assert rule.rule_id == hmac_id("personal cafe")
    assert rule.rule_id != derived_sha256_id("personal cafe")
    assert (rule.merchant_key, rule.match, rule.aliases, rule.label) == (None, "exact", (), None)
    assert (rule.category, rule.path, rule.platform, rule.is_subscription) == (None,) * 4
    assert len(rule.merchant_key_hmac) == 64 and rule.active and rule.revision == 1
    assert derived_sha256_id("personal cafe") not in world.everything()
    assert "personal cafe" not in world.everything().lower()


def test_a_personal_rule_is_matched_by_hmac_on_the_next_batch(world):
    submitted, _ = confirm_personal(world, three_lines())
    world.approve(submitted.proposal_id)
    nxt = world.paste(batch([line("PERSONAL CAFE", 900, tag=None)], number=2,
                            charge_date="2026-11-15"))
    item = world.lines(nxt.proposal_id)[0]
    assert (item["tag"], item["rule_id"], item["suggested_tag"]) == (
        "personal", hmac_id("personal cafe"), None)
    assert nxt.summary["personal_count"] == 1 and nxt.summary["unassigned_count"] == 0


def test_a_personal_rule_that_disagrees_with_the_assistant_is_flagged(world):
    submitted, _ = confirm_personal(world, three_lines())
    world.approve(submitted.proposal_id)
    nxt = world.paste(batch([line("PERSONAL CAFE", 900, tag="business")], number=2,
                            charge_date="2026-11-15"))
    item = world.lines(nxt.proposal_id)[0]
    assert item["tag"] == "personal" and item["disagreement"] is True
    assert nxt.summary["disagreement_count"] == 1


def test_a_personal_rule_turned_business_keeps_its_row_and_gains_the_key(world):
    submitted, _ = confirm_personal(world, three_lines())
    world.approve(submitted.proposal_id)
    nxt = world.paste(batch([line("PERSONAL CAFE", 900, tag=None)], number=2,
                            charge_date="2026-11-15"))
    key = world.lines(nxt.proposal_id)[0]["line_key"]
    world.draft(nxt.proposal_id, [{"line_key": key, "tag": "business", "category": "Food",
                                   "make_rule": True}])
    world.approve(nxt.proposal_id)
    (rule,) = world.tables()["rules"].values()
    assert rule.rule_id == hmac_id("personal cafe") and rule.revision == 2
    assert (rule.tag, rule.merchant_key, rule.category) == ("business", "personal cafe", "Food")


def test_a_business_rule_turned_personal_retires_it_and_mints_an_hmac_rule(world):
    submitted = world.paste(batch([line("ZZ-PRIVATE-SHOP", 700)]))
    key = world.lines(submitted.proposal_id)[0]["line_key"]
    world.draft(submitted.proposal_id, [{"line_key": key, "tag": "business", "make_rule": True}])
    world.approve(submitted.proposal_id)
    nxt = world.paste(batch([line("ZZ-PRIVATE-SHOP", 800)], number=2, charge_date="2026-11-15"))
    key2 = world.lines(nxt.proposal_id)[0]["line_key"]
    world.draft(nxt.proposal_id, [{"line_key": key2, "tag": "personal", "make_rule": True}])
    world.approve(nxt.proposal_id)
    rules = list(world.tables()["rules"].values())
    active = [r for r in rules if r.active]
    assert [(r.tag, r.rule_id) for r in active] == [("personal", hmac_id("zz private shop"))]
    assert active[0].merchant_key is None
    retired = [r for r in rules if not r.active]
    assert len(retired) == 1 and retired[0].tag == "business" and retired[0].revision == 2
    assert len({(r.match, r.merchant_key_hmac) for r in rules}) == len(rules)
    assert [r["rule_id"] for r in world.service.merchant_rules(world.reader)["rules"]] == [
        hmac_id("zz private shop")]


def test_nothing_personal_survives_an_approval_anywhere(world):
    submitted = world.paste(batch([
        line("DEMO SHOP", 10000), line("PERSONAL CAFE", 421731, tag="personal"),
        line("ZZ-PRIVATE-SHOP", 619377, tag="personal")]))
    world.approve(submitted.proposal_id)
    dump = world.everything()
    for secret in SECRETS + ("619377",):
        assert secret not in dump, secret
    assert "DEMO SHOP" in dump  # the business line is, of course, kept


def test_a_rejected_batch_leaves_no_personal_detail_either(world):
    submitted = world.paste(batch(three_lines()))
    assert "PERSONAL CAFE" in world.everything()  # while pending, the detail is there
    world.reject(submitted.proposal_id)
    assert "PERSONAL CAFE" not in world.everything()


def test_a_superseded_batch_is_purged_at_once(world):
    world.paste(batch(three_lines(), number=1))
    world.paste(batch([line("OTHER", 1)], number=2))
    assert "PERSONAL CAFE" not in world.everything()


def test_events_receipts_and_outbox_carry_no_statement_text(world):
    submitted = world.paste(batch(three_lines()))
    world.approve(submitted.proposal_id)
    ledger_side = "\n".join(repr(world.rows(t)) for t in ("os_events", "os_receipts", "os_outbox"))
    for secret in SECRETS + ("DEMO SHOP", "10000"):
        assert secret not in ledger_side, secret


def test_no_log_line_carries_statement_text_amounts_or_ids(world, caplog):
    with caplog.at_level("DEBUG"):
        submitted = world.paste(batch(three_lines() + [line("ZZ-PRIVATE-SHOP", 619377)]))
        key = world.lines(submitted.proposal_id)[0]["line_key"]
        world.draft(submitted.proposal_id, [{"line_key": key, "category": "Tools"}])
        world.approve(submitted.proposal_id)
        world.paste(batch(three_lines(), number=2))
        world.advance(8 * 24 * 3600)
        world.service.expire_due(world.worker)
        with pytest.raises(Exception):  # a refused call logs a reason code too
            world.paste(b"not json")
    text = caplog.text
    assert "os_statements" in text  # the service does log, as codes and counts
    for secret in SECRETS + ("DEMO SHOP", "BIZ REFUND", "10000", "619377", "1500", key,
                             submitted.proposal_id, submitted.statement_id):
        assert secret not in text, secret
