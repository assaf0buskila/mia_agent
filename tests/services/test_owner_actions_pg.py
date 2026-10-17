"""V2-28: Telegram approve/reject cards decided on the ledger (V2_PG_CONTRACTS.md section 13).

Built against the V2-16f FakeLedger and a stand-in for V2-19's authorize module. Every case runs
with ``assaf_os_backend=postgres``; the Sheets behaviour is covered by the unchanged suites.
"""

from __future__ import annotations

import threading

import pytest
from app.capabilities.types import Principal
from app.db.store import LeadStore
from app.domain.approvals import DECISION_APPROVED, DECISION_REJECTED
from app.services import owner_actions
from app.services.os_ledger import api
from app.services.owner_actions import (
    execute_approved_owner_action_with_adapters,
    execute_ledger_owner_decision,
    is_ledger_owner_action,
    read_owner_action,
    reject_assaf_os_owner_action,
)

from tests.services.os_v2_28_support import (
    OWNER,
    LedgerWorld,
    approve_row,
    committed_probe,
    ledger_owner_action,
    make_store_factory,
    pg_settings,
    provider_claims,
)

PRINCIPAL = Principal.owner(source="telegram", actor_id=OWNER)


@pytest.fixture
def factory(tmp_path):
    return make_store_factory(tmp_path)


@pytest.fixture
def store(factory):
    session = factory()
    yield LeadStore(session)
    session.close()


@pytest.fixture
def world(monkeypatch):
    return LedgerWorld().install(monkeypatch)


def _card(store, world, **kwargs):
    row = world.propose_lead(**kwargs)
    proposal = ledger_owner_action(store, world.settings, row)
    store.session.commit()
    return row, proposal


def _approve(store, world, proposal, *, settings=None):
    return execute_approved_owner_action_with_adapters(
        store,
        settings=settings or world.settings,
        principal=PRINCIPAL,
        proposal_id=proposal.proposal_id,
    )


def test_decision_uuid_travels_in_parameters_never_in_the_approval_id_column(store, world):
    row, proposal = _card(store, world)
    approval = store.get_approval_by_approval_id(proposal.approval_id)
    envelope = read_owner_action(approval)
    # approval_id is String(32): the Telegram callback token, never the 36-char ledger UUID.
    assert len(approval.approval_id) <= 32 and approval.approval_id != row.approval_id
    assert envelope["parameters"] == {
        "proposal_id": row.proposal_id,
        "request_id": row.approval_id,
        "rejection_request_id": row.rejection_id,
    }
    assert len(envelope["parameters"]["request_id"]) == 36
    assert api.canonical_uuid(envelope["parameters"]["request_id"]) == row.approval_id
    assert is_ledger_owner_action(envelope)


def test_approve_commits_the_cas_first_then_decides_once_with_the_stored_uuid(
    store, world, factory
):
    row, proposal = _card(store, world)
    world.authorize.committed_probe = committed_probe(factory)
    approve_row(store, proposal.approval_id)  # the CAS, not yet committed
    outcome = _approve(store, world, proposal)
    assert outcome.status == "executed"
    assert world.ledger.named("decide") == [("decide", "TELEGRAM_APPROVAL", row.approval_id)]
    assert ("from_telegram_decision", DECISION_APPROVED) in world.authorize.calls
    receipts = world.receipts()
    assert [(r["request_id"], r["status"]) for r in receipts] == [(row.approval_id, "committed")]
    approval = store.get_approval_by_approval_id(proposal.approval_id)
    assert approval.executed_at and approval.result == "committed"
    assert provider_claims(store) == []  # the ledger needs no provider claim


def test_reject_decides_with_the_stored_rejection_uuid(store, world, factory):
    row, proposal = _card(store, world)
    world.authorize.committed_probe = committed_probe(factory)
    approve_row(store, proposal.approval_id, DECISION_REJECTED)
    outcome = reject_assaf_os_owner_action(
        store, settings=world.settings, principal=PRINCIPAL, proposal_id=proposal.proposal_id
    )
    assert outcome.status == "executed"
    assert world.ledger.named("decide") == [("decide", "TELEGRAM_APPROVAL", row.rejection_id)]
    assert [(r["request_id"], r["status"]) for r in world.receipts()] == [
        (row.rejection_id, "rejected")
    ]
    assert world.ledger.rows("os_outbox") == []


