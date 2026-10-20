"""Prototype ledger behaviours, ported onto the frozen ledger API (V2-16).

Each case re-states a scenario of ``tests/prototypes/test_assaf_os_ledger.py`` or
``test_assaf_os_primary.py`` (named in its docstring) against ``api.Ledger``, through the shared
``ledger`` fixture: it runs on the FakeLedger everywhere and on PostgreSQL whenever the conformance
suite does. The prototype-only parts (its own tables, principals, capacity cap and adapter) have no
counterpart in the v2 API and are covered by the conformance suite and ``test_core_*.py`` instead.
These cases live outside ``conformance/``, so they never count toward ``MIN_PG_CASES``.
"""

from datetime import date

import pytest
from app.services.os_ledger import api

from tests.fakes.os_ledger_builders import (
    CLIENT_INACTIVE,
    LEAD_CONVERTED,
    PAYMENT_REFUND,
    TASK_OPEN,
    client,
    commit_direct,
    counts,
    ctx,
    current,
    expense,
    fresh_workspace,
    lead,
    payment,
    propose_task,
    rid,
    task,
    time_entry,
    update,
    work,
)
from tests.fakes.os_ledger_env import OWNER_ID

MAX_SAFE = 9_007_199_254_740_991


def body(mutation, *drop):
    system = {api.ID_KEYS[mutation.entity], "revision", *drop}
    return {k: v for k, v in mutation.after.items() if k not in system}


def propose(env, entity, values, record_id=None, revision=0, context=None):
    return env.propose(context or ctx(), api.ProposeInput(rid(), entity, record_id, revision,
                                                          values))


def approve(env, proposal, context=None):
    return env.decide(context or ctx(), proposal.proposal_id, proposal.approval_id, "approve")


def test_all_seven_families_commit_exact_receipts_and_outbox_refs(ledger):
    """test_all_seven_families_proposals_commit_exact_receipts_and_outbox."""
    commit_direct(ledger, ctx(), [client(), work()])
    families = [
        ("clients", body(client("PER_x"))),
        ("works", body(work("WOR_x"))),
        ("payments", body(payment("PAY_x"))),
        ("time", body(time_entry("TIM_x"))),
        ("expenses", body(expense("EXP_x", source_key="seven"))),
        ("tasks", body(task("TAS_x"))),
        ("leads", body(lead("LEA_x", phone="0527777777"), "person_id")),
    ]
    for entity, values in families:
        proposal = propose(ledger, entity, values)
        assert current(ledger, entity, proposal.record_id) is None  # no effect before approval
        result = approve(ledger, proposal)
        receipt = result.receipt
        assert (receipt.status, receipt.kind) == ("committed", "proposal")
        assert [(r["entity"], r["record_id"], r["revision"]) for r in receipt.records] == [
            (entity, proposal.record_id, 1)]
        assert ledger.rows("os_outbox")[-1]["body"] == {
            "authority_epoch": 1,
            "refs": [{"entity": entity, "record_id": proposal.record_id, "revision": 1}]}
        assert current(ledger, entity, proposal.record_id)["revision"] == 1
    assert ledger.version() == 1 + len(families)


def test_a_long_unicode_expense_source_commits_and_deduplicates_exactly(ledger):
    """test_long_unicode_expense_source_commits_and_deduplicates_exactly."""
    key = "ק" * 1999 + "א"
    commit_direct(ledger, ctx(), [expense("EXP_long", source_key=key)])
    with pytest.raises(api.LedgerError) as caught:
        propose(ledger, "expenses", body(expense("EXP_dup", source_key=key)))
    assert caught.value.details["reason"] == "duplicate_expense_source"
    near = "ק" * 1999 + "ב"
    commit_direct(ledger, ctx(), [expense("EXP_near", source_key=near)])
    assert sorted(r["record_id"] for r in ledger.rows("os_expenses")) == ["EXP_long", "EXP_near"]


