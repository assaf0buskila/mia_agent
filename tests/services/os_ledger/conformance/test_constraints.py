"""Client-reachable constraints return their business code, never an invariant (§5.3).

A caller bug trips ``fake_invariant:*`` (fake) or ``ledger_integrity_failed`` (PG). A constraint a
client input can reach is pre-checked by the shared core logic and mapped to a business code first.
"""

from datetime import datetime, timedelta

import pytest
from app.services.os_ledger import api

from tests.fakes.os_ledger_builders import (
    CLIENT_INACTIVE,
    LEAD_CONVERTED,
    TASK_OPEN,
    client,
    commit_direct,
    commit_import,
    ctx,
    current,
    expense,
    fresh_workspace,
    lead,
    payment,
    propose_task,
    rid,
    task,
    update,
)


def propose(env, entity, input, record_id=None, revision=0, channel="OS", request_id=None):
    return env.propose(ctx(channel), api.ProposeInput(
        rid() if request_id is None else request_id, entity, record_id, revision, input))


def expense_input(source_key="key-1", **over):
    return {k: v for k, v in expense(source_key=source_key, **over).after.items()
            if k not in {"expense_id", "revision"}}


# --- expense source key -----------------------------------------------------------------------


def test_a_duplicate_expense_source_key_in_a_proposal_is_validation_error(ledger):
    commit_direct(ledger, ctx(), [expense()])
    with pytest.raises(api.LedgerError) as caught:
        propose(ledger, "expenses", expense_input())
    assert caught.value.code == "validation_error"
    assert caught.value.details["reason"] == "duplicate_expense_source"
    assert len(ledger.rows("os_proposals")) == 1  # only the setup commit


def test_a_duplicate_expense_source_key_in_apply_is_target_changed(ledger):
    commit_direct(ledger, ctx(), [expense()])
    with pytest.raises(api.LedgerError) as caught:
        commit_direct(ledger, ctx(), [expense("EXP_e2")])
    assert caught.value.code == "target_changed"
    assert len(ledger.rows("os_expenses")) == 1


def test_an_expense_that_took_the_key_before_approval_gives_a_target_changed_receipt(ledger):
    proposal = propose(ledger, "expenses", expense_input("key-9"))
    commit_direct(ledger, ctx(), [expense("EXP_other", source_key="key-9")])
    result = ledger.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    assert result.receipt.status == "target_changed"
    assert [r["record_id"] for r in ledger.rows("os_expenses")] == ["EXP_other"]


def test_a_tombstoned_expense_still_holds_its_source_key(ledger):
    fresh_workspace(ledger, "ws-i")
    context = ctx(workspace="ws-i")
    first = expense()
    commit_import(ledger, context, [first], api.digest({"m": 1}))
    commit_import(ledger, context, [api.Mutation(
        "expenses", "EXP_e1", "tombstone", first.after,
        {**first.after, "revision": 2})], api.digest({"m": 2}))
    with pytest.raises(api.LedgerError) as caught:
        commit_import(ledger, context, [expense("EXP_e2")], api.digest({"m": 3}))
    assert caught.value.code == "target_changed"


# --- lead person_id and contact ---------------------------------------------------------------


def test_a_second_lead_with_the_same_person_id_is_lead_already_exists(ledger):
    commit_direct(ledger, ctx(), [lead()])
    with pytest.raises(api.LedgerError, match="^lead_already_exists$"):
        commit_direct(ledger, ctx(), [lead("LEA_l2", person_id="PER_l1", phone="0529999999")])
    assert len(ledger.rows("os_leads")) == 1


def test_a_lead_with_the_same_contact_in_a_proposal_is_lead_already_exists(ledger):
    commit_direct(ledger, ctx(), [lead()])
    lead_input = {k: v for k, v in lead("LEA_x").after.items()
                  if k not in {"lead_id", "revision", "person_id"}}
    with pytest.raises(api.LedgerError, match="^lead_already_exists$"):
        propose(ledger, "leads", lead_input)
    assert len(ledger.rows("os_proposals")) == 1  # only the setup commit


def test_promote_lead_creates_the_client_with_the_leads_person_id(ledger):
    commit_direct(ledger, ctx(), [lead()])
    proposal = propose(ledger, "promote_lead", {}, record_id="LEA_l1", revision=1)
    assert proposal.entity == "promote_lead" and proposal.record_id == "LEA_l1"
    result = ledger.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    assert result.receipt.status == "committed"
    assert sorted(r["entity"] for r in result.receipt.records) == ["clients", "leads"]
    assert current(ledger, "leads", "LEA_l1")["status"] == LEAD_CONVERTED
    assert current(ledger, "clients", "PER_l1")["name"] == "Lead"


