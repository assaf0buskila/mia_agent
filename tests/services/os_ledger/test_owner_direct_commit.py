"""V2-24 POST /commits at the service level (§11.1, §5.3), on the fake and (once V2-16 lands) PG.

Every case drives ``owner_direct.commit`` through the ``LedgerTx`` protocol only. Covers: the
replay/idempotency table and the keyspace rule, revision and validate failures as 200
``target_changed`` receipts, every single op on every entity per FORK-DELETE, ``has_dependents``
over ``api.DEPENDENTS``, and bulk (1..100 unique pairs, all-or-nothing).
"""

from __future__ import annotations

import pytest
from app.services.os_ledger import api, owner_direct

from tests.fakes.os_ledger_builders import (
    CLIENT_INACTIVE,
    EXPENSE_CANCELLED,
    LEAD_IRRELEVANT,
    PAYMENT_CANCELLED,
    PAYMENT_REFUND,
    TIME_CANCELLED,
    WORK_CANCELLED,
    client,
    commit_direct,
    current,
    expense,
    lead,
    payment,
    propose_task,
    rejection_of,
    rid,
    task,
    time_entry,
    tombstone,
    update,
    work,
)
from tests.services.os_ledger.owner_direct_support import (
    BACKENDS,
    TASK_DONE,
    TASK_OPEN,
    KnobSettings,
    bulk,
    ledger_case,
    owner_ctx,
    patch_of,
    single,
    state,
)


@pytest.fixture(params=BACKENDS)
def env(request):
    return ledger_case(request.param)


def commit(env, body, *, context=None, settings=None, **kwargs):
    return owner_direct.commit(env, context or owner_ctx(), body,
                               settings=settings or KnobSettings(env), **kwargs)


def refused(code, call, *args, **kwargs):
    with pytest.raises(api.LedgerError) as caught:
        call(*args, **kwargs)
    assert caught.value.code == code, caught.value.code
    assert caught.value.write_outcome == "not_started"
    return caught.value


def record_of(body, record_id):
    return next(item["record"] for item in body["records"]
                if item["record"][api.ID_KEYS[item["entity"]]] == record_id)


def seed_tasks(env, *ids, **inputs):
    commit_direct(env, owner_ctx(), [task(i, **inputs) for i in ids])


# --- the receipt and the binding ---------------------------------------------------------------


def test_a_create_commits_with_revision_one_and_a_server_minted_id(env):
    before = env.version()
    body = commit(env, single("create", patch={"title": "  Draft  ", "status": TASK_OPEN}))
    assert body["status"] == "committed" and body["kind"] == "owner_direct"
    assert body["actor_source"] == "OS_DIRECT" and body["undo_of"] is None
    assert (body["api_version"], body["source"]) == ("assaf-os.v2", "postgresql")
    assert (body["workspace_id"], body["authority_epoch"]) == ("test", 1)
    assert body["commit_version"] == before + 1 == body["observed_version"] == env.version()
    [item] = body["records"]
    record = item["record"]
    assert item["entity"] == "tasks" and record["revision"] == 1
    assert record["task_id"].startswith("TAS_") and len(record["task_id"]) == 36
    assert record == {"task_id": record["task_id"], "revision": 1, "title": "Draft",
                      "status": TASK_OPEN}
    assert body["committed_at"] == body["server_now"]
    assert body["undo"]["state"] == "available" and body["undo"]["undo_request_id"] is None
    assert body["reason_codes"] == []
    stored = current(env, "tasks", record["task_id"])
    assert stored == record


