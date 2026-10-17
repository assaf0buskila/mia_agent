"""V2-24 undo, abandon, admission, write accounting and logs at the service level (§11.1, §5.3).

Runs on the fake and (once V2-16's ``core.py`` lands) PostgreSQL through ``LedgerEnv``.
"""

from __future__ import annotations

import logging
import re
import threading

import pytest
from app.services.os_ledger import api, owner_direct

from tests.fakes.os_ledger_builders import (
    client,
    commit_direct,
    current,
    propose_task,
    rejection_of,
    rid,
    task,
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
    single,
    state,
)


@pytest.fixture(params=BACKENDS)
def env(request):
    return ledger_case(request.param)


def settings_of(env, **kwargs):
    return KnobSettings(env, **kwargs)


def commit(env, body, *, context=None, settings=None):
    return owner_direct.commit(env, context or owner_ctx(), body,
                               settings=settings or settings_of(env))


def undo(env, request_id, undo_request_id=None, *, context=None, settings=None):
    return owner_direct.undo(env, context or owner_ctx(), request_id,
                             {"undo_request_id": undo_request_id or rid()},
                             settings=settings or settings_of(env))


def abandon(env, request_id, *, context=None, settings=None, body=None):
    return owner_direct.abandon(env, context or owner_ctx(), request_id,
                                {} if body is None else body,
                                settings=settings or settings_of(env))


def refused(code, call, *args, **kwargs):
    with pytest.raises(api.LedgerError) as caught:
        call(*args, **kwargs)
    assert caught.value.code == code, caught.value.code
    assert caught.value.write_outcome == "not_started"
    return caught.value


def seeded(env, *ids, **inputs):
    commit_direct(env, owner_ctx(), [task(i, **inputs) for i in ids])


def receipt_view(env, request_id):
    return env.receipt(owner_ctx(), request_id)


# --- undo inside the window ----------------------------------------------------------------------


def test_undo_of_an_update_applies_the_before_image_at_revision_plus_one(env):
    seeded(env, "TAS_t1", title="Before")
    original = commit(env, single("update", record_id="TAS_t1", revision=1,
                                  patch={"title": "After"}))
    before = state(env)
    undo_id = rid()
    body = undo(env, original["request_id"], undo_id)
    assert (body["status"], body["kind"], body["request_id"]) == (
        "committed", "owner_undo", undo_id)
    assert body["undo_of"] == original["request_id"] and body["undo"] is None
    assert body["actor_source"] == "OS_DIRECT"
    [item] = body["records"]
    assert item["record"] == {"task_id": "TAS_t1", "revision": 3, "title": "Before",
                              "status": TASK_OPEN}
    assert current(env, "tasks", "TAS_t1") == item["record"]
    assert env.version() == before["version"] + 1 == body["commit_version"]
    assert len(env.rows("os_outbox")) == len(before["os_outbox"]) + 1
    view = receipt_view(env, original["request_id"]).undo
    assert (view.state, view.undo_request_id) == ("undone", undo_id)


def test_undo_of_a_create_deletes_and_undo_of_a_delete_restores(env):
    created = commit(env, single("create", patch={"title": "New", "status": TASK_OPEN}))
    task_id = created["records"][0]["record"]["task_id"]
    undo(env, created["request_id"])
    assert current(env, "tasks", task_id) is None
    seeded(env, "TAS_t1")
    deleted = commit(env, single("delete", record_id="TAS_t1", revision=1))
    undo(env, deleted["request_id"])
    restored = current(env, "tasks", "TAS_t1")
    assert restored is not None and restored["revision"] == 3


def test_undo_of_a_non_task_create_is_that_entity_delete(env):
    created = commit(env, single("create", entity="clients",
                                 patch={"name": "C", "status": "פעיל"}))
    person = created["records"][0]["record"]["person_id"]
    undo(env, created["request_id"])
    assert current(env, "clients", person)["status"] == "לא פעיל"


def test_undo_of_a_complete_restores_the_prior_status(env):
    seeded(env, "TAS_t1")
    done = commit(env, single("complete", record_id="TAS_t1", revision=1))
    undo(env, done["request_id"])
    final = current(env, "tasks", "TAS_t1")
    assert (final["status"], final["revision"]) == (TASK_OPEN, 3)