def test_duplicate_execution_writes_once(store, world):
    row, proposal = _card(store, world)
    approve_row(store, proposal.approval_id)
    first = _approve(store, world, proposal)
    second = _approve(store, world, proposal)
    assert (first.status, second.status) == ("executed", "already_handled")
    assert len(world.ledger.named("decide")) == 1
    assert len(world.receipts()) == 1
    assert len(world.ledger.rows("os_outbox")) == 1


def test_crash_after_the_cas_recovers_with_the_same_decision_id(store, world):
    """The CAS committed and the process died before the ledger commit: nothing was written."""
    row, proposal = _card(store, world)
    approve_row(store, proposal.approval_id)
    store.session.commit()
    # A crash inside the ledger transaction rolls it back (the fake restores its snapshot).
    world.ledger.inject("before_commit", RuntimeError("process died"))
    crashed = _approve(store, world, proposal)
    assert crashed.status in {"unknown", "not_connected"}
    assert world.receipts() == []
    assert provider_claims(store) == []  # no sticky unknown for this kind
    recovered = _approve(store, world, proposal)
    assert recovered.status == "executed"
    decided_ids = {call[2] for call in world.ledger.named("decide")}
    assert decided_ids == {row.approval_id}  # a new decision id is never minted
    assert [(r["request_id"], r["status"]) for r in world.receipts()] == [
        (row.approval_id, "committed")
    ]


def test_crash_after_the_ledger_commit_recovers_by_receipt_without_deciding_again(store, world):
    row, proposal = _card(store, world)
    approve_row(store, proposal.approval_id)
    store.session.commit()
    # The ledger committed, then the process died before recording anything locally.
    world.ledger.decide(world.ctx("TELEGRAM_APPROVAL"), row.proposal_id, row.approval_id, "approve")
    decides = len(world.ledger.named("decide"))
    recovered = _approve(store, world, proposal)
    assert recovered.status == "executed"
    assert len(world.ledger.named("decide")) == decides  # recovery is a read
    assert world.ledger.named("receipt")[0][2] == row.approval_id
    assert len(world.receipts()) == 1


def test_unknown_commit_is_resolved_by_a_receipt_read_not_left_sticky(store, world):
    row, proposal = _card(store, world)
    approve_row(store, proposal.approval_id)
    world.ledger.inject("commit_unknown", RuntimeError("commit outcome lost"))
    outcome = _approve(store, world, proposal)
    assert outcome.status == "executed"
    assert len(world.ledger.named("decide")) == 1
    assert [r["request_id"] for r in world.receipts()] == [row.approval_id]
    assert provider_claims(store) == []


def test_concurrent_browser_and_telegram_approve_yield_one_receipt(factory, world):
    setup = LeadStore(factory())
    row, proposal = _card(setup, world)
    approve_row(setup, proposal.approval_id)
    setup.session.commit()
    setup.session.close()

    paused = world.ledger.pause("after_apply")
    results: dict[str, object] = {}

    def browser():
        results["browser"] = world.ledger.decide(
            world.ctx("OS"), row.proposal_id, row.approval_id, "approve"
        )

    def telegram():
        session = factory()
        try:
            results["telegram"] = _approve(LeadStore(session), world, proposal)
            session.commit()
        finally:
            session.close()

    first = threading.Thread(target=browser)
    first.start()
    assert paused.reached.wait(5)  # the browser holds the workspace lock mid-commit
    reading = threading.Event()
    world.ledger.before_read = reading.set
    second = threading.Thread(target=telegram)
    second.start()
    assert reading.wait(5)  # Telegram is now waiting behind the browser's lock
    paused.set()
    first.join(10)
    second.join(10)
    assert results["browser"].receipt.status == "committed"
    assert results["telegram"].status == "executed"
    assert len(world.receipts()) == 1
    assert len(world.ledger.rows("os_events")) == 1
    assert len(world.ledger.rows("os_outbox")) == 1


