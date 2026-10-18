"""V2-26 cycle views, merchant rules and the assistant-facing reads (contract §12.2, §12.4)."""

from __future__ import annotations

import json

import pytest
from app.services.os_ledger import api

from tests.services.os_ledger.statements.support import (
    CLAUDE,
    CRED,
    CRED_B,
    batch,
    line,
    three_lines,
)

BINDING = {"api_version": "assaf-os.v2", "source": "postgresql", "workspace_id": "test",
           "authority_epoch": 1}


def refused(code: str):
    return pytest.raises(api.LedgerError, match=f"^{code}$")


def month(world, cycle: str, lines, number: int, account: str = "Synthetic Business Card"):
    submitted = world.paste(batch(lines, number=number, label=account,
                                  charge_date=f"{cycle}-15"))
    world.approve(submitted.proposal_id)
    return submitted


def test_there_are_no_cycles_before_any_statement(world):
    body = world.service.cycles(world.reader)
    assert body == {**BINDING, "observed_version": 0, "cycles": []}


def test_a_missing_cycle_is_null_never_zero(world):
    month(world, "2026-10", three_lines(), 1)
    entries = world.service.cycles(world.reader)["cycles"]
    assert len(entries) == 36 and entries[0]["cycle"] == "2026-10"
    assert [e["cycle"] for e in entries][:3] == ["2026-10", "2026-09", "2026-08"]
    imported = entries[0]
    assert set(imported) == {"cycle", "account_label", "charge_date", "statement_id", "status",
                             "business_total_minor", "business_line_count",
                             "pending_proposal_id"}
    assert (imported["status"], imported["charge_date"], imported["business_total_minor"],
            imported["business_line_count"]) == ("imported", "2026-10-15", 8500, 2)
    late = entries[1]
    assert (late["status"], late["statement_id"], late["charge_date"],
            late["business_total_minor"], late["business_line_count"],
            late["pending_proposal_id"]) == ("not_imported", None, None, None, None, None)
    assert late["account_label"] == "Synthetic Business Card"


def test_an_open_cycle_is_pending_until_the_20th_and_not_imported_after(world):
    month(world, "2026-09", three_lines(), 1)
    entry = world.service.cycles(world.reader)["cycles"][0]
    assert (entry["cycle"], entry["status"]) == ("2026-10", "pending")  # the clock reads Oct 16
    assert entry["pending_proposal_id"] is None and entry["business_total_minor"] is None
    world.advance(6 * 24 * 3600)  # Oct 22: past the 20th
    entry = world.service.cycles(world.reader)["cycles"][0]
    assert (entry["cycle"], entry["status"]) == ("2026-10", "not_imported")


def test_a_pending_batch_shows_in_its_cycle(world):
    submitted = world.paste(batch(three_lines()))
    entry = world.service.cycles(world.reader)["cycles"][0]
    assert (entry["status"], entry["pending_proposal_id"], entry["statement_id"]) == (
        "pending", submitted.proposal_id, None)
    assert entry["charge_date"] == "2026-10-15" and entry["business_total_minor"] is None
    world.advance(7 * 24 * 3600 + 1)  # an expired batch is not pending any more
    assert world.service.cycles(world.reader)["cycles"][0]["pending_proposal_id"] is None


def test_an_imported_statement_with_a_pending_reimport_reports_both(world):
    first = month(world, "2026-10", three_lines(), 1)
    again = world.paste(batch(three_lines(), number=2))
    entry = world.service.cycles(world.reader)["cycles"][0]
    assert (entry["status"], entry["statement_id"], entry["pending_proposal_id"]) == (
        "imported", first.statement_id, again.proposal_id)
    assert entry["business_total_minor"] == 8500  # the committed total, not the pending one


def test_the_list_is_capped_at_36_entries_across_accounts(world):
    month(world, "2026-10", three_lines(), 1)
    month(world, "2026-10", three_lines(), 2, account="Second Card")
    entries = world.service.cycles(world.reader)["cycles"]
    assert len(entries) == 36
    assert [(e["cycle"], e["account_label"]) for e in entries][:4] == [
        ("2026-10", "Second Card"), ("2026-10", "Synthetic Business Card"),
        ("2026-09", "Second Card"), ("2026-09", "Synthetic Business Card")]