def test_undo_of_a_restore_tombstones_again(env):
    seeded(env, "TAS_t1")
    commit(env, single("delete", record_id="TAS_t1", revision=1))
    restored = commit(env, single("restore", record_id="TAS_t1", revision=2))
    undo(env, restored["request_id"])
    assert current(env, "tasks", "TAS_t1") is None
    assert current(env, "tasks", "TAS_t1", include_deleted=True)["revision"] == 4


def test_the_undo_fused_columns_follow_the_contract(env):
    seeded(env, "TAS_t1")
    original = commit(env, single("complete", record_id="TAS_t1", revision=1))
    undo_id = rid()
    undo(env, original["request_id"], undo_id)
    proposal = next(p for p in env.rows("os_proposals") if p["request_id"] == undo_id)
    assert (proposal["kind"], proposal["source"], proposal["entity"]) == (
        "owner_undo", "OS_DIRECT", "tasks")
    assert (proposal["record_id"], proposal["expected_revision"]) == ("TAS_t1", 2)
    stored = next(r for r in env.rows("os_receipts")
                  if r["request_id"] == original["request_id"])
    assert proposal["dependency_digest"] == api.digest([dict(r) for r in stored["records"]])
    assert proposal["rejection_id"] == rejection_of(undo_id)


# --- undo refusals --------------------------------------------------------------------------------


def test_after_the_window_undo_is_expired_and_writes_nothing(env):
    seeded(env, "TAS_t1")
    original = commit(env, single("complete", record_id="TAS_t1", revision=1))
    env.advance(env.knobs.undo_window_s + 1)
    snapshot = state(env)
    refused("undo_expired", undo, env, original["request_id"])
    assert state(env) == snapshot


def test_after_another_edit_undo_is_a_record_changed_conflict(env):
    seeded(env, "TAS_t1")
    original = commit(env, single("complete", record_id="TAS_t1", revision=1))
    commit(env, single("update", record_id="TAS_t1", revision=2, patch={"title": "Later"}))
    snapshot = state(env)
    error = refused("undo_conflict", undo, env, original["request_id"])
    assert error.details == {"reason": "record_changed",
                             "changed": [{"entity": "tasks", "record_id": "TAS_t1"}]}
    assert state(env) == snapshot


def test_a_created_record_with_a_live_referrer_is_a_dependent_exists_conflict(env):
    created = commit(env, single("create", entity="clients",
                                 patch={"name": "C", "status": "פעיל"}))
    person = created["records"][0]["record"]["person_id"]
    commit_direct(env, owner_ctx(), [work("WOR_w9", person_id=person)])
    snapshot = state(env)
    error = refused("undo_conflict", undo, env, created["request_id"])
    assert error.details == {"reason": "dependent_exists",
                             "changed": [{"entity": "works", "record_id": "WOR_w9"}]}
    assert state(env) == snapshot


def test_an_undo_that_would_cancel_a_referenced_record_is_a_dependent_exists_conflict(env):
    commit_direct(env, owner_ctx(), [client(status="לא פעיל")])
    revived = commit(env, single("update", entity="clients", record_id="PER_c1", revision=1,
                                 patch={"status": "פעיל"}))
    commit_direct(env, owner_ctx(), [work()])
    error = refused("undo_conflict", undo, env, revived["request_id"])
    assert error.details["reason"] == "dependent_exists"


def test_the_same_undo_request_id_replays_and_another_is_already_undone(env):
    seeded(env, "TAS_t1")
    original = commit(env, single("complete", record_id="TAS_t1", revision=1))
    undo_id = rid()
    first = undo(env, original["request_id"], undo_id)
    snapshot = state(env)
    assert undo(env, original["request_id"], undo_id) == first
    assert state(env) == snapshot
    error = refused("already_undone", undo, env, original["request_id"], rid())
    assert error.details == {"undo_request_id": undo_id}
    assert state(env) == snapshot


def test_an_undo_id_reused_for_another_original_is_idempotency_mismatch(env):
    seeded(env, "TAS_t1", "TAS_t2")
    one = commit(env, single("complete", record_id="TAS_t1", revision=1))
    two = commit(env, single("complete", record_id="TAS_t2", revision=1))
    undo_id = rid()
    undo(env, one["request_id"], undo_id)
    refused("idempotency_mismatch", undo, env, two["request_id"], undo_id)
    refused("idempotency_mismatch", undo, env, one["request_id"], two["request_id"])