def test_telegram_decide_waiting_behind_a_browser_commit_replays_its_receipt(factory, world):
    """The other interleaving: Telegram has passed its reads and is inside decide."""
    setup = LeadStore(factory())
    row, proposal = _card(setup, world)
    approve_row(setup, proposal.approval_id)
    setup.session.commit()
    setup.session.close()

    paused = world.ledger.pause("after_apply")
    results: dict[str, object] = {}

    def browser():
        results["browser"] = world.ledger.decide(
            world.ctx("OS"), row.proposal_id, row.approval_id, "approve"
        )

    browser_thread = threading.Thread(target=browser)

    def start_browser_first() -> None:
        browser_thread.start()
        assert paused.reached.wait(5)
        threading.Timer(0.2, paused.set).start()  # Telegram then queues on the lock

    world.ledger.before_decide = start_browser_first
    session = factory()
    try:
        results["telegram"] = _approve(LeadStore(session), world, proposal)
        session.commit()
    finally:
        session.close()
    browser_thread.join(10)
    assert results["browser"].receipt.status == "committed"
    assert results["telegram"].status == "executed"
    channels = [call[1] for call in world.ledger.named("decide")]
    assert sorted(channels) == ["OS", "TELEGRAM_APPROVAL"]
    assert {call[2] for call in world.ledger.named("decide")} == {row.approval_id}
    assert len(world.receipts()) == 1
    assert len(world.ledger.rows("os_events")) == 1
    assert len(world.ledger.rows("os_outbox")) == 1


def test_browser_reject_first_makes_the_telegram_approve_terminal(store, world):
    row, proposal = _card(store, world)
    world.ledger.decide(world.ctx("OS"), row.proposal_id, row.rejection_id, "reject")
    approve_row(store, proposal.approval_id)
    outcome = _approve(store, world, proposal)
    assert outcome.status == "rejected"
    assert world.ledger.named("decide")[-1][1] == "OS"  # Telegram never decided
    assert [r["status"] for r in world.receipts()] == ["rejected"]
    assert store.get_approval_by_approval_id(proposal.approval_id).result == "terminal:rejected"


def test_a_changed_proposal_view_refuses_before_any_decide(store, world):
    from app.domain.owner.proposal_cards import ledger_proposal_body

    row = world.propose_lead()
    forged = ledger_proposal_body(row)
    forged["after"] = {**forged["after"], "name": "שם אחר"}
    proposal = ledger_owner_action(store, world.settings, row, body=forged)
    approve_row(store, proposal.approval_id)
    outcome = _approve(store, world, proposal)
    assert outcome.status == "target_changed"
    assert world.ledger.named("decide") == []
    assert world.receipts() == []


def test_the_view_ignores_status_so_a_replayed_receipt_is_reported_truthfully(store, world):
    row, proposal = _card(store, world)
    approve_row(store, proposal.approval_id)
    store.session.commit()
    world.ledger.decide(world.ctx("OS"), row.proposal_id, row.approval_id, "approve")
    assert _approve(store, world, proposal).status == "executed"
    assert len(world.receipts()) == 1


def test_kill_switch_refuses_before_the_ledger(store, world):
    _, proposal = _card(store, world)
    approve_row(store, proposal.approval_id)
    world.settings = pg_settings(kill_switch=True)
    outcome = _approve(store, world, proposal)
    assert outcome.status == "policy_denied"
    assert world.ledger.calls == [call for call in world.ledger.calls if call[0] == "propose"]
    assert world.receipts() == []


def test_missing_authorize_module_fails_closed_without_a_write(store, monkeypatch):
    world = LedgerWorld().install(monkeypatch, authorize=False)
    monkeypatch.setattr(
        owner_actions, "AUTHORIZE_MODULE", "app.services.os_ledger.authorize_absent_v2_28"
    )
    _, proposal = _card(store, world)
    approve_row(store, proposal.approval_id)
    outcome = _approve(store, world, proposal)
    assert outcome.status == "not_connected"
    assert world.receipts() == []
    assert not store.get_approval_by_approval_id(proposal.approval_id).executed_at


def test_a_ledger_card_is_never_decided_under_the_sheets_backend(store, world, monkeypatch):
    _, proposal = _card(store, world)
    approve_row(store, proposal.approval_id)
    sheets = pg_settings(assaf_os_backend="sheets")
    monkeypatch.setattr(
        "app.services.assaf_os.AssafOsPort",
        lambda *_a, **_k: pytest.fail("a ledger card must not reach Apps Script"),
    )
    outcome = _approve(store, world, proposal, settings=sheets)
    assert outcome.status == "not_connected"
    assert world.ledger.named("decide") == []


