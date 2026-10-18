"""Import staging on the ledger (erratum E1-rev, §22): batches, rows, status machine, live_ids.

Rows are insert-only. A batch moves only along ``api.IMPORT_STATUS_EDGES``. The writers run only in
a ``kind="import"`` transaction (core=shadow); anything else is ``invalid_transaction_kind``.
``live_ids`` reads ``api.LIVE_IDS_LIMIT`` and ``api.FORK_DELETE_END_STATE`` at call time, so a
test patches the limit; an implementation must not cache either.
"""

from itertools import product

import pytest
from app.services.os_ledger import api

from tests.fakes.contexts import for_worker
from tests.fakes.os_ledger_builders import (
    CLIENT_INACTIVE,
    EXPENSE_CANCELLED,
    LEAD_CONVERTED,
    LEAD_IRRELEVANT,
    PAYMENT_CANCELLED,
    TASK_CANCELLED,
    TASK_DONE,
    TIME_CANCELLED,
    WORK_CANCELLED,
    client,
    commit_import,
    counts,
    ctx,
    expense,
    fresh_workspace,
    invariant_violation,
    lead,
    payment,
    rid,
    task,
    time_entry,
    tombstone,
    work,
)
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE

IMPORT_WS = "ws-i"
STATUSES = ("staged", "validated", "imported", "quarantined", "failed")
ILLEGAL = [pair for pair in product(STATUSES, STATUSES) if pair not in api.IMPORT_STATUS_EDGES]
QUARANTINE = {"reason": "unresolved_reference"}


def worker(epoch=1):
    return for_worker("import", workspace=IMPORT_WS, epoch=epoch)


def import_tx(env, epoch=1):
    return env.transaction(worker(epoch), domain="core", kind="import")


def to_epoch_two(env):
    """fenced -> shadow bumps the epoch (no non-import receipt exists); the knob follows."""
    for state in ("active", "fenced", "shadow"):
        env.transition(IMPORT_WS, "core", state, expected_epoch=1, actor_owner_id=OWNER_ID)
    env.knobs.settings_epoch = 2


@pytest.fixture
def shadow(ledger):
    fresh_workspace(ledger, IMPORT_WS)
    return ledger


def batch_args(number=1, epoch=1, batch_id=None):
    return dict(batch_id=batch_id or rid(), authority_epoch=epoch,
                source_digest=api.digest({"source": number}), source_binding="synthetic:test",
                provenance={"workbook": "synthetic"})


def row(number, *, entity="tasks", status="staged", record_id="auto", quarantine=None, **over):
    values = dict(
        row_number=number, entity=entity,
        record_id=f"TAS_i{number}" if record_id == "auto" else record_id,
        source_digest=api.digest({"row": number}), status=status,
        raw_record={"title": f"row {number}"}, canonical_record={"title": f"row {number}"},
        provenance={"sheet": "Tasks"}, quarantine=quarantine)
    return api.ImportRow(**(values | over))


def bad_row(number):
    return row(number, status="quarantined", record_id=None, canonical_record=None,
               quarantine=QUARANTINE)


def batch(env, batch_id):
    return next(r for r in env.rows("os_import_batches") if r["batch_id"] == batch_id)


def stage(env, rows=(), *, ctx_epoch=1, **kw):
    args = batch_args(**kw)
    with import_tx(env, ctx_epoch) as tx:
        tx.insert_import_batch(**args)
        if rows:
            tx.insert_import_rows(args["batch_id"], list(rows))
    return args["batch_id"]


def set_status(env, batch_id, status, **kw):
    with import_tx(env) as tx:
        tx.set_import_status(batch_id, status, **kw)


def reach(env, wanted):
    """A fresh batch in ``wanted``, reached only along legal edges."""
    bad = [bad_row(1)] if wanted == "quarantined" else [row(1)]
    batch_id = stage(env, bad, number=len(env.rows("os_import_batches")) + 1)
    path = {"staged": (), "validated": ("validated",), "imported": ("validated", "imported"),
            "quarantined": ("quarantined",), "failed": ("failed",)}[wanted]
    for step in path:
        set_status(env, batch_id, step, **({"quarantine": QUARANTINE} if step == "quarantined"
                                            else {}))
    return batch_id


