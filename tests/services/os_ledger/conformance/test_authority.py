"""Authority state machine and the epoch (§5.4; decisions D1, D2, D3), and I12."""

from contextlib import contextmanager

import pytest
from app.services.os_ledger import api

from tests.fakes.os_ledger_builders import (
    commit_direct,
    commit_import,
    ctx,
    fresh_workspace,
    propose_task,
    task,
    workspace_row,
)
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE

STATES = ("shadow", "active", "fenced", "restoring")
LEGAL = (("shadow", "active"), ("shadow", "fenced"), ("active", "fenced"), ("fenced", "restoring"))
BUMPING = (("fenced", "shadow"), ("restoring", "active"))
ILLEGAL = tuple((a, b) for a in STATES for b in STATES
                if a != b and (a, b) not in LEGAL + BUMPING)
FINGERPRINT = "ab" * 32


def authority_rows(env, name):
    return {r["domain"]: r for r in env.rows("os_authority") if r["workspace_id"] == name}


def go(env, name, to, *, domain="core", epoch=1, owner=OWNER_ID):
    return env.transition(name, domain, to, expected_epoch=epoch, actor_owner_id=owner)


@contextmanager
def refused(reason):
    with pytest.raises(api.LedgerError) as caught:
        yield
    assert caught.value.code == "authority_changed"
    assert caught.value.reason == reason


def test_ensure_workspace_creates_epoch_one_with_both_domains_in_shadow(ledger):
    snapshot = ledger.ensure_workspace("fresh", OWNER_ID, 1)
    assert (snapshot.workspace_id, snapshot.epoch, snapshot.version) == ("fresh", 1, 0)
    assert dict(snapshot.states) == {"core": "shadow", "statements": "shadow"}
    assert dict(snapshot.activated_at) == {"core": None, "statements": None}
    row = workspace_row(ledger, "fresh")
    assert (row["owner_id"], row["epoch"], row["version"], row["pending_seq"]) == (
        OWNER_ID, 1, 0, 0)
    assert row["statement_pepper_fp"] is None
    rows = authority_rows(ledger, "fresh")
    assert {d: (r["state"], r["epoch"]) for d, r in rows.items()} == {
        "core": ("shadow", 1), "statements": ("shadow", 1)}
    assert ledger.state("fresh") == snapshot


def test_ensure_workspace_equal_repeat_is_a_no_op(ledger):
    first = ledger.ensure_workspace("fresh", OWNER_ID, 1)
    assert ledger.ensure_workspace("fresh", OWNER_ID, 1) == first
    assert len([r for r in ledger.rows("os_workspaces") if r["workspace_id"] == "fresh"]) == 1


@pytest.mark.parametrize("owner,epoch", (("999", 1), (OWNER_ID, 2)))
def test_ensure_workspace_with_a_different_owner_or_epoch_is_authority_changed(
        ledger, owner, epoch):
    ledger.ensure_workspace("fresh", OWNER_ID, 1)
    with pytest.raises(api.LedgerError, match="^authority_changed$"):
        ledger.ensure_workspace("fresh", owner, epoch)
    assert ledger.state("fresh").epoch == 1


@pytest.mark.parametrize("call", ("state", "transition", "bump_epoch", "pin_statement_pepper"))
def test_admin_calls_on_a_missing_workspace_are_ledger_unavailable(ledger, call):
    calls = {
        "state": lambda: ledger.state("nowhere"),
        "transition": lambda: go(ledger, "nowhere", "active"),
        "bump_epoch": lambda: ledger.bump_epoch("nowhere", expected_epoch=1),
        "pin_statement_pepper": lambda: ledger.pin_statement_pepper("nowhere", FINGERPRINT),
    }
    with pytest.raises(api.LedgerError) as caught:
        calls[call]()
    assert caught.value.code == "ledger_unavailable"