def test_a_sheets_card_is_never_decided_under_the_postgres_backend(store, world, monkeypatch):
    from app.services.owner_actions import propose_owner_action

    proposal = propose_owner_action(
        store,
        principal=PRINCIPAL,
        source_ref="telegram:sheets-card",
        kind="assaf_os.approve",
        parameters={
            "proposal_id": "PR_12345678901234567890",
            "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "rejection_request_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        },
        target={"approval_guard_version": 1, "proposal": {"status": "pending"}},
        ttl_seconds=600,
    )
    approve_row(store, proposal.approval_id)
    monkeypatch.setattr(
        "app.services.os_backend.get_os_backend",
        lambda *_a, **_k: pytest.fail("a Sheets card must not reach any backend"),
    )
    outcome = _approve(store, world, proposal)
    assert outcome.status == "not_connected"
    assert world.ledger.named("decide") == []
    assert provider_claims(store) == []


def test_the_decide_context_is_always_telegram_approval_never_mia(store, world):
    _, proposal = _card(store, world)
    approve_row(store, proposal.approval_id)
    _approve(store, world, proposal)
    decide_channels = {call[1] for call in world.ledger.named("decide")}
    read_channels = {call[1] for call in world.ledger.calls if call[0] != "decide"}
    assert decide_channels == {"TELEGRAM_APPROVAL"}
    assert "TELEGRAM_APPROVAL" not in read_channels
    with pytest.raises(api.LedgerError) as refused:
        world.ledger.decide(world.ctx("MIA"), "PRO_x", "0" * 8 + "-0000-4000-8000-" + "0" * 12,
                            "approve")
    assert refused.value.code == "owner_approval_required"


def test_execute_ledger_owner_decision_requires_the_matching_owner_decision(store, world):
    _, proposal = _card(store, world)
    pending = execute_ledger_owner_decision(
        store,
        settings=world.settings,
        principal=PRINCIPAL,
        proposal_id=proposal.proposal_id,
        decision="approve",
    )
    assert pending.status == "not_approved"
    stranger = execute_ledger_owner_decision(
        store,
        settings=world.settings,
        principal=Principal.owner(source="telegram", actor_id="999"),
        proposal_id=proposal.proposal_id,
        decision="approve",
    )
    assert stranger.status in {"unauthorized", "unbound"}
    assert world.ledger.named("decide") == []


def _reject(store, world, proposal):
    return reject_assaf_os_owner_action(
        store, settings=world.settings, principal=PRINCIPAL, proposal_id=proposal.proposal_id
    )


def _fail_next_read(world):
    def fail():
        raise api.LedgerError("ledger_unavailable")

    world.ledger.before_read = fail


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_an_outage_in_the_receipt_read_never_claims_a_no_write(store, world, decision):
    # Review r1 (#177): a decision committed, then the next tap's receipt read fails. The server
    # cannot prove a no-write, so the text must not say one; the next tap still converges.
    row, proposal = _card(store, world)
    if decision == "approve":
        approve_row(store, proposal.approval_id)
        decision_id = row.approval_id
    else:
        approve_row(store, proposal.approval_id, DECISION_REJECTED)
        decision_id = row.rejection_id
    store.session.commit()
    world.ledger.decide(world.ctx("TELEGRAM_APPROVAL"), row.proposal_id, decision_id, decision)
    _fail_next_read(world)
    run = _approve if decision == "approve" else _reject
    outcome = run(store, world, proposal)
    assert outcome.status == "not_connected"
    assert "לא נכתבה רשומה" not in outcome.text and "ממתינה עד שתפוג" not in outcome.text
    assert "לא נשלחה" in outcome.text
    again = run(store, world, proposal)
    assert again.status == "executed"
    assert len(world.receipts()) == 1


def test_a_definite_refusal_after_a_clean_receipt_read_may_say_nothing_was_written(
    store, world, monkeypatch
):
    # The receipt read succeeded and found nothing, and the decide was refused definitely, so
    # "no record was written" is provable here.
    row, proposal = _card(store, world)
    approve_row(store, proposal.approval_id)
    store.session.commit()

    def refuse(*_args, **_kwargs):
        raise api.LedgerError("ledger_unavailable")

    monkeypatch.setattr(world.ledger, "decide", refuse)
    outcome = _approve(store, world, proposal)
    assert outcome.status == "not_connected"
    assert "לא נכתבה רשומה" in outcome.text
    assert world.receipts() == []