def test_a_batch_and_its_rows_are_stored_as_given(shadow):
    args = batch_args(7, epoch=1)
    with import_tx(shadow) as tx:
        assert tx.insert_import_batch(**args) is None
        assert tx.insert_import_rows(args["batch_id"], [row(1), row(2), bad_row(3)]) == 3
    stored = batch(shadow, args["batch_id"])
    assert (stored["status"], stored["authority_epoch"], stored["source_digest"]) == (
        "staged", 1, args["source_digest"])
    assert stored["source_binding"] == "synthetic:test"
    assert stored["provenance"] == {"workbook": "synthetic"}
    assert stored["completed_at"] is None and stored["quarantine"] is None
    rows = {r["row_number"]: r for r in shadow.rows("os_import_rows")}
    assert sorted(rows) == [1, 2, 3]
    assert rows[1]["entity"] == "tasks" and rows[1]["record_id"] == "TAS_i1"
    assert rows[1]["status"] == "staged" and rows[1]["raw_record"] == {"title": "row 1"}
    assert rows[3]["status"] == "quarantined" and rows[3]["record_id"] is None
    assert rows[3]["quarantine"] == QUARANTINE and rows[3]["canonical_record"] is None


def test_the_happy_status_path_sets_completed_at_only_on_the_terminal_states(shadow):
    batch_id = stage(shadow, [row(1)])
    set_status(shadow, batch_id, "validated")
    assert batch(shadow, batch_id)["status"] == "validated"
    assert batch(shadow, batch_id)["completed_at"] is None
    set_status(shadow, batch_id, "imported")
    done = batch(shadow, batch_id)
    assert done["status"] == "imported" and done["completed_at"] is not None
    assert done["completed_at"] >= done["created_at"]


def test_quarantine_is_terminal_and_stores_the_quarantine_object(shadow):
    batch_id = stage(shadow, [row(1), bad_row(2)])
    set_status(shadow, batch_id, "quarantined", quarantine=QUARANTINE)
    stored = batch(shadow, batch_id)
    assert stored["status"] == "quarantined" and stored["quarantine"] == QUARANTINE
    assert stored["completed_at"] is not None


@pytest.mark.parametrize("source", ("staged", "validated"))
def test_failed_is_the_abandon_path_from_staged_and_validated(shadow, source):
    batch_id = reach(shadow, source)
    set_status(shadow, batch_id, "failed")
    stored = batch(shadow, batch_id)
    assert stored["status"] == "failed" and stored["completed_at"] is not None


def test_validated_needs_no_quarantined_row(shadow):
    batch_id = stage(shadow, [row(1), bad_row(2)])
    with pytest.raises(api.LedgerError, match="^import_status_invalid$"):
        set_status(shadow, batch_id, "validated")
    assert batch(shadow, batch_id)["status"] == "staged"


def test_quarantined_needs_a_quarantined_row_and_a_quarantine_object(shadow):
    clean = stage(shadow, [row(1)], number=1)
    with pytest.raises(api.LedgerError, match="^import_status_invalid$"):
        set_status(shadow, clean, "quarantined", quarantine=QUARANTINE)
    dirty = stage(shadow, [bad_row(1)], number=2)
    with pytest.raises(api.LedgerError, match="^import_status_invalid$"):
        set_status(shadow, dirty, "quarantined")
    assert {batch(shadow, b)["status"] for b in (clean, dirty)} == {"staged"}


@pytest.mark.parametrize("source,target", ILLEGAL)
def test_every_status_edge_outside_the_machine_is_import_status_invalid(shadow, source, target):
    batch_id = reach(shadow, source)
    kw = {"quarantine": QUARANTINE} if target == "quarantined" else {}
    with pytest.raises(api.LedgerError, match="^import_status_invalid$"):
        set_status(shadow, batch_id, target, **kw)
    assert batch(shadow, batch_id)["status"] == source


def test_an_imported_batch_is_immutable(shadow):
    batch_id = reach(shadow, "imported")
    with pytest.raises(api.LedgerError, match="^import_status_invalid$"):
        with import_tx(shadow) as tx:
            tx.insert_import_rows(batch_id, [row(9)])
    assert len(shadow.rows("os_import_rows")) == 1


@pytest.mark.parametrize("status", ("validated", "quarantined", "failed"))
def test_rows_can_only_be_added_while_the_batch_is_staged(shadow, status):
    batch_id = reach(shadow, status)
    with pytest.raises(api.LedgerError, match="^import_status_invalid$"):
        with import_tx(shadow) as tx:
            tx.insert_import_rows(batch_id, [row(9)])


WRITERS = {
    "insert_import_batch": lambda tx: tx.insert_import_batch(**batch_args()),
    "insert_import_rows": lambda tx: tx.insert_import_rows(rid(), [row(1)]),
    "set_import_status": lambda tx: tx.set_import_status(rid(), "validated"),
    "live_ids": lambda tx: tx.live_ids("tasks"),
}


@pytest.mark.parametrize("kind", ("proposal", "owner_direct", "owner_undo"))
@pytest.mark.parametrize("method", WRITERS)
def test_the_import_methods_refuse_every_other_transaction_kind(ledger, method, kind):
    before = counts(ledger)
    with pytest.raises(api.LedgerError, match="^invalid_transaction_kind$"):
        with ledger.transaction(ctx(), domain="core", kind=kind) as tx:
            WRITERS[method](tx)
    assert counts(ledger) == before
    assert ledger.rows("os_import_batches") == [] and ledger.rows("os_import_rows") == []