def test_the_fused_columns_follow_the_contract(env):
    seed_tasks(env, "TAS_t1")
    request_id = rid()
    commit(env, single("update", record_id="TAS_t1", revision=1, patch={"title": "New"},
                       request_id=request_id))
    proposal = next(p for p in env.rows("os_proposals") if p["request_id"] == request_id)
    assert (proposal["kind"], proposal["source"], proposal["status"]) == (
        "owner_direct", "OS_DIRECT", "committed")
    assert (proposal["entity"], proposal["record_id"], proposal["expected_revision"]) == (
        "tasks", "TAS_t1", 1)
    assert proposal["approval_id"] == request_id
    assert proposal["rejection_id"] == rejection_of(request_id)
    assert proposal["dependency_digest"] == api.digest(
        [{"entity": "tasks", "record_id": "TAS_t1", "revision": 1}])
    window = env.knobs.undo_window_s
    assert (proposal["expires_at"] - proposal["created_at"]).total_seconds() == window


# --- idempotency and the keyspace rule ------------------------------------------------------------


def test_the_same_request_id_and_digest_replays_the_same_receipt(env):
    body = single("create", patch={"title": "Once", "status": TASK_OPEN})
    first = commit(env, body)
    snapshot = state(env)
    assert commit(env, dict(body)) == first
    assert state(env) == snapshot


def test_a_replay_after_other_writes_returns_the_stored_receipt_with_a_fresh_view(env):
    body = single("create", patch={"title": "Once", "status": TASK_OPEN})
    first = commit(env, body)
    commit(env, single("create", patch={"title": "Other", "status": TASK_OPEN}))
    again = commit(env, body)
    assert again["commit_version"] == first["commit_version"]
    assert again["observed_version"] == env.version() == first["observed_version"] + 1
    assert again["records"] == first["records"]


@pytest.mark.parametrize("change", ["patch", "op", "entity", "revision"])
def test_a_different_digest_is_idempotency_mismatch_and_writes_nothing(env, change):
    seed_tasks(env, "TAS_t1")
    body = single("update", record_id="TAS_t1", revision=1, patch={"title": "A"})
    commit(env, body)
    other = dict(body)
    other.update({"patch": {"patch": {"title": "B"}}, "op": {"op": "complete", "patch": {}},
                  "entity": {"entity": "clients", "record_id": "PER_c1",
                             "patch": {"name": "B"}},
                  "revision": {"expected_revision": 2}}[change])
    snapshot = state(env)
    refused("idempotency_mismatch", commit, env, other)
    assert state(env) == snapshot


def test_another_kind_under_the_id_is_idempotency_mismatch(env):
    proposal = propose_task(env, owner_ctx())
    snapshot = state(env)
    refused("idempotency_mismatch", commit, env, single(
        "create", patch={"title": "T", "status": TASK_OPEN}, request_id=proposal.request_id))
    assert state(env) == snapshot