def test_promotion_keeps_an_existing_clients_values_and_bumps_its_revision(ledger):
    """test_promotion_preserves_existing_client_values_and_native_revision_semantics."""
    commit_direct(ledger, ctx(), [lead(), client("PER_l1", name="Kept", status=CLIENT_INACTIVE)])
    proposal = propose(ledger, "promote_lead", {}, record_id="LEA_l1", revision=1)
    assert approve(ledger, proposal).receipt.status == "committed"
    promoted = current(ledger, "clients", "PER_l1")
    assert (promoted["name"], promoted["status"], promoted["revision"]) == (
        "Kept", CLIENT_INACTIVE, 2)
    assert current(ledger, "leads", "LEA_l1")["status"] == LEAD_CONVERTED


def test_competing_refunds_cannot_overdraw_the_original(ledger):
    """test_competing_refunds_cannot_overdraw_original."""
    commit_direct(ledger, ctx(), [client(), work(), payment()])
    refund = {**body(payment("PAY_r")), "kind": PAYMENT_REFUND, "original_payment_id": "PAY_p1",
              "actual_amount_ils": 600}
    first, second = propose(ledger, "payments", refund), propose(ledger, "payments", refund)
    assert approve(ledger, first).receipt.status == "committed"
    assert approve(ledger, second).receipt.status == "target_changed"
    refunds = [r for r in ledger.rows("os_payments") if r["record_id"] != "PAY_p1"]
    assert [int(r["amount_minor"]) for r in refunds] == [600]


def test_competing_duplicate_sources_and_contacts_are_fenced(ledger):
    """test_competing_duplicate_sources_and_contacts_are_fenced."""
    source = body(expense("EXP_s", source_key="same"))
    contact = body(lead("LEA_c", phone="0541111111"), "person_id")
    pairs = [(propose(ledger, "expenses", source), propose(ledger, "expenses", source)),
             (propose(ledger, "leads", contact), propose(ledger, "leads", contact))]
    for first, second in pairs:
        assert approve(ledger, first).receipt.status == "committed"
        assert approve(ledger, second).receipt.status == "target_changed"
    assert len(ledger.rows("os_expenses")) == 1 and len(ledger.rows("os_leads")) == 1


def test_editing_the_original_payment_cannot_invalidate_its_refunds(ledger):
    """test_edit_original_payment_cannot_invalidate_existing_refunds."""
    commit_direct(ledger, ctx(), [client(), work(), payment(), payment(
        "PAY_r1", kind=PAYMENT_REFUND, original_payment_id="PAY_p1", actual_amount_ils=600)])
    with pytest.raises(api.LedgerError) as caught:
        commit_direct(ledger, ctx(), [update(current(ledger, "payments", "PAY_p1"),
                                             actual_amount_ils=500)])
    assert (caught.value.code, caught.value.details["reason"]) == (
        "target_changed", "refund_exceeds_original")
    assert current(ledger, "payments", "PAY_p1")["actual_amount_ils"] == 1000


def test_an_unrelated_commit_does_not_expire_exact_dependencies(ledger):
    """test_unrelated_commit_does_not_expire_exact_dependencies."""
    proposal = propose_task(ledger)
    commit_direct(ledger, ctx(), [expense()])
    assert approve(ledger, proposal).receipt.status == "committed"


def test_a_lost_response_recovers_the_same_receipt_without_reapplying(ledger):
    """test_lost_post_commit_response_recovers_same_receipt_without_reapply."""
    proposal = propose_task(ledger)
    first = approve(ledger, proposal)
    before = counts(ledger)
    again = approve(ledger, proposal)
    assert again.receipt == first.receipt and counts(ledger) == before
    recovered = ledger.receipt(ctx(), proposal.approval_id)
    assert recovered.found and recovered.receipt == first.receipt
    assert [record for _, record in recovered.records] == [
        current(ledger, "tasks", proposal.record_id)]