def test_the_statement_view_totals_categories_subscriptions_and_previous(world):
    month(world, "2026-09", [line("CLAUDE SUB", 7500, suggested_category="AI",
                                  is_subscription=True),
                             line("OLD SUB", 900, suggested_category="AI", is_subscription=True),
                             line("COFFEE", 300, suggested_category="Food")], 1)
    second = month(world, "2026-10", [
        line("CLAUDE SUB", 8000, suggested_category="AI", is_subscription=True),
        line("NEW SUB", 400, suggested_category="AI", is_subscription=True),
        line("COFFEE", 600, suggested_category="Food"),
        line("MY LUNCH", 777, tag="personal"),
        line("REFUND", 100, kind="credit", suggested_category="Food")], 2)
    view = world.service.statement_view(world.reader, second.statement_id)
    assert set(view) == {*BINDING, "observed_version", "statement_id", "cycle", "account_label",
                         "charge_date", "cycle_from", "cycle_to", "totals", "by_category",
                         "subscriptions", "previous"}
    assert (view["cycle"], view["charge_date"]) == ("2026-10", "2026-10-15")
    assert view["cycle_from"] == "2026-09-20" and view["cycle_to"] == "2026-09-20"
    assert view["totals"] == {
        "business_charge_minor": 9000, "business_credit_minor": 100, "business_total_minor": 8900,
        "business_line_count": 4, "personal_count": 1, "unassigned_count": 0}
    assert view["by_category"] == [{"category": "AI", "amount_minor": 8400},
                                   {"category": "Food", "amount_minor": 500}]
    subs = {s["label"]: s for s in view["subscriptions"]}
    assert subs["CLAUDE SUB"]["badges"] == ["changed"]
    assert (subs["CLAUDE SUB"]["amount_minor"], subs["CLAUDE SUB"]["previous_amount_minor"]) == (
        8000, 7500)
    assert subs["NEW SUB"]["badges"] == ["new"]
    previous = view["previous"]
    assert previous["business_total_minor"] == 8700 and previous["statement_id"].startswith("STM_")


def test_the_first_statement_has_no_previous(world):
    first = month(world, "2026-10", three_lines(), 1)
    view = world.service.statement_view(world.reader, first.statement_id)
    assert view["previous"] is None and all("new" not in s["badges"] for s in
                                           view["subscriptions"])


def test_a_statement_that_was_never_approved_has_no_view(world):
    submitted = world.paste(batch(three_lines()))
    with refused("batch_not_found"):
        world.service.statement_view(world.reader, submitted.statement_id)
    with refused("batch_not_found"):
        world.service.statement_view(world.reader, "STM_" + "0" * 32)
    with refused("batch_not_found"):
        world.service.statement_view(world.reader, "bad")


def test_reads_need_the_read_scope_and_the_os_channel(world):
    first = month(world, "2026-10", three_lines(), 1)
    with refused("scope_denied"):
        world.service.cycles(world.ctx("OS", scopes={"assaf-os:propose"}))
    for channel in ("MIA", "TELEGRAM_APPROVAL"):
        with refused("channel_operation_denied"):
            world.service.statement_view(world.ctx(channel), first.statement_id)


def test_reads_do_not_write(world):
    first = month(world, "2026-10", three_lines(), 1)
    before = (world.version(), world.everything())
    world.service.cycles(world.reader)
    world.service.statement_view(world.reader, first.statement_id)
    world.service.merchant_rules(world.reader)
    assert (world.version(), world.everything()) == before


# ---- merchant rules -----------------------------------------------------------------------------


def test_the_rules_view_lists_active_rules_with_personal_keys_hidden(world):
    submitted = world.paste(batch(three_lines()))
    keys = {i["merchant_display"]: i["line_key"] for i in world.lines(submitted.proposal_id)}
    world.draft(submitted.proposal_id, [
        {"line_key": keys["DEMO SHOP"], "tag": "business", "category": "Tools",
         "is_subscription": True, "make_rule": True},
        {"line_key": keys["PERSONAL CAFE"], "tag": "personal", "make_rule": True}])
    world.approve(submitted.proposal_id)
    body = world.service.merchant_rules(world.reader)
    assert set(body) == {*BINDING, "observed_version", "rules"}
    rules = {r["tag"]: r for r in body["rules"]}
    assert set(rules["business"]) == {"rule_id", "merchant_key", "match", "tag", "category",
                                      "path", "platform", "is_subscription", "label", "revision"}
    assert (rules["business"]["merchant_key"], rules["business"]["category"],
            rules["business"]["is_subscription"], rules["business"]["revision"]) == (
        "demo shop", "Tools", True, 1)
    assert rules["personal"]["merchant_key"] is None and rules["personal"]["match"] == "exact"
    assert rules["personal"]["category"] is None
    assert "personal cafe" not in json.dumps(body).lower()