@pytest.mark.parametrize("key", ["approval", "rejection", "decided_approval"])
def test_a_decision_or_receipt_key_is_refused_before_any_insert(env, key):
    proposal = propose_task(env, owner_ctx())
    if key == "decided_approval":
        env.decide(owner_ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    request_id = proposal.rejection_id if key == "rejection" else proposal.approval_id
    snapshot = state(env)
    refused("idempotency_mismatch", commit, env, single(
        "create", patch={"title": "T", "status": TASK_OPEN}, request_id=request_id))
    assert state(env) == snapshot


def test_a_fused_rejection_id_can_never_be_used_as_a_request_id(env):
    # It is a UUIDv5 (§5.3), and a commit id must be a UUIDv4, so it never reaches the keyspace.
    first = commit(env, single("create", patch={"title": "T", "status": TASK_OPEN}))
    snapshot = state(env)
    refused("invalid_request_id", commit, env, single(
        "create", patch={"title": "T", "status": TASK_OPEN},
        request_id=rejection_of(first["request_id"])))
    assert state(env) == snapshot


# --- body validation ---------------------------------------------------------------------------


@pytest.mark.parametrize("body,reason", [
    ([], "invalid_body"),
    ({"request_id": "x"}, "invalid_body"),
    (single("create") | {"source": "OS"}, "invalid_body"),
    (single("create") | {"op": "upsert"}, "invalid_op"),
    (single("create") | {"op": ["create"]}, "invalid_op"),
    (single("create") | {"entity": {"tasks": 1}}, "invalid_entity"),
    (single("create") | {"entity": "proposals"}, "invalid_entity"),
    (single("create", record_id="TAS_x"), "invalid_create"),
    (single("create", revision=1), "invalid_create"),
    (single("update", record_id="TAS_t1", revision=1), "empty_patch"),
    (single("update", record_id=None, revision=1, patch={"title": "x"}), "invalid_record_id"),
    (single("update", record_id="TAS_t1", revision=0, patch={"title": "x"}),
     "invalid_revision"),
    (single("update", record_id="TAS_t1", revision=True, patch={"title": "x"}),
     "invalid_revision"),
    (single("update", record_id="bad id", revision=1, patch={"title": "x"}),
     "invalid_record_id"),
    (single("update", record_id="TAS_t1", revision=1, patch={"revision": 3}),
     "non_input_field"),
    (single("update", record_id="TAS_t1", revision=1, patch={"task_id": "TAS_z"}),
     "non_input_field"),
    (single("complete", record_id="TAS_t1", revision=1, patch={"status": TASK_DONE}),
     "invalid_patch"),
    (single("complete", entity="clients", record_id="PER_c1", revision=1),
     "invalid_op_entity"),
    (single("reopen", entity="works", record_id="WOR_w1", revision=1), "invalid_op_entity"),
    (single("restore", entity="payments", record_id="PAY_p1", revision=1),
     "invalid_op_entity"),
    (single("create", patch={"status": TASK_OPEN}), "missing_title"),
    (single("create") | {"patch": []}, "invalid_patch"),
])
def test_a_malformed_body_is_a_validation_error_before_any_transaction(env, body, reason):
    snapshot = state(env)
    error = refused("validation_error", commit, env, body)
    assert error.details == {"reason": reason}
    assert state(env) == snapshot


@pytest.mark.parametrize("request_id", ["not-a-uuid", "6F1C7D2A-0B4E-4C2B-9A11-0D5E4B3C2A10",
                                        "00000000-0000-1000-8000-000000000001", None])
def test_the_request_id_must_be_a_canonical_uuid_v4(env, request_id):
    body = single("create", patch={"title": "T", "status": TASK_OPEN})
    body["request_id"] = request_id
    refused("invalid_request_id", commit, env, body)


# --- revisions and validate failures --------------------------------------------------------------


def test_a_revision_mismatch_is_a_200_target_changed_receipt_with_fresh_records(env):
    seed_tasks(env, "TAS_t1")
    commit_direct(env, owner_ctx(), [update(current(env, "tasks", "TAS_t1"), title="Moved")])
    fresh = current(env, "tasks", "TAS_t1")
    before = state(env)
    body = commit(env, single("update", record_id="TAS_t1", revision=1, patch={"title": "Mine"}))
    assert body["status"] == "target_changed" and body["committed_at"] is None
    assert body["records"] == [{"entity": "tasks", "record": fresh}]
    assert body["undo"] == {"state": "none", "until": None, "undo_request_id": None}
    assert current(env, "tasks", "TAS_t1") == fresh
    after = state(env)
    assert after["version"] == before["version"] + 1 == body["commit_version"]
    assert len(env.rows("os_outbox")) == len(before["os_outbox"])
    assert len(env.rows("os_events")) == len(before["os_events"]) + 1
    assert after["os_tasks"] == before["os_tasks"]


@pytest.mark.parametrize("op,record_id", [
    ("update", "TAS_none"), ("complete", "TAS_none"), ("delete", "TAS_none"),
    ("restore", "TAS_t1"), ("delete", "TAS_gone"), ("update", "TAS_gone"),
])
def test_a_missing_or_wrong_state_target_is_target_changed(env, op, record_id):
    seed_tasks(env, "TAS_t1", "TAS_gone")
    commit_direct(env, owner_ctx(), [tombstone("tasks", current(env, "tasks", "TAS_gone"))])
    before = state(env)
    patch = {"title": "x"} if op == "update" else {}
    revision = 2 if record_id == "TAS_gone" else 1
    body = commit(env, single(op, record_id=record_id, revision=revision, patch=patch))
    assert body["status"] == "target_changed"
    assert state(env)["os_tasks"] == before["os_tasks"]
    expected = [] if record_id == "TAS_none" else [record_id]
    assert [item["record"]["task_id"] for item in body["records"]] == expected


def test_a_validate_failure_is_target_changed_with_no_business_change(env):
    commit_direct(env, owner_ctx(), [
        client(), work(), payment(),
        payment("PAY_r1", original_payment_id="PAY_p1", kind=PAYMENT_REFUND,
                actual_amount_ils=100)])
    before = state(env)
    body = commit(env, single("update", entity="payments", record_id="PAY_r1", revision=1,
                              patch={"actual_amount_ils": 5000}))
    assert body["status"] == "target_changed"
    assert record_of(body, "PAY_r1") == current(env, "payments", "PAY_r1")
    assert current(env, "payments", "PAY_r1")["actual_amount_ils"] == 100
    assert state(env)["os_payments"] == before["os_payments"]


def test_a_create_that_fails_validation_is_target_changed_without_records(env):
    body = commit(env, single("create", patch={"title": "T", "status": TASK_OPEN,
                                                "work_id": "WOR_missing"}))
    assert body["status"] == "target_changed" and body["records"] == []
    assert env.rows("os_tasks") == []


# --- every single op on every entity -------------------------------------------------------------


CREATES = {
    "clients": client(), "works": work(), "payments": payment(), "time": time_entry(),
    "expenses": expense(), "tasks": task(), "leads": lead(),
}


@pytest.mark.parametrize("entity", list(CREATES))
def test_create_on_every_entity(env, entity):
    if entity in {"works", "payments", "time"}:
        commit_direct(env, owner_ctx(), [client()] + ([work()] if entity != "works" else []))
    body = commit(env, single("create", entity=entity, patch=patch_of(CREATES[entity])))
    assert body["status"] == "committed"
    [item] = body["records"]
    record = item["record"]
    key = api.ID_KEYS[entity]
    assert item["entity"] == entity and record["revision"] == 1
    assert record[key].startswith(owner_direct.ID_PREFIXES[entity] + "_")
    assert {k: v for k, v in record.items() if k not in {key, "revision", "person_id"}} == {
        k: v for k, v in patch_of(CREATES[entity]).items() if k != "person_id"}
    if entity == "leads":
        assert record["person_id"].startswith("PER_") and record["person_id"] != record[key]


def test_update_merges_the_patch_over_the_current_input_and_recanonicalizes(env):
    seed_tasks(env, "TAS_t1", notes="keep me")
    body = commit(env, single("update", record_id="TAS_t1", revision=1,
                              patch={"title": "  Renamed  ", "due_date": "2026-11-01"}))
    assert record_of(body, "TAS_t1") == {
        "task_id": "TAS_t1", "revision": 2, "title": "Renamed", "status": TASK_OPEN,
        "notes": "keep me", "due_date": "2026-11-01"}
    cleared = commit(env, single("update", record_id="TAS_t1", revision=2,
                                 patch={"notes": None}))
    assert "notes" not in record_of(cleared, "TAS_t1")


def test_update_of_a_lead_keeps_its_person_id(env):
    commit_direct(env, owner_ctx(), [lead()])
    body = commit(env, single("update", entity="leads", record_id="LEA_l1", revision=1,
                              patch={"name": "Renamed"}))
    record = record_of(body, "LEA_l1")
    assert record["person_id"] == "PER_l1" and record["name"] == "Renamed"


def test_an_update_that_breaks_a_merged_rule_is_a_validation_error(env):
    commit_direct(env, owner_ctx(), [
        client(), work(), payment(),
        payment("PAY_r1", original_payment_id="PAY_p1", kind=PAYMENT_REFUND,
                actual_amount_ils=100)])
    snapshot = state(env)
    # The patch alone is valid; only the merged record breaks the refund rule.
    error = refused("validation_error", commit, env, single(
        "update", entity="payments", record_id="PAY_r1", revision=1,
        patch={"original_payment_id": None}))
    assert error.details == {"reason": "refund_original_required"}
    assert state(env) == snapshot


def test_complete_and_reopen_set_the_task_status(env):
    seed_tasks(env, "TAS_t1")
    done = commit(env, single("complete", record_id="TAS_t1", revision=1))
    assert record_of(done, "TAS_t1")["status"] == TASK_DONE
    assert record_of(done, "TAS_t1")["revision"] == 2
    reopened = commit(env, single("reopen", record_id="TAS_t1", revision=2))
    assert record_of(reopened, "TAS_t1")["status"] == TASK_OPEN
    assert current(env, "tasks", "TAS_t1")["revision"] == 3


def test_delete_tombstones_a_task(env):
    seed_tasks(env, "TAS_t1")
    request_id = rid()
    body = commit(env, single("delete", record_id="TAS_t1", revision=1, request_id=request_id))
    assert body["status"] == "committed"
    assert record_of(body, "TAS_t1")["revision"] == 2
    assert current(env, "tasks", "TAS_t1") is None
    row = next(r for r in env.rows("os_tasks") if r["record_id"] == "TAS_t1")
    assert row["deleted_at"] is not None and row["deleted_by_request_id"] == request_id
    page = env.deleted(owner_ctx(), "tasks", after=None, limit=50)
    assert [r["task_id"] for r in page.records] == ["TAS_t1"]


DELETE_END = {
    "payments": ("PAY_p2", "kind", PAYMENT_CANCELLED),
    "expenses": ("EXP_e1", "payment_status", EXPENSE_CANCELLED),
    "works": ("WOR_w2", "status", WORK_CANCELLED),
    "time": ("TIM_t1", "status", TIME_CANCELLED),
    "clients": ("PER_c2", "status", CLIENT_INACTIVE),
    "leads": ("LEA_l1", "status", LEAD_IRRELEVANT),
}


@pytest.mark.parametrize("entity", list(DELETE_END))
def test_delete_sets_the_fork_delete_cancellation_status(env, entity):
    commit_direct(env, owner_ctx(), [
        client(), work(), payment(), client("PER_c2"), work("WOR_w2"),
        payment("PAY_p2", work_id="WOR_w2"), time_entry(), expense(), lead()])
    if entity == "works":
        commit_direct(env, owner_ctx(), [update(current(env, "payments", "PAY_p2"),
                                                kind=PAYMENT_CANCELLED)])
    record_id, column, value = DELETE_END[entity]
    before = current(env, entity, record_id)
    body = commit(env, single("delete", entity=entity, record_id=record_id,
                              revision=before["revision"]))
    assert body["status"] == "committed"
    after = current(env, entity, record_id)
    assert after is not None and after[column] == value
    assert after["revision"] == before["revision"] + 1
    assert {k: v for k, v in after.items() if k not in {column, "revision"}} == {
        k: v for k, v in before.items() if k not in {column, "revision"}}


def test_delete_of_a_refund_cancels_it_and_drops_the_original_link(env):
    commit_direct(env, owner_ctx(), [
        client(), work(), payment(),
        payment("PAY_r1", original_payment_id="PAY_p1", kind=PAYMENT_REFUND,
                actual_amount_ils=100)])
    body = commit(env, single("delete", entity="payments", record_id="PAY_r1", revision=1))
    assert body["status"] == "committed"
    refund = current(env, "payments", "PAY_r1")
    assert refund["kind"] == PAYMENT_CANCELLED and "original_payment_id" not in refund


def test_restore_brings_back_a_tombstoned_task_only(env):
    seed_tasks(env, "TAS_t1")
    commit(env, single("delete", record_id="TAS_t1", revision=1))
    body = commit(env, single("restore", record_id="TAS_t1", revision=2))
    assert body["status"] == "committed"
    restored = current(env, "tasks", "TAS_t1")
    assert restored["revision"] == 3 and restored["title"] == "Task"
    again = commit(env, single("restore", record_id="TAS_t1", revision=3))
    assert again["status"] == "target_changed"


# --- has_dependents over api.DEPENDENTS ----------------------------------------------------------


def test_deleting_a_payment_with_a_live_refund_is_has_dependents(env):
    commit_direct(env, owner_ctx(), [
        client(), work(), payment(),
        payment("PAY_r1", original_payment_id="PAY_p1", kind=PAYMENT_REFUND,
                actual_amount_ils=100)])
    snapshot = state(env)
    error = refused("has_dependents", commit, env,
                    single("delete", entity="payments", record_id="PAY_p1", revision=1))
    assert error.details == {"dependents": [{"entity": "payments", "record_id": "PAY_r1"}]}
    assert state(env) == snapshot
    commit(env, single("delete", entity="payments", record_id="PAY_r1", revision=1))
    body = commit(env, single("delete", entity="payments", record_id="PAY_p1", revision=1))
    assert body["status"] == "committed"


def test_deleting_a_client_with_live_works_is_has_dependents(env):
    commit_direct(env, owner_ctx(), [client(), work()])
    error = refused("has_dependents", commit, env,
                    single("delete", entity="clients", record_id="PER_c1", revision=1))
    assert error.details == {"dependents": [{"entity": "works", "record_id": "WOR_w1"}]}


def test_deleting_a_work_with_live_time_is_has_dependents(env):
    commit_direct(env, owner_ctx(), [client(), work(), time_entry()])
    error = refused("has_dependents", commit, env,
                    single("delete", entity="works", record_id="WOR_w1", revision=1))
    assert error.details == {"dependents": [{"entity": "time", "record_id": "TIM_t1"}]}


def test_a_done_task_never_blocks_a_delete(env):
    commit_direct(env, owner_ctx(), [client(), task("TAS_k1", person_id="PER_c1")])
    refused("has_dependents", commit, env,
            single("delete", entity="clients", record_id="PER_c1", revision=1))
    commit(env, single("complete", record_id="TAS_k1", revision=1))
    body = commit(env, single("delete", entity="clients", record_id="PER_c1", revision=1))
    assert body["status"] == "committed"
    assert current(env, "clients", "PER_c1")["status"] == CLIENT_INACTIVE


def test_has_dependents_lists_at_most_twenty_refs(env):
    commit_direct(env, owner_ctx(), [client()] + [
        task(f"TAS_k{i:02d}", person_id="PER_c1") for i in range(25)])
    error = refused("has_dependents", commit, env,
                    single("delete", entity="clients", record_id="PER_c1", revision=1))
    assert len(error.details["dependents"]) == 20


# --- bulk -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("items,reason", [
    ([], "invalid_items"),
    ([(f"TAS_{i:03d}", 1) for i in range(101)], "invalid_items"),
    ([("TAS_a01", 1), ("TAS_a01", 1)], "duplicate_item"),
    ([("TAS_a01", 1), ("TAS_a01", 2)], "duplicate_item"),
    ([("TAS_a01", 0)], "invalid_revision"),
    ([("bad id", 1)], "invalid_record_id"),
])
def test_a_bulk_needs_one_to_a_hundred_unique_pairs(env, items, reason):
    snapshot = state(env)
    error = refused("validation_error", commit, env, bulk("bulk_complete", items))
    assert error.details == {"reason": reason}
    assert state(env) == snapshot