@pytest.mark.parametrize("source,target", LEGAL)
@pytest.mark.parametrize("domain", ("core", "statements"))
def test_legal_transitions_never_bump_the_epoch(ledger, source, target, domain):
    fresh_workspace(ledger, "ws", **{domain: source})
    version_before = ledger.version("ws")
    snapshot = go(ledger, "ws", target, domain=domain)
    assert snapshot.states[domain] == target and snapshot.epoch == 1
    assert (workspace_row(ledger, "ws")["epoch"], ledger.version("ws")) == (1, version_before)
    assert {d: r["epoch"] for d, r in authority_rows(ledger, "ws").items()} == {
        "core": 1, "statements": 1}
    # activation is a fact that later states keep (fenced and restoring stay activated)
    assert (snapshot.activated_at[domain] is not None) == ("active" in (source, target))
    assert ledger.state("ws") == snapshot


@pytest.mark.parametrize("source,target", BUMPING)
@pytest.mark.parametrize("domain", ("core", "statements"))
def test_the_two_bumping_edges_set_all_three_epochs_atomically(ledger, source, target, domain):
    fresh_workspace(ledger, "ws", **{domain: source})
    snapshot = go(ledger, "ws", target, domain=domain)
    assert snapshot.epoch == 2 and snapshot.states[domain] == target
    assert workspace_row(ledger, "ws")["epoch"] == 2
    assert {d: r["epoch"] for d, r in authority_rows(ledger, "ws").items()} == {
        "core": 2, "statements": 2}
    assert (snapshot.activated_at[domain] is not None) == (target == "active")


@pytest.mark.parametrize("source,target", BUMPING)
def test_a_repeated_bumping_edge_at_the_new_epoch_is_a_no_op(ledger, source, target):
    fresh_workspace(ledger, "ws", core=source)
    first = go(ledger, "ws", target)
    again = go(ledger, "ws", target)  # the caller still believes epoch 1
    assert again == first and again.epoch == 2
    assert workspace_row(ledger, "ws")["epoch"] == 2


@pytest.mark.parametrize("source,target", ILLEGAL)
def test_every_other_edge_is_refused(ledger, source, target):
    fresh_workspace(ledger, "ws", core=source)
    with refused("transition_refused"):
        go(ledger, "ws", target)
    assert ledger.state("ws").states["core"] == source and ledger.state("ws").epoch == 1


@pytest.mark.parametrize("state", STATES)
def test_a_transition_to_the_current_state_is_a_no_op(ledger, state):
    fresh_workspace(ledger, "ws", core=state)
    before = ledger.state("ws")
    assert go(ledger, "ws", state) == before


def test_there_is_no_fenced_to_active_edge(ledger):
    fresh_workspace(ledger, "ws", core="fenced")
    with refused("transition_refused"):
        go(ledger, "ws", "active")


@pytest.mark.parametrize("wrong", ("epoch", "owner"))
def test_transition_checks_the_expected_epoch_and_actor(ledger, wrong):
    fresh_workspace(ledger, "ws")
    with refused("epoch_mismatch"):
        go(ledger, "ws", "active", epoch=2 if wrong == "epoch" else 1,
           owner="999" if wrong == "owner" else OWNER_ID)
    assert ledger.state("ws").states["core"] == "shadow"


def test_fenced_to_shadow_is_refused_while_a_non_import_receipt_exists(ledger):
    commit_direct(ledger, ctx(), [task()])
    go(ledger, WORKSPACE, "fenced")
    with refused("transition_refused"):
        go(ledger, WORKSPACE, "shadow")
    assert ledger.state(WORKSPACE).epoch == 1


def test_fenced_to_shadow_is_allowed_when_only_import_receipts_exist(ledger):
    fresh_workspace(ledger, "ws-i")
    commit_import(ledger, ctx(workspace="ws-i"), [task()], api.digest({"m": 1}))
    go(ledger, "ws-i", "active")
    go(ledger, "ws-i", "fenced")
    snapshot = go(ledger, "ws-i", "shadow")
    assert snapshot.epoch == 2 and snapshot.states["core"] == "shadow"


def test_the_receipt_guard_looks_only_at_the_current_epoch(ledger):
    commit_direct(ledger, ctx(), [task()])  # a non-import receipt at epoch 1
    go(ledger, WORKSPACE, "fenced")
    go(ledger, WORKSPACE, "restoring")
    assert go(ledger, WORKSPACE, "active").epoch == 2
    go(ledger, WORKSPACE, "fenced", epoch=2)
    assert go(ledger, WORKSPACE, "shadow", epoch=2).epoch == 3