@pytest.mark.parametrize("entity,values,reason", [
    ("payments", {**body(payment()), "actual_amount_ils": 0}, "invalid_actual_amount_ils"),
    ("payments", {**body(payment()), "work_id": "WOR_missing"}, "work_not_found"),
    ("tasks", {"title": "T", "status": TASK_OPEN, "person_id": "PER_missing"},
     "client_not_found"),
])
def test_references_and_money_fail_before_any_proposal_or_effect(ledger, entity, values, reason):
    """test_references_and_money_fail_before_proposal_or_effect."""
    commit_direct(ledger, ctx(), [client(), work()])
    before = counts(ledger)
    with pytest.raises(api.LedgerError) as caught:
        propose(ledger, entity, values)
    assert (caught.value.code, caught.value.details["reason"]) == ("validation_error", reason)
    assert counts(ledger) == before


def test_workspaces_are_isolated(ledger):
    """test_workspace_isolation."""
    fresh_workspace(ledger, "ws-b", core="active")
    other = ctx(workspace="ws-b")
    _, mine = commit_direct(ledger, ctx(), [task("TAS_a")])
    commit_direct(ledger, other, [task("TAS_b"), task("TAS_c")])
    assert [r["task_id"] for r in ledger.page(ctx(), "tasks", after=None, limit=9).records] == [
        "TAS_a"]
    assert ledger.page(other, "tasks", after=None, limit=9).total == 2
    assert not ledger.receipt(other, mine.request_id).found
    with pytest.raises(api.LedgerError, match="^authority_changed$"):
        ledger.head(ctx(workspace="ws-b", owner=999))
    assert ledger.version() == 1 and ledger.version("ws-b") == 1


def test_a_proposal_is_detached_and_has_no_business_effect(ledger):
    """test_proposal_is_detached_immutable_and_no_business_effect."""
    proposal = propose_task(ledger, title="Original")
    proposal.mutations[0].after["title"] = "Mutated by the caller"
    stored = ledger.creation_request(ctx(), proposal.request_id).proposal
    assert stored.mutations[0].after["title"] == "Original"
    assert ledger.rows("os_tasks") == [] and ledger.version() == 0


def test_a_business_date_round_trips_without_a_time_zone_shift(ledger):
    """test_business_date_is_postgresql_date_without_timezone_shift."""
    commit_direct(ledger, ctx(), [client(), work(), payment(date="2026-12-31")])
    [row] = ledger.rows("os_payments")
    assert row["business_date"] == date(2026, 12, 31)
    [record] = ledger.page(ctx(), "payments", after=None, limit=5).records
    assert record["date"] == "2026-12-31"


def test_a_summary_overflow_fails_closed_at_propose_and_at_apply(ledger):
    """test_summary_overflow_is_sanitized_and_competing_payments_fail_closed."""
    commit_direct(ledger, ctx(), [client(), work(), payment(actual_amount_ils=MAX_SAFE)])
    second = {**body(payment("PAY_2")), "actual_amount_ils": 1}
    with pytest.raises(api.LedgerError) as caught:
        propose(ledger, "payments", second)
    assert (caught.value.code, caught.value.details["reason"]) == (
        "validation_error", "amount_overflow")
    with pytest.raises(api.LedgerError) as caught:
        commit_direct(ledger, ctx(), [payment("PAY_2", actual_amount_ils=1)])
    assert (caught.value.code, caught.value.details["reason"]) == (
        "target_changed", "amount_overflow")
    assert [row["paid_income_ils"] for row in ledger.monthly_summary(ctx())] == [MAX_SAFE]


def test_the_owner_of_a_workspace_is_numeric_and_exact(ledger):
    """test_wrong_owner_and_model_cannot_approve (owner half; channels are conformance)."""
    proposal = propose_task(ledger)
    with pytest.raises(api.LedgerError, match="^authority_changed$"):
        approve(ledger, proposal, ctx(owner=int(OWNER_ID) + 1))
    assert ledger.rows("os_receipts") == []