@pytest.mark.parametrize("change,reason", [
    ({"entity": "clients"}, "invalid_op_entity"),
    ({"extra": 1}, "invalid_body"),
    ({"items": [{"record_id": "TAS_a01"}]}, "invalid_items"),
    ({"items": "TAS_a01"}, "invalid_items"),
])
def test_a_malformed_bulk_body_is_refused(env, change, reason):
    body = bulk("bulk_delete_done", [("TAS_a01", 1)]) | change
    error = refused("validation_error", commit, env, body)
    assert error.details == {"reason": reason}


def test_a_hundred_items_commit_in_one_receipt(env):
    ids = [f"TAS_h{i:03d}" for i in range(100)]
    seed_tasks(env, *ids)
    before = state(env)
    body = commit(env, bulk("bulk_complete", [(i, 1) for i in ids]))
    assert body["status"] == "committed"
    assert [item["record"]["task_id"] for item in body["records"]] == ids
    assert all(current(env, "tasks", i)["status"] == TASK_DONE for i in ids)
    assert len(env.rows("os_outbox")) == len(before["os_outbox"]) + 1
    assert env.version() == before["version"] + 1


def test_bulk_complete_is_all_or_nothing(env):
    ids = ["TAS_b1", "TAS_b2", "TAS_b3"]
    seed_tasks(env, *ids)
    commit_direct(env, owner_ctx(), [update(current(env, "tasks", "TAS_b3"), title="Moved")])
    before = state(env)
    body = commit(env, bulk("bulk_complete", [(i, 1) for i in ids]))
    assert body["status"] == "target_changed"
    assert [item["record"]["task_id"] for item in body["records"]] == ids
    assert state(env)["os_tasks"] == before["os_tasks"]
    good = commit(env, bulk("bulk_complete", [("TAS_b1", 1), ("TAS_b2", 1), ("TAS_b3", 2)]))
    assert good["status"] == "committed"
    assert [current(env, "tasks", i)["status"] for i in ids] == [TASK_DONE] * 3