@pytest.mark.parametrize("method", WRITERS)
def test_the_import_methods_refuse_a_statement_batch_transaction(ledger, method):
    ledger.transition(WORKSPACE, "statements", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    before = counts(ledger)
    with pytest.raises(api.LedgerError, match="^invalid_transaction_kind$"):
        with ledger.transaction(ctx(), domain="statements", kind="statement_batch") as tx:
            WRITERS[method](tx)
    assert counts(ledger) == before
    assert ledger.rows("os_import_batches") == [] and ledger.rows("os_import_rows") == []


@pytest.mark.parametrize("method", WRITERS)
def test_the_import_methods_are_outside_the_expiry_scope(ledger, method):
    with pytest.raises(AssertionError, match="ledger_invariant:expire_scope"):
        with ledger.transaction(for_worker("retention", workspace=WORKSPACE), domain="statements",
                                kind="statement_batch", purpose="expire") as tx:
            WRITERS[method](tx)
    assert ledger.rows("os_import_batches") == [] and ledger.rows("os_import_rows") == []


def test_the_import_methods_run_in_an_import_transaction(shadow):
    with import_tx(shadow) as tx:
        args = batch_args()
        tx.insert_import_batch(**args)
        assert tx.insert_import_rows(args["batch_id"], [row(1)]) == 1
        tx.set_import_status(args["batch_id"], "validated")
        assert tx.live_ids("tasks") == frozenset()


def test_rows_are_insert_only_a_duplicate_row_number_is_an_integrity_failure(shadow):
    batch_id = stage(shadow, [row(1)])
    with invariant_violation(shadow, "import_row_pk"):
        with import_tx(shadow) as tx:
            tx.insert_import_rows(batch_id, [row(1, source_digest=api.digest({"other": 1}))])
    assert [r["source_digest"] for r in shadow.rows("os_import_rows")] == [
        api.digest({"row": 1})]


def test_rows_are_insert_only_a_duplicate_source_digest_is_an_integrity_failure(shadow):
    batch_id = stage(shadow, [row(1)])
    with invariant_violation(shadow, "import_row_source_unique"):
        with import_tx(shadow) as tx:
            tx.insert_import_rows(batch_id, [row(2, source_digest=api.digest({"row": 1}))])
    assert len(shadow.rows("os_import_rows")) == 1


@pytest.mark.parametrize("name,rows", [
    ("import_row_pk", lambda: [row(1), row(1, source_digest=api.digest({"x": 1}))]),
    ("import_row_source_unique", lambda: [row(1), row(2, source_digest=api.digest({"row": 1}))]),
])
def test_duplicates_inside_one_call_are_integrity_failures_too(shadow, name, rows):
    batch_id = stage(shadow)
    with invariant_violation(shadow, name):
        with import_tx(shadow) as tx:
            tx.insert_import_rows(batch_id, rows())
    assert shadow.rows("os_import_rows") == []


@pytest.mark.parametrize("name,bad", [
    ("import_row_number", lambda: row(0)),
    ("import_row_entity", lambda: row(1, entity="statements")),
    ("import_row_quarantine", lambda: row(1, status="quarantined", quarantine=None)),
])
def test_row_checks_are_integrity_failures(shadow, name, bad):
    batch_id = stage(shadow)
    with invariant_violation(shadow, name):
        with import_tx(shadow) as tx:
            tx.insert_import_rows(batch_id, [bad()])
    assert shadow.rows("os_import_rows") == []


def test_rows_need_an_existing_batch(shadow):
    with invariant_violation(shadow, "import_row_batch_fk"):
        with import_tx(shadow) as tx:
            tx.insert_import_rows(rid(), [row(1)])


def test_the_batch_key_is_unique_per_epoch(shadow):
    stage(shadow, number=1, epoch=1)
    with invariant_violation(shadow, "import_batch_source_unique"):
        stage(shadow, number=1, epoch=1)
    to_epoch_two(shadow)
    # The same source digest at the new epoch is a new batch.
    stage(shadow, number=1, epoch=2, ctx_epoch=2)
    assert sorted(r["authority_epoch"] for r in shadow.rows("os_import_batches")) == [1, 2]


@pytest.mark.parametrize("batch_epoch,ctx_epoch", [(2, 1), (1, 2), (3, 2)])
def test_a_batch_must_name_the_epoch_of_the_context(shadow, batch_epoch, ctx_epoch):
    if ctx_epoch == 2:
        to_epoch_two(shadow)
    before = len(shadow.rows("os_import_batches"))
    with pytest.raises(api.LedgerError, match="^authority_changed$"):
        stage(shadow, number=1, epoch=batch_epoch, ctx_epoch=ctx_epoch)
    assert len(shadow.rows("os_import_batches")) == before


def test_a_batch_id_is_unique(shadow):
    stage(shadow, number=1, batch_id=rid())
    first = shadow.rows("os_import_batches")[0]["batch_id"]
    with invariant_violation(shadow, "import_batch_pk"):
        stage(shadow, number=2, batch_id=first)


def test_a_batch_epoch_must_be_positive(shadow):
    with invariant_violation(shadow, "import_batch_epoch"):
        stage(shadow, number=1, epoch=0)


# --- live_ids ----------------------------------------------------------------------------------


@pytest.fixture
def populated(shadow):
    first = [
        client("PER_c1"), client("PER_c2", status=CLIENT_INACTIVE),
        work("WOR_w1"), work("WOR_w2", status=WORK_CANCELLED),
        payment("PAY_p1"), payment("PAY_p2", kind=PAYMENT_CANCELLED),
        time_entry("TIM_t1"), time_entry("TIM_t2", status=TIME_CANCELLED),
        task("TAS_a"), task("TAS_b", status=TASK_DONE), task("TAS_c"),
        task("TAS_d", status=TASK_CANCELLED),
        expense("EXP_e1"), expense("EXP_e2", source_key="key-2", payment_status=EXPENSE_CANCELLED),
        lead("LEA_l1"), lead("LEA_l2", person_id="PER_l2", phone="0521111111",
                             status=LEAD_IRRELEVANT),
        lead("LEA_l3", person_id="PER_l3", phone="0532222222", status=LEAD_CONVERTED)]
    commit_import(shadow, ctx(workspace=IMPORT_WS), first, api.digest({"m": 1}))
    tomb = tombstone("tasks", task("TAS_c").after)
    commit_import(shadow, ctx(workspace=IMPORT_WS), [tomb], api.digest({"m": 2}))
    return shadow


def live(env, entity):
    with import_tx(env) as tx:
        return tx.live_ids(entity)


def test_live_ids_excludes_tombstones_and_rows_in_their_fork_delete_end_state(populated):
    """A row is excluded only when tombstoned or already in ``api.FORK_DELETE_END_STATE``.

    Tasks have no end state (their delete is a tombstone), so a done and a status-cancelled task
    are included; a cancelled expense is excluded; a converted lead is included.
    """
    assert live(populated, "tasks") == frozenset({"TAS_a", "TAS_b", "TAS_d"})
    assert live(populated, "payments") == frozenset({"PAY_p1"})
    assert live(populated, "works") == frozenset({"WOR_w1"})
    assert live(populated, "time") == frozenset({"TIM_t1"})
    assert live(populated, "clients") == frozenset({"PER_c1"})
    assert live(populated, "expenses") == frozenset({"EXP_e1"})
    assert live(populated, "leads") == frozenset({"LEA_l1", "LEA_l3"})
    assert isinstance(live(populated, "tasks"), frozenset)


def test_live_ids_reads_the_fork_delete_end_states_at_call_time(populated, monkeypatch):
    """The one map is ``api.FORK_DELETE_END_STATE``; neither implementation may copy it."""
    assert live(populated, "tasks") == frozenset({"TAS_a", "TAS_b", "TAS_d"})
    changed = {**api.FORK_DELETE_END_STATE, "tasks": ("status", TASK_CANCELLED)}
    monkeypatch.setattr(api, "FORK_DELETE_END_STATE", changed)
    assert live(populated, "tasks") == frozenset({"TAS_a", "TAS_b"})
    monkeypatch.setattr(api, "FORK_DELETE_END_STATE", {**changed, "tasks": None})
    assert live(populated, "tasks") == frozenset({"TAS_a", "TAS_b", "TAS_d"})


@pytest.mark.parametrize("entity", ("statements", "promote_lead", "", "Tasks", "os_tasks"))
def test_live_ids_refuses_an_entity_that_is_not_a_record_entity(populated, entity):
    with pytest.raises(api.LedgerError) as caught:
        live(populated, entity)
    assert caught.value.code == "invalid_entity" and caught.value.status == 422


def test_live_ids_is_capped_per_entity(populated, monkeypatch):
    monkeypatch.setattr(api, "LIVE_IDS_LIMIT", 3)
    assert len(live(populated, "tasks")) == 3  # exactly at the cap
    monkeypatch.setattr(api, "LIVE_IDS_LIMIT", 2)
    with pytest.raises(api.LedgerError, match="^local_record_limit$"):
        live(populated, "tasks")
    assert live(populated, "works") == frozenset({"WOR_w1"})  # other entities have their own cap


def test_live_ids_is_scoped_to_the_workspace(populated):
    fresh_workspace(populated, "ws-other")
    with populated.transaction(ctx(workspace="ws-other"), domain="core", kind="import") as tx:
        assert tx.live_ids("tasks") == frozenset()