# --- domain validation ------------------------------------------------------------------------


@pytest.mark.parametrize("entity,input,reason", [
    ("tasks", {"status": TASK_OPEN}, "missing_title"),
    ("tasks", {"title": "T", "status": TASK_OPEN, "bogus": 1}, "non_input_field"),
    ("tasks", {"title": "T", "status": "no-such-status"}, "invalid_status"),
    ("nothing", {"title": "T"}, "invalid_entity"),
])
def test_invalid_input_is_validation_error_with_a_reason(ledger, entity, input, reason):
    with pytest.raises(api.LedgerError) as caught:
        propose(ledger, entity, input)
    assert caught.value.code == "validation_error"
    assert caught.value.details["reason"] == reason
    assert ledger.rows("os_proposals") == []


def test_a_reference_to_a_missing_record_is_validation_error_in_a_proposal(ledger):
    body = {k: v for k, v in payment().after.items() if k not in {"payment_id", "revision"}}
    with pytest.raises(api.LedgerError) as caught:
        propose(ledger, "payments", body)
    assert caught.value.code == "validation_error"
    assert caught.value.details["reason"] == "client_not_found"


def test_a_reference_to_a_missing_record_is_target_changed_in_apply(ledger):
    with pytest.raises(api.LedgerError) as caught:
        commit_direct(ledger, ctx(), [payment()])
    assert caught.value.code == "target_changed"
    assert caught.value.details["reason"] == "client_not_found"
    assert ledger.rows("os_payments") == []


@pytest.mark.parametrize("record_id,revision", [("TAS_t1", 5), ("TAS_missing", 1)])
def test_a_stale_or_missing_target_is_target_changed_at_propose(ledger, record_id, revision):
    commit_direct(ledger, ctx(), [task()])
    with pytest.raises(api.LedgerError, match="^target_changed$"):
        propose(ledger, "tasks", {"title": "X"}, record_id=record_id, revision=revision)


def test_a_create_with_a_nonzero_revision_is_target_changed_at_propose(ledger):
    with pytest.raises(api.LedgerError, match="^target_changed$"):
        propose(ledger, "tasks", {"title": "X", "status": TASK_OPEN}, revision=2)


@pytest.mark.parametrize("bad", ("", "not-a-uuid", "12345678-1234-1234-1234-123456789ABC"))
def test_a_bad_request_id_is_invalid_request_id(ledger, bad):
    with pytest.raises(api.LedgerError, match="^invalid_request_id$"):
        propose(ledger, "tasks", {"title": "X", "status": TASK_OPEN}, request_id=bad)
    assert ledger.rows("os_proposals") == []


def test_a_decision_with_a_bad_id_is_invalid_request_id(ledger):
    proposal = propose_task(ledger)
    with pytest.raises(api.LedgerError, match="^invalid_request_id$"):
        ledger.decide(ctx(), proposal.proposal_id, "nope", "approve")


def test_an_approval_expiry_in_the_past_or_without_a_zone_is_validation_error(ledger):
    for expiry in (ledger.now() - timedelta(seconds=1), datetime(2030, 1, 1)):
        with pytest.raises(api.LedgerError) as caught:
            ledger.propose(ctx(), api.ProposeInput(
                rid(), "tasks", None, 0, {"title": "X", "status": TASK_OPEN},
                approval_expires_at=expiry))
        assert caught.value.code == "validation_error"


def test_an_outer_approval_expiry_can_only_shorten_the_ten_minutes(ledger):
    short = ledger.now() + timedelta(seconds=60)
    long = ledger.now() + timedelta(days=1)
    first = ledger.propose(ctx(), api.ProposeInput(
        rid(), "tasks", None, 0, {"title": "X", "status": TASK_OPEN}, approval_expires_at=short))
    second = ledger.propose(ctx(), api.ProposeInput(
        rid(), "tasks", None, 0, {"title": "X", "status": TASK_OPEN}, approval_expires_at=long))
    assert first.expires_at - ledger.now() <= timedelta(seconds=60)
    assert second.expires_at - ledger.now() <= timedelta(minutes=10)


def test_a_changed_referenced_record_gives_a_target_changed_receipt(ledger):
    commit_direct(ledger, ctx(), [client(), task()])
    body = {"title": "Linked", "status": TASK_OPEN, "person_id": "PER_c1"}
    proposal = propose(ledger, "tasks", body)
    before = current(ledger, "clients", "PER_c1")
    commit_direct(ledger, ctx(), [update(before, status=CLIENT_INACTIVE)])
    # The referenced client changed: the dependency digest differs, so approval is target_changed.
    result = ledger.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    assert result.receipt.status == "target_changed"