def test_bulk_delete_done_with_any_open_item_is_target_changed_for_the_whole_bulk(env):
    seed_tasks(env, "TAS_d1", "TAS_d2", status=TASK_DONE)
    seed_tasks(env, "TAS_o1")
    before = state(env)
    body = commit(env, bulk("bulk_delete_done", [("TAS_d1", 1), ("TAS_o1", 1), ("TAS_d2", 1)]))
    assert body["status"] == "target_changed"
    assert state(env)["os_tasks"] == before["os_tasks"]
    good = commit(env, bulk("bulk_delete_done", [("TAS_d1", 1), ("TAS_d2", 1)]))
    assert good["status"] == "committed"
    assert current(env, "tasks", "TAS_d1") is None and current(env, "tasks", "TAS_d2") is None
    assert current(env, "tasks", "TAS_o1")["status"] == TASK_OPEN


def test_bulk_fused_columns_name_the_first_item(env):
    seed_tasks(env, "TAS_b1", "TAS_b2")
    request_id = rid()
    commit(env, bulk("bulk_complete", [("TAS_b2", 1), ("TAS_b1", 1)], request_id))
    proposal = next(p for p in env.rows("os_proposals") if p["request_id"] == request_id)
    assert (proposal["entity"], proposal["record_id"], proposal["expected_revision"]) == (
        "tasks", "TAS_b2", 1)
    assert proposal["dependency_digest"] == api.digest([
        {"entity": "tasks", "record_id": "TAS_b2", "revision": 1},
        {"entity": "tasks", "record_id": "TAS_b1", "revision": 1}])


def test_an_update_into_the_cancellation_status_with_live_dependents_is_has_dependents(env):
    # Review r1: an update that lands a record in its FORK-DELETE end state is a delete, so it
    # meets the same dependents check (a cancelled client may not keep a live work).
    commit_direct(env, owner_ctx(), [client(), work()])
    snapshot = state(env)
    error = refused("has_dependents", commit, env,
                    single("update", entity="clients", record_id="PER_c1", revision=1,
                           patch={"status": CLIENT_INACTIVE}))
    assert error.details == {"dependents": [{"entity": "works", "record_id": "WOR_w1"}]}
    assert state(env) == snapshot
