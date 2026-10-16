"""The fake's own guarantees: every ``fake_invariant:*`` assertion, plus fake-only behaviour.

Shared behaviour belongs in ``tests/services/os_ledger/conformance``. These tests reach into the
fake (hooks, internals, a patched domain validator) and so never run against PG.
"""

import ast
import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from app.services.os_ledger import api

from tests.fakes import os_ledger as fake_module
from tests.fakes.contexts import for_worker
from tests.fakes.os_ledger import FakeLedger
from tests.fakes.os_ledger_builders import (
    PAYMENT_REFUND,
    abandon_draft,
    client,
    commit_direct,
    ctx,
    current,
    direct_draft,
    fresh_workspace,
    payment,
    propose_task,
    rid,
    task,
    time_entry,
    tombstone,
    update,
    work,
)
from tests.fakes.os_ledger_env import NOW, OWNER_ID, WORKSPACE, LedgerEnv, build_fake_env

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env() -> LedgerEnv:
    result = build_fake_env()
    result.ensure_workspace(WORKSPACE, OWNER_ID, 1)
    result.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    return result


@pytest.fixture
def fake(env) -> FakeLedger:
    return env._impl


def violation(name):
    return pytest.raises(AssertionError, match=f"^fake_invariant:{name}$")


def import_ctx():
    return for_worker("import", workspace="ws-i")


def import_tx(env):
    return env.transaction(import_ctx(), domain="core", kind="import")


# --- every invariant name in the fake has a test ----------------------------------------------