@pytest.mark.parametrize("state", STATES)
def test_bump_epoch_sets_all_three_epochs_and_has_no_state_precondition(ledger, state):
    fresh_workspace(ledger, "ws", core=state)
    snapshot = ledger.bump_epoch("ws", expected_epoch=1)
    assert snapshot.epoch == 2 and snapshot.states["core"] == state
    assert workspace_row(ledger, "ws")["epoch"] == 2
    assert {d: r["epoch"] for d, r in authority_rows(ledger, "ws").items()} == {
        "core": 2, "statements": 2}


def test_bump_epoch_is_idempotent_at_the_new_epoch(ledger):
    first = ledger.bump_epoch(WORKSPACE, expected_epoch=1)
    assert ledger.bump_epoch(WORKSPACE, expected_epoch=1) == first
    assert ledger.state(WORKSPACE).epoch == 2


@pytest.mark.parametrize("expected", (2, 3, 7))
def test_bump_epoch_with_any_other_expected_epoch_is_authority_changed(ledger, expected):
    with pytest.raises(api.LedgerError, match="^authority_changed$"):
        ledger.bump_epoch(WORKSPACE, expected_epoch=expected)
    assert ledger.state(WORKSPACE).epoch == 1


def test_a_bump_invalidates_every_context_of_the_old_epoch(ledger):
    ledger.bump_epoch(WORKSPACE, expected_epoch=1)
    ledger.knobs.settings_epoch = 2
    with pytest.raises(api.LedgerError, match="^authority_changed$"):
        ledger.head(ctx(epoch=1))
    with pytest.raises(api.LedgerError, match="^authority_changed$"):
        with ledger.transaction(ctx(epoch=1), domain="core", kind="owner_direct"):
            pass
    assert ledger.head(ctx(epoch=2)).authority_epoch == 2


def test_a_pending_proposal_of_an_older_epoch_is_finished_expired_not_decided(ledger):
    proposal = propose_task(ledger)
    go(ledger, WORKSPACE, "fenced")
    go(ledger, WORKSPACE, "restoring")
    go(ledger, WORKSPACE, "active")  # bumps to epoch 2
    ledger.knobs.settings_epoch = 2
    context = ctx(epoch=2)
    version_before = ledger.version()
    result = ledger.decide(context, proposal.proposal_id, proposal.approval_id, "approve")
    receipt = result.receipt
    assert receipt.status == "expired" and receipt.reason_codes == ("epoch_changed",)
    assert receipt.epoch == 1 and receipt.request_id == proposal.rejection_id
    assert result.records == ()
    assert ledger.version() == version_before + 1
    assert ledger.rows("os_tasks") == [] and ledger.rows("os_outbox") == []
    assert len(ledger.rows("os_events")) == 1


def test_a_receipt_of_another_epoch_is_never_read_at_the_new_epoch(ledger):
    _, receipt = commit_direct(ledger, ctx(), [task()])
    go(ledger, WORKSPACE, "fenced")
    go(ledger, WORKSPACE, "restoring")
    go(ledger, WORKSPACE, "active")
    ledger.knobs.settings_epoch = 2
    with pytest.raises(api.LedgerError, match="^authority_changed$"):
        ledger.receipt(ctx(epoch=2), receipt.request_id)


def test_state_reports_the_workspace_version_and_activation_time(ledger):
    snapshot = ledger.state(WORKSPACE)
    assert snapshot.states["core"] == "active" and snapshot.activated_at["core"] is not None
    assert snapshot.activated_at["statements"] is None and snapshot.version == 0
    commit_direct(ledger, ctx(), [task()])
    assert ledger.state(WORKSPACE).version == 1


def test_statement_pepper_is_write_once(ledger):
    pinned = ledger.pin_statement_pepper(WORKSPACE, FINGERPRINT)
    assert workspace_row(ledger)["statement_pepper_fp"] == FINGERPRINT
    assert ledger.pin_statement_pepper(WORKSPACE, FINGERPRINT) == pinned
    with pytest.raises(api.LedgerError) as caught:
        ledger.pin_statement_pepper(WORKSPACE, "cd" * 32)
    assert caught.value.code == "ledger_unavailable"
    assert caught.value.reason == "statement_pepper_mismatch"
    assert workspace_row(ledger)["statement_pepper_fp"] == FINGERPRINT