def test_an_unknown_original_is_commit_not_found(env):
    snapshot = state(env)
    refused("commit_not_found", undo, env, rid())
    assert state(env) == snapshot


def test_an_original_that_is_not_a_committed_owner_direct_is_not_undoable(env):
    seeded(env, "TAS_t1")
    stale = commit(env, single("update", record_id="TAS_t1", revision=9, patch={"title": "x"}))
    assert stale["status"] == "target_changed"
    abandoned = abandon(env, rid())
    proposal = propose_task(env, owner_ctx())
    env.decide(owner_ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    snapshot = state(env)
    for original in (stale["request_id"], abandoned["request_id"], proposal.request_id):
        refused("not_undoable", undo, env, original)
    assert state(env) == snapshot


def test_an_undo_is_never_undoable(env):
    seeded(env, "TAS_t1")
    original = commit(env, single("complete", record_id="TAS_t1", revision=1))
    first = undo(env, original["request_id"])
    refused("not_undoable", undo, env, first["request_id"])


def test_a_bulk_undo_is_all_or_nothing(env):
    ids = ["TAS_b1", "TAS_b2", "TAS_b3"]
    seeded(env, *ids)
    original = commit(env, bulk("bulk_complete", [(i, 1) for i in ids]))
    commit(env, single("update", record_id="TAS_b2", revision=2, patch={"title": "Moved"}))
    snapshot = state(env)
    error = refused("undo_conflict", undo, env, original["request_id"])
    assert error.details["changed"] == [{"entity": "tasks", "record_id": "TAS_b2"}]
    assert state(env) == snapshot
    assert [current(env, "tasks", i)["status"] for i in ids] == [TASK_DONE] * 3


def test_a_bulk_undo_restores_every_item(env):
    ids = ["TAS_b1", "TAS_b2"]
    seeded(env, *ids, status=TASK_DONE)
    original = commit(env, bulk("bulk_delete_done", [(i, 1) for i in ids]))
    body = undo(env, original["request_id"])
    assert [item["record"]["task_id"] for item in body["records"]] == ids
    assert all(current(env, "tasks", i)["revision"] == 3 for i in ids)


@pytest.mark.parametrize("payload,code", [
    ({}, "validation_error"),
    ({"undo_request_id": "nope"}, "invalid_request_id"),
    ({"undo_request_id": rid(), "extra": 1}, "validation_error"),
    ([], "validation_error"),
])
def test_a_malformed_undo_body_is_refused(env, payload, code):
    seeded(env, "TAS_t1")
    original = commit(env, single("complete", record_id="TAS_t1", revision=1))
    refused(code, owner_direct.undo, env, owner_ctx(), original["request_id"], payload,
            settings=settings_of(env))
    refused("invalid_request_id", owner_direct.undo, env, owner_ctx(), "nope",
            {"undo_request_id": rid()}, settings=settings_of(env))


# --- abandon --------------------------------------------------------------------------------------


def test_abandon_writes_an_abandoned_receipt_one_event_and_no_outbox_row(env):
    before = state(env)
    request_id = rid()
    body = abandon(env, request_id)
    assert (body["status"], body["kind"], body["request_id"]) == (
        "abandoned", "owner_direct", request_id)
    assert body["records"] == [] and body["committed_at"] is None
    assert body["undo"] == {"state": "none", "until": None, "undo_request_id": None}
    assert env.version() == before["version"] + 1 == body["commit_version"]
    assert len(env.rows("os_events")) == len(before["os_events"]) + 1
    assert len(env.rows("os_outbox")) == len(before["os_outbox"])
    proposal = next(p for p in env.rows("os_proposals") if p["request_id"] == request_id)
    assert (proposal["entity"], proposal["record_id"], list(proposal["mutations"])) == (
        "abandon", request_id, [])
    assert proposal["request_digest"] == api.digest({"abandon": request_id})


def test_abandon_replays_and_returns_an_existing_commit_or_undo_receipt(env):
    request_id = rid()
    first = abandon(env, request_id)
    snapshot = state(env)
    assert abandon(env, request_id) == first
    assert state(env) == snapshot
    seeded(env, "TAS_t1")
    committed = commit(env, single("complete", record_id="TAS_t1", revision=1))
    undone = undo(env, committed["request_id"])
    snapshot = state(env)
    replayed = abandon(env, committed["request_id"])
    assert (replayed["status"], replayed["undo"]["state"]) == ("committed", "undone")
    assert abandon(env, undone["request_id"]) == undone
    assert state(env) == snapshot


def test_abandon_is_refused_on_another_kind_or_a_decision_key(env):
    proposal = propose_task(env, owner_ctx())
    decided = propose_task(env, owner_ctx())
    env.decide(owner_ctx(), decided.proposal_id, decided.approval_id, "approve")
    snapshot = state(env)
    for key in (proposal.request_id, proposal.approval_id, proposal.rejection_id,
                decided.approval_id):
        refused("idempotency_mismatch", abandon, env, key)
    assert state(env) == snapshot


def test_a_late_original_returns_the_abandoned_receipt(env):
    request_id = rid()
    abandoned = abandon(env, request_id)
    snapshot = state(env)
    late = commit(env, single("create", patch={"title": "Late", "status": TASK_OPEN},
                              request_id=request_id))
    assert late == abandoned
    assert state(env) == snapshot


def test_a_late_undo_returns_the_abandoned_receipt(env):
    seeded(env, "TAS_t1")
    original = commit(env, single("complete", record_id="TAS_t1", revision=1))
    undo_id = rid()
    abandoned = abandon(env, undo_id)
    assert undo(env, original["request_id"], undo_id) == abandoned
    assert current(env, "tasks", "TAS_t1")["status"] == TASK_DONE


@pytest.mark.parametrize("body", [{"x": 1}, [], "{}"])
def test_abandon_takes_an_empty_object(env, body):
    refused("validation_error", abandon, env, rid(), body=body)


# --- admission ------------------------------------------------------------------------------------


@pytest.mark.parametrize("channel", ["MIA", "TELEGRAM_APPROVAL", "ASSISTANT_CLAUDE",
                                     "ASSISTANT_GPT", "WORKER"])
def test_non_os_channels_are_refused_for_commit_undo_and_abandon(env, channel):
    seeded(env, "TAS_t1")
    original = commit(env, single("complete", record_id="TAS_t1", revision=1))
    context = owner_ctx(channel)
    snapshot = state(env)
    refused("channel_operation_denied", commit, env,
            single("create", patch={"title": "T", "status": TASK_OPEN}), context=context)
    refused("channel_operation_denied", undo, env, original["request_id"], context=context)
    refused("channel_operation_denied", abandon, env, rid(), context=context)
    assert state(env) == snapshot


@pytest.mark.parametrize("fields,code", [
    ({"scopes": frozenset({"assaf-os:read", "assaf-os:propose", "assaf-os:approve"})},
     "scope_denied"),
    ({"browser_mutation_verified": False}, "browser_mutation_verification_required"),
    ({"owner": 999}, "owner_auth_required"),
    ({"expires_in": -5}, "owner_auth_required"),
])
def test_the_context_must_carry_the_direct_scope_a_verified_mutation_and_the_owner(
        env, fields, code):
    context = owner_ctx(**fields)
    snapshot = state(env)
    refused(code, commit, env, single("create", patch={"title": "T", "status": TASK_OPEN}),
            context=context)
    refused(code, abandon, env, rid(), context=context)
    assert state(env) == snapshot


def test_the_kill_switch_blocks_commit_undo_and_abandon(env):
    seeded(env, "TAS_t1")
    original = commit(env, single("complete", record_id="TAS_t1", revision=1))
    env.knobs.kill_switch = True
    snapshot = state(env)
    refused("kill_switch", commit, env, single("create", patch={"title": "T",
                                                                "status": TASK_OPEN}))
    refused("kill_switch", undo, env, original["request_id"])
    refused("kill_switch", abandon, env, rid())
    env.knobs.kill_switch = False
    assert state(env) == snapshot


def test_the_freeze_blocks_commit_and_undo_but_not_abandon(env):
    seeded(env, "TAS_t1")
    original = commit(env, single("complete", record_id="TAS_t1", revision=1))
    env.knobs.freeze = True
    snapshot = state(env)
    refused("os_frozen", commit, env, single("create", patch={"title": "T",
                                                              "status": TASK_OPEN}))
    refused("os_frozen", undo, env, original["request_id"])
    assert state(env) == snapshot
    assert abandon(env, rid())["status"] == "abandoned"


def test_the_feature_flag_and_the_backend_gate_the_service(env):
    body = single("create", patch={"title": "T", "status": TASK_OPEN})
    refused("operation_unavailable", commit, env, body,
            settings=settings_of(env, direct=False))
    env.knobs.backend = "sheets"
    refused("operation_unavailable", commit, env, body)
    refused("operation_unavailable", abandon, env, rid())
    env.knobs.backend = "postgres"


def test_an_epoch_mismatch_is_authority_changed(env):
    refused("authority_changed", commit, env,
            single("create", patch={"title": "T", "status": TASK_OPEN}),
            context=owner_ctx(epoch=2))


# --- writes: source, events and outbox ------------------------------------------------------------


def test_one_outbox_row_per_commit_and_per_undo_and_none_otherwise(env):
    seeded(env, "TAS_t1")
    def count():
        return len(env.rows("os_outbox"))

    start = count()
    original = commit(env, single("complete", record_id="TAS_t1", revision=1))
    assert count() == start + 1
    undo(env, original["request_id"])
    assert count() == start + 2
    commit(env, single("complete", record_id="TAS_t1", revision=1))  # stale: target_changed
    abandon(env, rid())
    assert count() == start + 2
    outbox = env.rows("os_outbox")[-1]
    assert outbox["body"] == {"authority_epoch": 1, "refs": [
        {"entity": "tasks", "record_id": "TAS_t1", "revision": 3}]}


def test_the_source_is_os_direct_assigned_by_the_server(env):
    body = commit(env, single("create", patch={"title": "T", "status": TASK_OPEN}))
    task_id = body["records"][0]["record"]["task_id"]
    row = next(r for r in env.rows("os_tasks") if r["record_id"] == task_id)
    proposal = next(p for p in env.rows("os_proposals")
                    if p["request_id"] == body["request_id"])
    assert row["source"] == "OS_DIRECT" == proposal["source"] == body["actor_source"]


# --- a commit/undo race serializes ----------------------------------------------------------------


def test_a_commit_and_an_undo_of_the_same_record_serialize(env):
    seeded(env, "TAS_t1")
    original = commit(env, single("complete", record_id="TAS_t1", revision=1))
    barrier = threading.Barrier(2)
    outcomes = {}

    def run(name, call):
        barrier.wait()
        try:
            outcomes[name] = call()["status"]
        except api.LedgerError as exc:
            outcomes[name] = exc.code

    threads = [
        threading.Thread(target=run, args=("undo", lambda: undo(env, original["request_id"]))),
        threading.Thread(target=run, args=("edit", lambda: commit(env, single(
            "update", record_id="TAS_t1", revision=2, patch={"title": "Raced"})))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    final = current(env, "tasks", "TAS_t1")
    assert outcomes in ({"undo": "committed", "edit": "target_changed"},
                        {"undo": "undo_conflict", "edit": "committed"})
    if outcomes["undo"] == "committed":
        assert (final["status"], final["title"], final["revision"]) == (TASK_OPEN, "Task", 3)
    else:
        assert (final["status"], final["title"], final["revision"]) == (TASK_DONE, "Raced", 3)


# --- logs -----------------------------------------------------------------------------------------

LOG_LINE = re.compile(r"os_commit op=[a-z_]+ outcome=[a-z_]+ ms=[0-9]+")


def test_logs_carry_the_op_the_outcome_and_ms_only(env, caplog):
    secret = "PRIVATE title never logged"
    caplog.set_level(logging.DEBUG)
    seeded(env, "TAS_t1")
    created = commit(env, single("create", patch={"title": secret, "status": TASK_OPEN}))
    refused("validation_error", commit, env, single("update", record_id="TAS_t1", revision=1,
                                                    patch={"title": secret, "bad": 1}))
    undo(env, created["request_id"])
    abandon(env, rid())
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("os_commit")]
    assert [line.rsplit(" ms=", 1)[0] for line in lines] == [
        "os_commit op=create outcome=committed",
        "os_commit op=update outcome=validation_error",
        "os_commit op=undo outcome=committed",
        "os_commit op=abandon outcome=abandoned",
    ]
    assert all(LOG_LINE.fullmatch(line) for line in lines)
    assert secret not in caplog.text