# ---- the assistant-facing reads -----------------------------------------------------------------


def test_the_assistant_context_has_business_rules_the_recent_statements_and_the_format(world):
    submitted = world.paste(batch(three_lines()))
    keys = {i["merchant_display"]: i["line_key"] for i in world.lines(submitted.proposal_id)}
    world.draft(submitted.proposal_id, [
        {"line_key": keys["DEMO SHOP"], "tag": "business", "make_rule": True},
        {"line_key": keys["PERSONAL CAFE"], "tag": "personal", "make_rule": True}])
    world.approve(submitted.proposal_id)
    for number, cycle in enumerate([f"2025-{m:02d}" for m in range(1, 13)], start=2):
        world.paste(batch(three_lines(), number=number, charge_date=f"{cycle}-15"))
    world.credential()
    body = world.service.context_for_assistant(world.assistant())
    rules = body["rules"]
    assert [set(r) for r in rules] == [{"merchant_key", "match", "category", "path", "platform",
                                        "is_subscription", "label"}]
    assert rules[0]["merchant_key"] == "demo shop"
    statements = body["statements"]
    assert len(statements) == 12
    assert set(statements[0]) == {"account_label", "charge_date", "status", "line_count"}
    assert statements[0]["charge_date"] == "2026-10-15" and statements[0]["status"] == "imported"
    assert statements[1]["status"] == "pending" and statements[1]["line_count"] == 3
    rendered = json.dumps(body, ensure_ascii=False)
    assert "personal" not in rendered.lower().replace("personal_count", "")
    # E4.4: the assistant is told to emit compact UTF-8 JSON
    assert body["format"] == {
        "schema": "statement-batch.v1", "encoding": "utf-8", "ensure_ascii": False,
        "separators": [",", ":"], "max_bytes": 131072, "max_lines": 300}


def test_the_assistant_context_needs_the_assistant_read_scope(world):
    with refused("scope_denied"):
        world.service.context_for_assistant(
            world.ctx("ASSISTANT_CLAUDE", scopes={"own_proposals:read"}, credential_id=CRED,
                      browser_mutation_verified=False))
    with refused("channel_operation_denied"):
        world.service.context_for_assistant(world.ctx("MIA"))


def test_a_credential_sees_only_its_own_batch_status(world):
    world.credential(CRED)
    world.credential(CRED_B)
    mine = world.paste(batch(three_lines(), number=1), ctx=world.assistant(), source=CLAUDE)
    other = world.paste(batch(three_lines(), number=2, charge_date="2026-11-15"),
                        ctx=world.assistant("gpt", CRED_B), source="ASSISTANT_GPT")
    status = world.service.status_for_credential(world.assistant(), mine.proposal_id)
    assert status == {"proposal_id": mine.proposal_id, "status": "pending",
                      "updated_at": "2026-10-16T12:00:00.000Z"}
    with refused("batch_not_found"):
        world.service.status_for_credential(world.assistant(), other.proposal_id)
    with refused("batch_not_found"):
        world.service.status_for_credential(world.assistant(), "PRO_" + "0" * 32)
    pasted = world.paste(batch(three_lines(), number=3, charge_date="2026-12-15"))
    with refused("batch_not_found"):  # an owner paste has no credential
        world.service.status_for_credential(world.assistant(), pasted.proposal_id)
    world.reject(mine.proposal_id)
    world.advance(60)
    status = world.service.status_for_credential(world.assistant(), mine.proposal_id)
    assert status["status"] == "rejected" and status["updated_at"] == "2026-10-16T12:00:00.000Z"
    assert "approval_request_id" not in status and "rejection_request_id" not in status