def invariant_names():
    tree = ast.parse((ROOT / "fakes" / "os_ledger.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_check":
            last = node.args[-1]
            if isinstance(last, ast.Constant):
                names.add(last.value)
            elif isinstance(last, ast.JoinedStr):  # f"fk_{referrer}_{field}"
                names.add("fk_*")
    return names


def test_every_check_in_the_fake_is_named_in_a_test():
    haystack = "\n".join(p.read_text(encoding="utf-8") for p in [
        *(ROOT / "fakes").glob("test_*.py"), *(ROOT / "services" / "os_ledger").rglob("*.py")])
    quoted = {m for m in re.findall(r"[\"']([a-z_*]+)[\"']", haystack)}
    quoted |= set(re.findall(r"fake_invariant:([a-z_]+)", haystack))
    missing = sorted(n for n in invariant_names() if n not in quoted and n != "fk_*")
    assert missing == []
    assert "fk_*" in invariant_names()


# --- workspace and admin invariants -----------------------------------------------------------


@pytest.mark.parametrize("owner,epoch,name", [("bad", 1, "workspace_owner"),
                                              ("123", 2, "workspace_initial_epoch")])
def test_workspace_invariants(owner, epoch, name):
    fake = FakeLedger(clock=lambda: NOW, settings_epoch=lambda: 1)
    with violation(name):
        fake.ensure_workspace("test", owner, epoch)


def test_pepper_format(fake):
    with violation("statement_pepper_fp"):
        fake.pin_statement_pepper(WORKSPACE, "invalid")


@pytest.mark.parametrize("kwargs,name", [
    ({"domain": "other", "to": "active"}, "authority_domain"),
    ({"domain": "core", "to": "bogus"}, "authority_state"),
])
def test_transition_arguments(fake, kwargs, name):
    with violation(name):
        fake.transition(WORKSPACE, kwargs["domain"], kwargs["to"], expected_epoch=1,
                        actor_owner_id=OWNER_ID)


@pytest.mark.parametrize("kwargs,name", [
    ({"domain": "other", "kind": "owner_direct"}, "transaction_domain"),
    ({"domain": "core", "kind": "bogus"}, "transaction_kind"),
    ({"domain": "core", "kind": "owner_direct", "purpose": "other"}, "transaction_purpose"),
])
def test_transaction_arguments(fake, kwargs, name):
    with violation(name):
        with fake.transaction(ctx(), **kwargs):
            pass


def test_the_domain_row_is_the_third_epoch_layer(fake):
    fake._workspaces[WORKSPACE]["authority_epochs"]["core"] = 2  # a desynced row, never reachable
    with pytest.raises(api.LedgerError, match="^authority_changed$"):
        with fake.transaction(ctx(), domain="core", kind="owner_direct"):
            pass
    with pytest.raises(api.LedgerError, match="^authority_changed$"):
        fake.head(ctx())


def test_no_implicit_workspace():
    fake = FakeLedger(clock=lambda: NOW, settings_epoch=lambda: 1)
    with pytest.raises(api.LedgerError, match="ledger_unavailable"):
        fake.head(ctx())
    assert fake.rows("os_workspaces") == []


def test_nested_transaction_fails_without_deadlocking(fake):
    with fake.transaction(ctx(), domain="core", kind="owner_direct"):
        with pytest.raises(AssertionError, match="^fake_invariant:nested_transaction$"):
            with fake.transaction(ctx(), domain="core", kind="owner_direct"):
                pytest.fail("nested transaction yielded")


def test_nested_transaction_is_detected_for_reads_and_admin_calls_too(fake):
    with fake.transaction(ctx(), domain="core", kind="owner_direct"):
        for call in (lambda: fake.head(ctx()), lambda: fake.state(WORKSPACE),
                     lambda: fake.rows("os_tasks"), lambda: fake.version(WORKSPACE)):
            with violation("nested_transaction"):
                call()


def test_a_transaction_on_another_workspace_may_nest_on_a_different_lock(fake):
    fresh_workspace(fake, "ws-b", core="active")
    with fake.transaction(ctx(), domain="core", kind="owner_direct"):
        with fake.transaction(ctx(workspace="ws-b"), domain="core", kind="owner_direct"):
            pass


# --- insert_proposal and finish invariants ----------------------------------------------------


def draft(tx, **changes):
    values = dict(kind="owner_direct", source="OS_DIRECT", request_id=rid(),
                  request_digest=api.digest({}), entity="tasks", record_id="TAS_one",
                  expected_revision=0, mutations=(task("TAS_one"),),
                  dependency_digest=api.digest([]), expires_at=tx.now + timedelta(seconds=30))
    return api.ProposalDraft(**(values | changes))


@pytest.mark.parametrize("changes,name", [
    ({"expected_revision": -1}, "proposal_revision"),
    ({"expires_at": NOW}, "proposal_expiry"),
    ({"request_id": "not-a-uuid"}, "proposal_request_id"),
    ({"source": "os_direct"}, "proposal_source"),
    ({"source": "OS"}, "proposal_direct_source"),
    ({"source": "ASSISTANT_GPT"}, "proposal_assistant_kind"),
    ({"kind": "proposal", "source": "OS", "expires_at": NOW + timedelta(minutes=11)},
     "proposal_ttl"),
    ({"kind": "statement_batch", "source": "ASSISTANT_PASTE",
      "expires_at": NOW + timedelta(days=8)}, "proposal_ttl"),
])
def test_proposal_checks(fake, changes, name):
    kind = changes.get("kind", "owner_direct")
    domain = "statements" if kind == "statement_batch" else "core"
    if kind == "statement_batch":
        fake.transition(WORKSPACE, "statements", "active", expected_epoch=1,
                        actor_owner_id=OWNER_ID)
    with violation(name):
        with fake.transaction(ctx(), domain=domain, kind=kind) as tx:
            tx.insert_proposal(draft(tx, **changes))
    assert fake.rows("os_proposals") == []


@pytest.mark.parametrize("tx_kind,draft_kind", [
    ("owner_direct", "proposal"), ("proposal", "owner_direct"), ("owner_undo", "owner_direct"),
    ("owner_direct", "owner_undo"), ("owner_direct", "statement_batch"),
    ("proposal", "import")])
def test_a_draft_must_have_the_kind_of_its_transaction(fake, tx_kind, draft_kind):
    source = {"proposal": "OS", "statement_batch": "ASSISTANT_PASTE",
              "import": "IMPORT"}.get(draft_kind, "OS_DIRECT")
    with violation("proposal_kind_mismatch"):
        with fake.transaction(ctx(), domain="core", kind=tx_kind) as tx:
            tx.insert_proposal(draft(tx, kind=draft_kind, source=source,
                                     expires_at=tx.now + timedelta(seconds=30)))
    assert fake.rows("os_proposals") == []


@pytest.mark.parametrize("field,name", [
    ("request_id", "proposal_request_unique"), ("approval_id", "proposal_approval_unique"),
    ("rejection_id", "proposal_rejection_unique"), ("proposal_id", "proposal_pk"),
    ("distinct", "proposal_decision_distinct")])
def test_proposal_unique_invariants(fake, field, name):
    with fake.transaction(ctx(), domain="core", kind="proposal") as tx:
        first = tx.insert_proposal(draft(tx, kind="proposal", source="OS"))
        new = draft(tx, kind="proposal", source="OS")
        ids = [rid(), rid(), rid()]
        if field == "request_id":
            new = replace(new, request_id=first.request_id)
        elif field == "distinct":
            ids[1] = ids[0]
        else:
            index = {"approval_id": 0, "rejection_id": 1, "proposal_id": 2}[field]
            ids[index] = getattr(first, field).removeprefix("PRO_")
        fake.uuid4 = iter(ids).__next__
        with violation(name):
            tx.insert_proposal(new)


@pytest.mark.parametrize("changes,name", [
    ({"status": "pending"}, "receipt_status"),
    ({"reason_codes": ["reason"] * 21}, "receipt_reason_codes"),
    ({"undo_of": rid()}, "receipt_undo_kind"),
    ({"status": "target_changed", "undo_until": NOW + timedelta(seconds=1)},
     "receipt_undo_until_kind"),
    ({"receipt_request_id": rid()}, "receipt_key"),
])
def test_receipt_invariants(fake, changes, name):
    with fake.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        proposal = tx.insert_proposal(draft(tx))
        args = {"status": "committed", "receipt_request_id": proposal.request_id} | changes
        with violation(name):
            tx.finish(proposal, **args)


def new_proposal(tx, kind):
    source = {"proposal": "OS", "import": "IMPORT"}.get(kind, "OS_DIRECT")
    return tx.insert_proposal(draft(tx, kind=kind, source=source,
                                    entity="import" if kind == "import" else "tasks"))


@pytest.mark.parametrize("status,key", [
    ("committed", "approval_id"), ("target_changed", "approval_id"),
    ("rejected", "rejection_id"), ("superseded", "rejection_id"),
    ("expired", "approval_id"), ("expired", "rejection_id")])
def test_the_receipt_key_table_for_a_decided_proposal(fake, status, key):
    other = {"approval_id": "rejection_id", "rejection_id": "approval_id"}[key]
    with fake.transaction(ctx(), domain="core", kind="proposal") as tx:
        proposal = new_proposal(tx, "proposal")
        if status != "expired":  # a late approve or reject expires under the id it supplied
            with violation("receipt_key"):
                tx.finish(proposal, status=status, receipt_request_id=getattr(proposal, other))
        tx.finish(proposal, status=status, receipt_request_id=getattr(proposal, key))


def test_an_epoch_changed_expiry_is_keyed_by_the_rejection_id(fake):
    with fake.transaction(ctx(), domain="core", kind="proposal") as tx:
        proposal = new_proposal(tx, "proposal")
        with violation("receipt_key"):
            tx.finish(proposal, status="expired", receipt_request_id=proposal.approval_id,
                      reason_codes=("epoch_changed",))
        tx.finish(proposal, status="expired", receipt_request_id=proposal.rejection_id,
                  reason_codes=("epoch_changed",))


def test_expiry_admission_is_keyed_by_the_rejection_id(env, fake):
    env.transition(WORKSPACE, "statements", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    with env.transaction(ctx(), domain="statements", kind="statement_batch") as tx:
        batch = tx.insert_proposal(draft(
            tx, kind="statement_batch", source="ASSISTANT_PASTE", entity="statements",
            expires_at=tx.now + timedelta(days=1)))
    env.advance(2 * 86400)
    with env.transaction(for_worker("retention", workspace=WORKSPACE), domain="statements",
                         kind="statement_batch", purpose="expire") as tx:
        stored = tx.proposal(batch.proposal_id)
        with violation("receipt_key"):
            tx.finish(stored, status="expired", receipt_request_id=stored.approval_id)
        tx.finish(stored, status="expired", receipt_request_id=stored.rejection_id)


@pytest.mark.parametrize("kind,status", [
    ("owner_direct", "committed"), ("owner_direct", "target_changed"),
    ("owner_direct", "abandoned"), ("owner_undo", "committed"), ("owner_undo", "target_changed")])
def test_a_fused_kind_is_finished_under_its_approval_id(fake, kind, status):
    with fake.transaction(ctx(), domain="core", kind="owner_direct") as setup:
        original = new_proposal(setup, "owner_direct")
        setup.finish(original, status="committed", receipt_request_id=original.approval_id)
    undo_of = original.approval_id if kind == "owner_undo" else None
    with fake.transaction(ctx(), domain="core", kind=kind) as tx:
        proposal = new_proposal(tx, kind)
        with violation("receipt_key"):
            tx.finish(proposal, status=status, receipt_request_id=proposal.rejection_id,
                      undo_of=undo_of)
        tx.finish(proposal, status=status, receipt_request_id=proposal.approval_id,
                  undo_of=undo_of)


@pytest.mark.parametrize("status", ("rejected", "expired", "superseded"))
@pytest.mark.parametrize("kind", ("owner_direct", "owner_undo", "import"))
def test_a_fused_kind_is_never_rejected_expired_or_superseded(fake, kind, status):
    if kind == "import":
        fresh_workspace(fake, "ws-i")
        context = ctx(workspace="ws-i")
    else:
        context = ctx()
    with fake.transaction(context, domain="core", kind=kind) as tx:
        proposal = new_proposal(tx, kind)
        for key in (proposal.approval_id, proposal.rejection_id):
            with violation("receipt_fused_status"):
                tx.finish(proposal, status=status, receipt_request_id=key)


def test_finish_pending(fake):
    with fake.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        proposal = tx.insert_proposal(draft(tx))
        tx.finish(proposal, status="abandoned", receipt_request_id=proposal.request_id)
        with violation("finish_pending"):
            tx.finish(proposal, status="abandoned", receipt_request_id=proposal.request_id)


def test_abandoned_kind(fake):
    with fake.transaction(ctx(), domain="core", kind="proposal") as tx:
        proposal = tx.insert_proposal(draft(tx, kind="proposal", source="OS"))
        with violation("abandoned_kind"):
            tx.finish(proposal, status="abandoned", receipt_request_id=proposal.approval_id)


def test_nonpositive_undo_window(fake):
    fake.undo_window_s = lambda: 0
    with fake.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        proposal = tx.insert_proposal(draft(tx))
        with violation("receipt_undo_window"):
            tx.finish(proposal, status="committed", receipt_request_id=proposal.request_id)


def test_receipt_primary_key_and_proposal_uniqueness(fake):
    # Both are redundant behind the unique decision ids and the pending precondition, so the
    # test tampers with the tables to reach them.
    with fake.transaction(ctx(), domain="core", kind="proposal") as tx:
        first = tx.insert_proposal(draft(tx, kind="proposal", source="OS"))
        second = tx.insert_proposal(draft(tx, kind="proposal", source="OS", record_id="TAS_two"))
        tx.finish(first, status="rejected", receipt_request_id=first.rejection_id)
        proposals = tx.data["tables"]["os_proposals"]
        proposals[second.proposal_id] = replace(second, rejection_id=first.rejection_id)
        with violation("receipt_pk"):
            tx.finish(second, status="rejected", receipt_request_id=first.rejection_id)
        proposals[first.proposal_id] = replace(proposals[first.proposal_id], status="pending")
        with violation("receipt_proposal_unique"):
            tx.finish(first, status="expired", receipt_request_id=first.approval_id)


def test_event_primary_key(fake):
    with fake.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        first = tx.insert_proposal(draft(tx))
        tx.finish(first, status="abandoned", receipt_request_id=first.request_id)
        event_id = next(iter(tx.data["tables"]["os_events"]))
        second = tx.insert_proposal(draft(tx, record_id="TAS_two"))
        fake.uuid4 = lambda: event_id
        with violation("event_pk"):
            tx.finish(second, status="abandoned", receipt_request_id=second.request_id)


def event_row(**over):
    return dict(workspace_id=WORKSPACE, event_id="tampered", decision_id=rid(), commit_version=99,
                created_at=NOW, body={}) | over


def test_events_and_outbox_rows_have_unique_decision_and_version_keys(fake):
    # decision_id is behind the receipt primary key and commit_version behind the monotonic
    # version, so the tables are tampered with to reach each key.
    with fake.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        proposal = tx.insert_proposal(draft(tx))
        tables = tx.data["tables"]
        next_version = tx.data["version"] + 1
        tables["os_events"]["tampered"] = event_row(decision_id=proposal.request_id)
        with violation("event_decision_unique"):
            tx.finish(proposal, status="abandoned", receipt_request_id=proposal.request_id)
        tables["os_events"]["tampered"] = event_row(commit_version=next_version)
        with violation("event_version_unique"):
            tx.finish(proposal, status="abandoned", receipt_request_id=proposal.request_id)
        tables["os_events"].clear()
        tables["os_outbox"]["tampered"] = dict(commit_version=next_version, body={})
        with violation("outbox_version_unique"):
            tx.finish(proposal, status="committed", receipt_request_id=proposal.request_id)
        # Nothing was written by the refused finishes.
        assert tx.data["version"] == next_version - 1 and tables["os_receipts"] == {}


def test_timestamps_cannot_run_backwards(env, fake):
    commit_direct(env, ctx(), [task()])
    fake.clock = lambda: NOW - timedelta(hours=1)  # the clock must never go backwards
    with violation("record_timestamps"):
        commit_direct(env, ctx(), [update(current(env, "tasks", "TAS_t1"), title="Later")])


def test_a_failed_check_leaves_the_workspace_untouched(env):
    commit_direct(env, ctx(), [task()])
    snapshot = {t: env.rows(t) for t in ("os_proposals", "os_receipts", "os_events", "os_tasks")}
    with violation("proposal_revision"):
        with env.transaction(ctx(), domain="core", kind="owner_direct") as tx:
            tx.insert_proposal(draft(tx, expected_revision=-1))
    assert snapshot == {t: env.rows(t) for t in snapshot}


# --- foreign keys (unreachable through the domain validator, so patch it out) -------------------


EDGES = [(referrer, field, target) for referrer, field, target in api.DEPENDENTS]


def reference_mutation(referrer, field):
    """A row of ``referrer`` valid in every reference but ``field``, which dangles."""
    dangling = "MISSING_ID"
    inputs = {
        "works": dict(person_id="PER_c1", title="W2", path=work().after["path"],
                      status=work().after["status"], currency="ILS"),
        "payments": dict(person_id="PER_c1", work_id="WOR_w1", path=work().after["path"],
                         date="2026-10-01", actual_amount_ils=100, currency="ILS",
                         kind=payment().after["kind"]),
        "time": dict(person_id="PER_c1", work_id="WOR_w1", path=work().after["path"],
                     date="2026-10-01", minutes=5, status=time_entry().after["status"]),
        "tasks": dict(title="T2", status=task().after["status"], person_id="PER_c1",
                      work_id="WOR_w1"),
    }[referrer]
    if field == "original_payment_id":
        inputs["kind"] = PAYMENT_REFUND
    inputs[field] = dangling
    return api.Mutation(referrer, "REC_new1", "upsert", None,
                        {api.ID_KEYS[referrer]: "REC_new1", "revision": 1, **inputs})


@pytest.mark.parametrize("referrer,field,target", EDGES)
def test_foreign_keys_are_mirrored(env, monkeypatch, referrer, field, target):
    commit_direct(env, ctx(), [client(), work(), payment()])
    monkeypatch.setattr(fake_module, "validate_transition", lambda *a, **k: None)
    monkeypatch.setattr(fake_module, "validate_state", lambda *a, **k: None)
    with violation(f"fk_{referrer}_{field}"):
        commit_direct(env, ctx(), [reference_mutation(referrer, field)])
    assert "REC_new1" not in {r["record_id"] for r in env.rows(f"os_{referrer}")}


def test_a_tombstoned_referenced_row_still_satisfies_the_foreign_key(env):
    commit_direct(env, ctx(), [task("TAS_a")])
    commit_direct(env, ctx(), [tombstone("tasks", current(env, "tasks", "TAS_a"))])
    assert env.rows("os_tasks")[0]["deleted_at"] is not None


# --- the E1-rev import tables (fake-only invariants; conformance covers the rest) --------------


@pytest.fixture
def shadow(env):
    fresh_workspace(env, "ws-i")
    return env


def batch_args(**over):
    return dict(batch_id=rid(), authority_epoch=1, source_digest="a" * 64,
                source_binding="synthetic:test", provenance={}) | over


def row(number=1, **over):
    values = dict(row_number=number, entity="tasks", record_id=f"TAS_{number}",
                  source_digest=api.digest({"n": number}), status="staged", raw_record={},
                  canonical_record=None, provenance={}, quarantine=None)
    return api.ImportRow(**(values | over))


@pytest.mark.parametrize("over,name", [
    ({"batch_id": "not-a-uuid"}, "import_batch_id"),
    ({"source_digest": ""}, "import_batch_source_digest"),
    ({"source_digest": "x" * 65}, "import_batch_source_digest"),
    ({"source_binding": ""}, "import_batch_source_binding"),
    ({"provenance": None}, "import_batch_provenance"),
])
def test_import_batch_column_checks(shadow, over, name):
    with violation(name):
        with import_tx(shadow) as tx:
            tx.insert_import_batch(**batch_args(**over))


@pytest.mark.parametrize("over,name", [
    ({"status": "validated"}, "import_row_status"),
    ({"record_id": ""}, "import_row_record"),
    ({"record_id": "x" * 65}, "import_row_record"),
    ({"source_digest": ""}, "import_row_source_digest"),
    ({"raw_record": []}, "import_row_raw"),
    ({"canonical_record": []}, "import_row_canonical"),
    ({"provenance": []}, "import_row_provenance"),
    ({"quarantine": []}, "import_row_quarantine_type"),
    ({"record_id": None}, "import_row_idless"),
])
def test_import_row_column_checks(shadow, over, name):
    args = batch_args()
    with violation(name):
        with import_tx(shadow) as tx:
            tx.insert_import_batch(**args)
            tx.insert_import_rows(args["batch_id"], [row(**over)])


def test_import_rows_must_be_import_row_values(shadow):
    args = batch_args()
    with violation("import_row_type"):
        with import_tx(shadow) as tx:
            tx.insert_import_batch(**args)
            tx.insert_import_rows(args["batch_id"], [{"row_number": 1}])


def test_import_status_arguments(shadow):
    args = batch_args()
    with import_tx(shadow) as tx:
        tx.insert_import_batch(**args)
    with violation("import_status_batch_fk"):
        with import_tx(shadow) as tx:
            tx.set_import_status(rid(), "validated")
    with violation("import_status_value"):
        with import_tx(shadow) as tx:
            tx.set_import_status(args["batch_id"], "manifest_signed")
    with violation("import_batch_quarantine"):
        with import_tx(shadow) as tx:
            tx.set_import_status(args["batch_id"], "validated", quarantine={"reason": "x"})


def test_import_completion_cannot_precede_creation(shadow, fake):
    args = batch_args()
    with import_tx(shadow) as tx:
        tx.insert_import_batch(**args)
    fake.clock = lambda: NOW - timedelta(hours=1)
    with violation("import_batch_completed"):
        with import_tx(shadow) as tx:
            tx.set_import_status(args["batch_id"], "failed")


def test_live_ids_also_runs_in_a_read_transaction(env, fake):
    commit_direct(env, ctx(), [task()])
    with fake._read(ctx()) as tx:
        assert tx.live_ids("tasks") == frozenset({"TAS_t1"})
        with pytest.raises(api.LedgerError, match="^invalid_transaction_kind$"):
            tx.insert_import_batch(**batch_args())


# --- copy-on-write, atomicity, scratch, hooks --------------------------------------------------


def test_copy_on_write_rollback_includes_scratch(env):
    with pytest.raises(api.LedgerError, match="target_changed"):
        with env.transaction(ctx(), domain="core", kind="owner_direct") as tx:
            tx.scratch["chunk"] = {"items": [1]}
            proposal = tx.insert_proposal(draft(tx))
            tx.apply(proposal.mutations, source=proposal.source, request_id=proposal.request_id)
            tx.finish(proposal, status="committed", receipt_request_id=proposal.request_id,
                      applied=proposal.mutations)
            raise api.LedgerError("target_changed")
    for table in ("os_tasks", "os_proposals", "os_receipts", "os_events", "os_outbox"):
        assert env.rows(table) == []
    assert env.version() == 0
    with env.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        assert tx.scratch == {}


def test_scratch_is_per_workspace_and_survives_a_commit(env):
    fresh_workspace(env, "ws-b", core="active")
    with env.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        tx.scratch["rows"] = [1, 2]
    with env.transaction(ctx(workspace="ws-b"), domain="core", kind="owner_direct") as tx:
        assert tx.scratch == {}
    with env.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        assert tx.scratch == {"rows": [1, 2]}
        assert tx.connection is None


def test_apply_atomicity_transaction_remains_usable(env):
    with env.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        proposal = tx.insert_proposal(draft(tx))
        with pytest.raises(api.LedgerError, match="target_changed"):
            skipped = task("TAS_bad")
            skipped = api.Mutation("tasks", "TAS_bad", "upsert", None,
                                   {**skipped.after, "revision": 2})
            tx.apply((task("TAS_one"), skipped), source="OS_DIRECT",
                     request_id=proposal.request_id)
        assert tx.get("tasks", "TAS_one") is None
        tx.finish(proposal, status="target_changed", receipt_request_id=proposal.request_id)
    assert env.rows("os_tasks") == []
    assert len(env.rows("os_events")) == 1 and env.rows("os_outbox") == []
    assert env.version() == 1


def test_injected_validator_rolls_back_apply_and_scratch():
    class Validator:
        def validate(self, tx, applied):
            tx.scratch["dirty"] = True
            raise api.LedgerError("target_changed")

    fake = FakeLedger(clock=lambda: NOW, settings_epoch=lambda: 1, validator=Validator())
    fake.ensure_workspace(WORKSPACE, OWNER_ID, 1)
    fake.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    with fake.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        with pytest.raises(api.LedgerError, match="target_changed"):
            tx.apply((task(),), source="OS_DIRECT", request_id=rid())
        assert tx.scratch == {}
        assert tx.get("tasks", "TAS_t1") is None


def test_injected_summaries_replace_the_full_recompute():
    seen = []

    class Summaries:
        def on_apply(self, tx, applied):
            seen.append([m.record_id for m in applied])
            tx.scratch["summarised"] = True

    fake = FakeLedger(clock=lambda: NOW, settings_epoch=lambda: 1, summaries=Summaries())
    fake.ensure_workspace(WORKSPACE, OWNER_ID, 1)
    fake.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    with fake.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        tx.apply((task(),), source="OS_DIRECT", request_id=rid())
        assert tx.scratch["summarised"]
    assert seen == [["TAS_t1"]]


def test_rows_and_returned_records_are_copies(env):
    with env.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        result = tx.apply((task(),), source="OS_DIRECT", request_id=rid())
        result[0]["title"] = "changed"
        assert tx.get("tasks", "TAS_t1")["title"] == "Task"
    rows = env.rows("os_tasks")
    rows.clear()
    assert len(env.rows("os_tasks")) == 1


def test_rows_are_plain_dicts_with_the_sql_column_names(env):
    proposal = propose_task(env)
    [row_] = env.rows("os_proposals")
    assert isinstance(row_, dict) and row_["proposal_id"] == proposal.proposal_id
    assert {"request_id", "approval_id", "rejection_id", "kind", "status", "epoch",
            "dependency_digest", "expires_at", "created_at"} <= set(row_)
    [ws] = env.rows("os_workspaces")
    assert set(ws) >= {"workspace_id", "owner_id", "epoch", "version", "pending_seq",
                       "statement_pepper_fp", "summaries"}
    assert {r["domain"] for r in env.rows("os_authority")} == {"core", "statements"}


def test_cursor_tokens_never_consume_the_uuid_source(env, fake):
    commit_direct(env, ctx(), [task("TAS_a"), task("TAS_b"), task("TAS_c")])
    fake.uuid4 = lambda: pytest.fail("a cursor must not draw from the uuid source")
    page = env.page(ctx(), "tasks", after=None, limit=1)
    assert page.next_cursor.startswith("fake:")
    assert re.fullmatch(r"fake:[A-Za-z0-9_-]+", page.next_cursor)


def test_abandon_drafts_are_pending_owner_direct_rows_until_finished(env):
    request_id = rid()
    with env.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        proposal = tx.insert_proposal(abandon_draft(tx, request_id, 30))
        assert tx.proposal_by_request(request_id) == proposal and proposal.status == "pending"
        assert proposal.kind == "owner_direct" and proposal.mutations == ()
        assert direct_draft(tx, [task()], rid(), 30).kind == proposal.kind


def test_fake_expiry_without_the_restored_code_stays_out_of_scope(env):
    """§22 E7 caveat. The fake cannot see V2-26's batch lines, so it trusts the reason code
    ``restored_without_detail`` for "no batch lines". PG checks zero ``os_statement_batch_lines``
    rows as well. Without that code, a not-overdue batch of the current epoch is refused."""
    from tests.services.os_ledger.conformance.test_admission import expire_batch, pending_batch

    batch = pending_batch(env)
    for codes in ((), ("credential_revoked",)):
        with pytest.raises(AssertionError, match="ledger_invariant:expire_scope"):
            expire_batch(env, batch, reason_codes=codes)
    assert expire_batch(env, batch, reason_codes=("restored_without_detail",)).status == "expired"
