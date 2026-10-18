"""Owner-direct at the LedgerTx level (§5.3, §11.1; decision D6).

The service paths (``has_dependents``, ``undo_conflict``, ``undo_expired``, bulk) are V2-24's tests,
written against this same ``ledger`` fixture. Here: fused-kind columns, the receipt/undo state,
``dependents()``, abandoned replay, one event and at most one outbox row per finish, bulk atomicity
and the ``apply`` revision rules.
"""

from datetime import timedelta

import pytest
from app.services.os_ledger import api

from tests.fakes.os_ledger_builders import (
    PAYMENT_CANCELLED,
    PAYMENT_REFUND,
    TASK_DONE,
    TASK_OPEN,
    TIME_CANCELLED,
    WORK_CANCELLED,
    abandon_draft,
    before_images,
    client,
    commit_direct,
    commit_import,
    commit_undo,
    counts,
    ctx,
    current,
    direct_draft,
    expense,
    fresh_workspace,
    import_draft,
    invariant_violation,
    lead,
    payment,
    propose_task,
    rejection_of,
    restore,
    rid,
    task,
    time_entry,
    tombstone,
    undo_draft,
    update,
    window_s,
    work,
)

OPEN_TX = dict(domain="core", kind="owner_direct")


def row_of(env, table, record_id):
    return next(r for r in env.rows(table) if r["record_id"] == record_id)


def refs(*pairs):
    return [{"entity": e, "record_id": i, "revision": r} for e, i, r in pairs]


def test_a_committed_direct_commit_writes_one_receipt_one_event_one_outbox_row(ledger):
    request_id = rid()
    proposal, receipt = commit_direct(ledger, ctx(), [task()], request_id)
    after = ledger.rows("os_tasks")
    assert len(after) == 1 and after[0]["source"] == "OS_DIRECT" and after[0]["revision"] == 1
    assert (receipt.status, receipt.kind, receipt.request_id) == (
        "committed", "owner_direct", request_id)
    assert receipt.commit_version == 1 == ledger.version()
    assert receipt.records == ({"entity": "tasks", "record_id": "TAS_t1", "revision": 1,
                                "digest": api.digest(proposal.mutations[0].after)},)
    assert receipt.undo_until == receipt.completed_at + timedelta(seconds=window_s(ledger))
    events = ledger.rows("os_events")
    assert len(events) == 1 and events[0]["decision_id"] == request_id
    assert events[0]["body"]["status"] == "committed"
    assert events[0]["body"]["kind"] == "owner_direct"
    assert events[0]["body"]["effects"] == refs(("tasks", "TAS_t1", 1))
    outbox = ledger.rows("os_outbox")
    assert len(outbox) == 1 and outbox[0]["status"] == "pending"
    assert outbox[0]["body"] == {"authority_epoch": 1, "refs": refs(("tasks", "TAS_t1", 1))}
    assert outbox[0]["commit_version"] == 1 and events[0]["commit_version"] == 1


def test_apply_writes_the_typed_columns(ledger):
    commit_direct(ledger, ctx(), [
        client(), work(agreed_amount_ils=5000), payment(), time_entry(),
        task(due_date="2026-11-01"), expense(), lead()])
    assert str(row_of(ledger, "os_tasks", "TAS_t1")["business_date"]) == "2026-11-01"
    pay = row_of(ledger, "os_payments", "PAY_p1")
    assert (pay["person_id"], pay["work_id"], int(pay["amount_minor"])) == (
        "PER_c1", "WOR_w1", 1000)
    assert pay["currency"] == "ILS" and str(pay["business_date"]) == "2026-10-01"
    assert int(row_of(ledger, "os_works", "WOR_w1")["agreed_amount_minor"]) == 5000
    assert row_of(ledger, "os_time", "TIM_t1")["minutes"] == 30
    exp = row_of(ledger, "os_expenses", "EXP_e1")
    assert (exp["source_type"], exp["source_key"]) == (
        current(ledger, "expenses", "EXP_e1")["source_type"], "key-1")
    assert int(exp["amount_minor"]) == 500 and exp["source_digest"]
    assert row_of(ledger, "os_leads", "LEA_l1")["person_id"] == "PER_l1"
    for table in ("os_tasks", "os_payments", "os_leads"):
        for row in ledger.rows(table):
            assert row["updated_at"] >= row["created_at"]
            assert row["deleted_at"] is None and row["deleted_by_request_id"] is None


# --- fused-kind columns (§5.3) ---------------------------------------------------------------


def inserted(env, kind, build):
    with env.transaction(ctx(), domain="core", kind=kind) as tx:
        return tx.insert_proposal(build(tx))


def assert_fused(proposal, request_id, *, entity, record_id, revision, digest, window):
    assert proposal.approval_id == request_id
    assert proposal.rejection_id == rejection_of(request_id)
    assert (proposal.entity, proposal.record_id) == (entity, record_id)
    assert proposal.expected_revision == revision and proposal.dependency_digest == digest
    assert proposal.expires_at == proposal.created_at + timedelta(seconds=window)
    assert proposal.status == "pending" and proposal.created_at < proposal.expires_at


def test_fused_columns_of_an_owner_direct_create(ledger):
    request_id, window = rid(), window_s(ledger)
    mutations = [task()]
    proposal = inserted(ledger, "owner_direct",
                        lambda tx: direct_draft(tx, mutations, request_id, window))
    assert_fused(proposal, request_id, entity="tasks", record_id="TAS_t1", revision=0,
                 digest=api.digest([{"entity": "tasks", "record_id": "TAS_t1", "revision": 0}]),
                 window=window)
    assert (proposal.kind, proposal.source, proposal.mutations) == (
        "owner_direct", "OS_DIRECT", tuple(mutations))


def test_fused_columns_of_an_owner_direct_update_and_bulk(ledger):
    commit_direct(ledger, ctx(), [task(), task("TAS_t2"), task("TAS_t3")])
    first, second = current(ledger, "tasks", "TAS_t1"), current(ledger, "tasks", "TAS_t2")
    mutations = [update(second, status=TASK_DONE), update(first, status=TASK_DONE)]
    request_id, window = rid(), window_s(ledger)
    proposal = inserted(ledger, "owner_direct",
                        lambda tx: direct_draft(tx, mutations, request_id, window))
    assert_fused(proposal, request_id, entity="tasks", record_id="TAS_t2", revision=1,
                 digest=api.digest(before_images(mutations)), window=window)
    assert [i["record_id"] for i in before_images(mutations)] == ["TAS_t2", "TAS_t1"]


def test_fused_columns_of_an_abandon(ledger):
    request_id, window = rid(), window_s(ledger)
    proposal = inserted(ledger, "owner_direct", lambda tx: abandon_draft(tx, request_id, window))
    assert_fused(proposal, request_id, entity="abandon", record_id=request_id, revision=0,
                 digest=api.digest([]), window=window)
    assert proposal.kind == "owner_direct" and proposal.mutations == ()


def test_fused_columns_of_an_import(ledger):
    fresh_workspace(ledger, "ws-i")
    manifest, batch = api.digest({"m": 1}), rid()
    window = window_s(ledger)
    request_id = api.import_request_uuid(1, manifest)
    with ledger.transaction(ctx(workspace="ws-i"), domain="core", kind="import") as tx:
        proposal = tx.insert_proposal(import_draft(tx, batch, manifest, 1, window))
    assert_fused(proposal, request_id, entity="import", record_id=batch, revision=0,
                 digest=manifest, window=window)
    assert proposal.kind == "import" and proposal.mutations == ()


def test_fused_columns_of_an_undo(ledger):
    _, original = commit_direct(ledger, ctx(), [task()])
    undo_id, window = rid(), window_s(ledger)
    mutations = [tombstone("tasks", current(ledger, "tasks", "TAS_t1"))]
    proposal = inserted(ledger, "owner_undo", lambda tx: undo_draft(
        tx, original, undo_id, mutations, window, "tasks", "TAS_t1"))
    assert_fused(proposal, undo_id, entity="tasks", record_id="TAS_t1", revision=1,
                 digest=api.digest(list(original.records)), window=window)
    assert proposal.kind == "owner_undo" and proposal.source == "OS_DIRECT"


# --- receipt and derived undo state ----------------------------------------------------------


def undo_of_receipt(env, key):
    return env.receipt(ctx(), key).undo


def test_undo_state_is_available_then_expired_by_the_clock(ledger):
    _, receipt = commit_direct(ledger, ctx(), [task()])
    view = undo_of_receipt(ledger, receipt.request_id)
    assert (view.state, view.until, view.undo_request_id) == ("available", receipt.undo_until, None)
    ledger.advance(window_s(ledger) + 1)
    view = undo_of_receipt(ledger, receipt.request_id)
    assert (view.state, view.until) == ("expired", receipt.undo_until)


def test_undo_state_becomes_undone_by_a_derived_lookup_never_an_update(ledger):
    _, original = commit_direct(ledger, ctx(), [task()])
    assert ledger.version() == 1
    with ledger.transaction(ctx(), **OPEN_TX) as tx:
        assert tx.undo_receipt_of(original.request_id) is None
    undo_id = rid()
    _, undo = commit_undo(ledger, ctx(), original,
                          [tombstone("tasks", current(ledger, "tasks", "TAS_t1"))], undo_id)
    assert (undo.kind, undo.status, undo.undo_of, undo.undo_until) == (
        "owner_undo", "committed", original.request_id, None)
    with ledger.transaction(ctx(), **OPEN_TX) as tx:
        assert tx.undo_receipt_of(original.request_id) == undo
        assert tx.undo_receipt_of(undo.request_id) is None
        assert tx.undo_receipt_of(rid()) is None
    ledger.advance(10_000)
    view = undo_of_receipt(ledger, original.request_id)
    assert (view.state, view.undo_request_id) == ("undone", undo_id)
    assert undo_of_receipt(ledger, undo_id).state == "none"
    stored = next(r for r in ledger.rows("os_receipts") if r["request_id"] == original.request_id)
    assert stored["undo_until"] == original.undo_until  # never rewritten


def test_undo_of_a_create_tombstones_and_undo_of_a_delete_restores(ledger):
    _, created = commit_direct(ledger, ctx(), [task()])
    before = current(ledger, "tasks", "TAS_t1")
    _, undo = commit_undo(ledger, ctx(), created, [tombstone("tasks", before)])
    assert current(ledger, "tasks", "TAS_t1") is None
    row = row_of(ledger, "os_tasks", "TAS_t1")
    assert row["revision"] == 2 and row["deleted_at"] == ledger.now()
    assert row["deleted_by_request_id"] == undo.request_id
    page = ledger.page(ctx(), "tasks", after=None, limit=50)
    assert page.total == 0 and page.records == ()
    deleted = ledger.deleted(ctx(), "tasks", after=None, limit=50)
    assert [r["task_id"] for r in deleted.records] == ["TAS_t1"]
    tomb = current(ledger, "tasks", "TAS_t1", include_deleted=True)
    _, restored = commit_direct(ledger, ctx(), [restore("tasks", tomb)])
    row = row_of(ledger, "os_tasks", "TAS_t1")
    assert row["revision"] == 3 and row["deleted_at"] is None
    assert row["deleted_by_request_id"] is None and restored.status == "committed"
    assert ledger.page(ctx(), "tasks", after=None, limit=50).total == 1


def test_undo_of_an_update_restores_the_prior_inputs(ledger):
    commit_direct(ledger, ctx(), [task(title="Before")])
    updated_before = current(ledger, "tasks", "TAS_t1")
    _, changed = commit_direct(ledger, ctx(), [update(updated_before, title="After")])
    now_current = current(ledger, "tasks", "TAS_t1")
    commit_undo(ledger, ctx(), changed, [update(now_current, title="Before")])
    final = current(ledger, "tasks", "TAS_t1")
    assert (final["title"], final["revision"]) == ("Before", 3)


def test_undo_state_is_none_for_target_changed_and_abandoned_receipts(ledger):
    request_id = rid()
    with ledger.transaction(ctx(), **OPEN_TX) as tx:
        proposal = tx.insert_proposal(direct_draft(tx, [task()], request_id, window_s(ledger)))
        receipt = tx.finish(proposal, status="target_changed", receipt_request_id=request_id)
    assert receipt.undo_until is None and receipt.records == ()
    result = ledger.receipt(ctx(), request_id)
    assert result.found and result.undo.state == "none"
    assert result.records == ()
    abandon_id = rid()
    with ledger.transaction(ctx(), **OPEN_TX) as tx:
        proposal = tx.insert_proposal(abandon_draft(tx, abandon_id, window_s(ledger)))
        receipt = tx.finish(proposal, status="abandoned", receipt_request_id=abandon_id)
    assert receipt.undo_until is None
    assert ledger.receipt(ctx(), abandon_id).undo.state == "none"
    ledger.advance(10_000)
    assert ledger.receipt(ctx(), request_id).undo.state == "none"


def test_other_kinds_never_carry_an_available_undo(ledger):
    proposal = propose_task(ledger)
    committed = ledger.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    assert committed.receipt.undo_until is None
    assert committed.undo is None or committed.undo.state == "none"


def test_a_receipt_may_be_undone_by_at_most_one_undo_receipt(ledger):
    _, original = commit_direct(ledger, ctx(), [task()])
    commit_undo(ledger, ctx(), original, [tombstone("tasks", current(ledger, "tasks", "TAS_t1"))])
    tomb = current(ledger, "tasks", "TAS_t1", include_deleted=True)
    with invariant_violation(ledger, "receipt_undo_unique"):
        commit_undo(ledger, ctx(), original, [restore("tasks", tomb)])


@pytest.mark.parametrize("bad,name", [
    ("missing_original", "receipt_undo_fk"),
    ("self", "receipt_undo_self"),
])
def test_undo_of_must_name_another_existing_receipt(ledger, bad, name):
    undo_id = rid()
    with invariant_violation(ledger, name):
        with ledger.transaction(ctx(), domain="core", kind="owner_undo") as tx:
            proposal = tx.insert_proposal(api.ProposalDraft(
                kind="owner_undo", source="OS_DIRECT", request_id=undo_id,
                request_digest=api.digest({"u": 1}), entity="tasks", record_id="TAS_t1",
                expected_revision=1, mutations=(), dependency_digest=api.digest([]),
                expires_at=tx.now + timedelta(seconds=window_s(ledger))))
            tx.finish(proposal, status="committed", receipt_request_id=undo_id,
                      undo_of=undo_id if bad == "self" else rid())


# --- dependents() over api.DEPENDENTS and api.is_live ----------------------------------------


def dependents(env, entity, record_id):
    with env.transaction(ctx(), **OPEN_TX) as tx:
        return {(r.entity, r.record_id) for r in tx.dependents(entity, record_id)}


def chain(env):
    commit_direct(env, ctx(), [
        client(), work(), payment(), time_entry(),
        task("TAS_k1", person_id="PER_c1", work_id="WOR_w1"),
        task("TAS_k2", person_id="PER_c1"),
        payment("PAY_r1", original_payment_id="PAY_p1", kind=PAYMENT_REFUND,
                actual_amount_ils=100),
        task("TAS_free"), expense(), lead()])


def test_dependents_walk_the_pinned_dependency_table(ledger):
    chain(ledger)
    assert dependents(ledger, "clients", "PER_c1") == {
        ("works", "WOR_w1"), ("payments", "PAY_p1"), ("payments", "PAY_r1"),
        ("time", "TIM_t1"), ("tasks", "TAS_k1"), ("tasks", "TAS_k2")}
    assert dependents(ledger, "works", "WOR_w1") == {
        ("payments", "PAY_p1"), ("payments", "PAY_r1"), ("time", "TIM_t1"), ("tasks", "TAS_k1")}
    assert dependents(ledger, "payments", "PAY_p1") == {("payments", "PAY_r1")}
    for entity, record_id in (("payments", "PAY_r1"), ("tasks", "TAS_free"),
                              ("expenses", "EXP_e1"), ("leads", "LEA_l1"), ("clients", "PER_x")):
        assert dependents(ledger, entity, record_id) == set()
    assert {(e, c) for e, c, _ in api.DEPENDENTS if e == "payments"} >= {
        ("payments", "work_id"), ("payments", "person_id")}


def test_dependents_ignore_referrers_that_are_not_live(ledger):
    chain(ledger)
    steps = [
        (lambda: update(current(ledger, "time", "TIM_t1"), status=TIME_CANCELLED),
         ("time", "TIM_t1")),
        (lambda: update(current(ledger, "tasks", "TAS_k1"), status=TASK_DONE), ("tasks", "TAS_k1")),
        (lambda: tombstone("tasks", current(ledger, "tasks", "TAS_k2")), ("tasks", "TAS_k2")),
        (lambda: update(current(ledger, "payments", "PAY_r1"), kind=PAYMENT_CANCELLED,
                        original_payment_id=None), ("payments", "PAY_r1")),
    ]
    expected = dependents(ledger, "clients", "PER_c1")
    for build, ref in steps:
        commit_direct(ledger, ctx(), [build()])
        expected = expected - {ref}
        assert dependents(ledger, "clients", "PER_c1") == expected
    assert dependents(ledger, "clients", "PER_c1") == {("works", "WOR_w1"), ("payments", "PAY_p1")}
    assert dependents(ledger, "works", "WOR_w1") == {("payments", "PAY_p1")}
    assert dependents(ledger, "payments", "PAY_p1") == set()
    # §22 E20: an update can no longer cancel a work under a live payment, so this case builds
    # the cancelled-work referrer by a create (a first import may hold one). The cancelled work is
    # ignored and its live payment is still reported, which is what the case pins.
    commit_direct(ledger, ctx(), [client("PER_c2"), work("WOR_w2", "PER_c2", status=WORK_CANCELLED),
                                  payment("PAY_p2", "PER_c2", "WOR_w2")])
    assert dependents(ledger, "clients", "PER_c2") == {("payments", "PAY_p2")}


# --- abandoned wins; one event and at most one outbox row per finish -------------------------


def test_abandon_writes_a_receipt_and_an_event_but_no_outbox_row(ledger):
    request_id = rid()
    with ledger.transaction(ctx(), **OPEN_TX) as tx:
        proposal = tx.insert_proposal(abandon_draft(tx, request_id, window_s(ledger)))
        receipt = tx.finish(proposal, status="abandoned", receipt_request_id=request_id)
    assert (receipt.status, receipt.kind, receipt.records) == ("abandoned", "owner_direct", ())
    assert ledger.version() == 1 and len(ledger.rows("os_events")) == 1
    assert ledger.rows("os_events")[0]["body"]["status"] == "abandoned"
    assert ledger.rows("os_outbox") == []


def test_a_late_original_finds_the_abandoned_receipt(ledger):
    request_id = rid()
    with ledger.transaction(ctx(), **OPEN_TX) as tx:
        proposal = tx.insert_proposal(abandon_draft(tx, request_id, window_s(ledger)))
        tx.finish(proposal, status="abandoned", receipt_request_id=request_id)
    with ledger.transaction(ctx(), **OPEN_TX) as tx:
        stored = tx.proposal_by_request(request_id)
        assert stored.status == "abandoned" and stored.kind == "owner_direct"
        assert tx.receipt(request_id).status == "abandoned"
        assert tx.receipt_for_proposal(stored.proposal_id).request_id == request_id
    found = ledger.creation_request(ctx(), request_id)
    assert found.found and found.proposal.status == "abandoned"
    assert found.decision.status == "abandoned"


@pytest.mark.parametrize("status", ("committed", "target_changed"))
def test_a_direct_finish_writes_one_event_and_an_outbox_row_only_when_committed(ledger, status):
    """A fused kind finishes only committed or target_changed (abandoned has its own test)."""
    request_id = rid()
    with ledger.transaction(ctx(), **OPEN_TX) as tx:
        mutations = [task()]
        proposal = tx.insert_proposal(direct_draft(tx, mutations, request_id, window_s(ledger)))
        key = request_id
        applied = ()
        if status == "committed":
            tx.apply(mutations, source="OS_DIRECT", request_id=request_id)
            applied = mutations
        tx.finish(proposal, status=status, receipt_request_id=key, applied=applied)
    assert ledger.version() == 1 and len(ledger.rows("os_events")) == 1
    assert len(ledger.rows("os_outbox")) == (1 if status == "committed" else 0)
    receipt = ledger.rows("os_receipts")[0]
    assert (receipt["status"], receipt["commit_version"]) == (status, 1)
    assert (len(receipt["records"]) == 1) == (status == "committed")


# --- bulk -----------------------------------------------------------------------------------


def test_bulk_commit_is_one_proposal_one_receipt_one_outbox_row_in_order(ledger):
    ids = ["TAS_b3", "TAS_b1", "TAS_b2"]
    commit_direct(ledger, ctx(), [task(i) for i in ids])
    after = counts(ledger)
    mutations = [update(current(ledger, "tasks", i), status=TASK_DONE) for i in ids]
    _, receipt = commit_direct(ledger, ctx(), mutations)
    final = counts(ledger)
    assert final["os_proposals"] == after["os_proposals"] + 1
    assert final["os_receipts"] == after["os_receipts"] + 1
    assert final["os_outbox"] == after["os_outbox"] + 1
    assert final["version"] == after["version"] + 1
    assert [r["record_id"] for r in receipt.records] == ids
    assert ledger.rows("os_outbox")[-1]["body"]["refs"] == refs(*[("tasks", i, 2) for i in ids])
    assert all(current(ledger, "tasks", i)["status"] == TASK_DONE for i in ids)


def test_a_failing_bulk_leaves_no_partial_write_and_records_a_target_changed_receipt(ledger):
    ids = ["TAS_b1", "TAS_b2", "TAS_b3"]
    commit_direct(ledger, ctx(), [task(i) for i in ids])
    commit_direct(ledger, ctx(), [update(current(ledger, "tasks", "TAS_b3"), title="Moved")])
    stale = [update(current(ledger, "tasks", i), status=TASK_DONE) for i in ids[:2]]
    stale.append(api.Mutation("tasks", "TAS_b3", "upsert",
                              {**current(ledger, "tasks", "TAS_b3"), "revision": 1},
                              {**current(ledger, "tasks", "TAS_b3"), "revision": 2,
                               "status": TASK_DONE}))
    request_id = rid()
    before = counts(ledger)
    with ledger.transaction(ctx(), **OPEN_TX) as tx:
        proposal = tx.insert_proposal(direct_draft(tx, stale, request_id, window_s(ledger)))
        with pytest.raises(api.LedgerError, match="^target_changed$"):
            tx.apply(stale, source="OS_DIRECT", request_id=request_id)
        assert [tx.get("tasks", i)["status"] for i in ids] == [TASK_OPEN] * 3
        receipt = tx.finish(proposal, status="target_changed", receipt_request_id=request_id)
    after = counts(ledger)
    assert receipt.records == () and after["version"] == before["version"] + 1
    assert after["os_events"] == before["os_events"] + 1
    assert after["os_outbox"] == before["os_outbox"]
    assert [current(ledger, "tasks", i)["status"] for i in ids] == [TASK_OPEN] * 3


# --- apply revision and state rules -----------------------------------------------------------


def apply_refused(env, mutations):
    with env.transaction(ctx(), **OPEN_TX) as tx:
        with pytest.raises(api.LedgerError, match="^target_changed$"):
            tx.apply(mutations, source="OS_DIRECT", request_id=rid())


def test_apply_refuses_a_create_that_is_not_revision_one(ledger):
    m = task()
    apply_refused(ledger, [api.Mutation(m.entity, m.record_id, "upsert", None,
                                        {**m.after, "revision": 2})])
    assert ledger.rows("os_tasks") == []


def test_apply_refuses_a_create_over_an_existing_row(ledger):
    commit_direct(ledger, ctx(), [task()])
    apply_refused(ledger, [task()])
    assert len(ledger.rows("os_tasks")) == 1


def test_apply_refuses_a_stale_before_image_and_a_revision_jump(ledger):
    commit_direct(ledger, ctx(), [task()])
    first = current(ledger, "tasks", "TAS_t1")
    commit_direct(ledger, ctx(), [update(first, title="Second")])
    apply_refused(ledger, [update(first, title="Stale")])
    latest = current(ledger, "tasks", "TAS_t1")
    jump = update(latest, title="Jump")
    apply_refused(ledger, [api.Mutation(jump.entity, jump.record_id, "upsert", latest,
                                        {**jump.after, "revision": latest["revision"] + 2})])
    assert current(ledger, "tasks", "TAS_t1")["title"] == "Second"


def test_apply_refuses_update_of_a_tombstone_double_tombstone_and_bad_restore(ledger):
    commit_direct(ledger, ctx(), [task()])
    live = current(ledger, "tasks", "TAS_t1")
    apply_refused(ledger, [restore("tasks", live)])
    commit_direct(ledger, ctx(), [tombstone("tasks", live)])
    tomb = current(ledger, "tasks", "TAS_t1", include_deleted=True)
    apply_refused(ledger, [update(tomb, title="Revive by update")])
    apply_refused(ledger, [tombstone("tasks", tomb)])
    apply_refused(ledger, [restore("tasks", live)])


def test_revisions_never_rewind_across_tombstone_and_restore(ledger):
    commit_direct(ledger, ctx(), [task()])
    seen = [1]
    for build in (lambda r: tombstone("tasks", r), lambda r: restore("tasks", r),
                  lambda r: update(r, title="Again")):
        record = current(ledger, "tasks", "TAS_t1", include_deleted=True)
        commit_direct(ledger, ctx(), [build(record)])
        seen.append(row_of(ledger, "os_tasks", "TAS_t1")["revision"])
    assert seen == [1, 2, 3, 4]


def test_a_failed_apply_in_a_service_can_still_record_a_terminal_receipt(ledger):
    request_id = rid()
    stale = api.Mutation("tasks", "TAS_none", "upsert",
                         {"task_id": "TAS_none", "revision": 4, "title": "Ghost",
                          "status": TASK_OPEN},
                         {"task_id": "TAS_none", "revision": 5, "title": "Ghost",
                          "status": TASK_OPEN})
    with ledger.transaction(ctx(), **OPEN_TX) as tx:
        proposal = tx.insert_proposal(direct_draft(tx, [stale], request_id, window_s(ledger)))
        with pytest.raises(api.LedgerError):
            tx.apply([stale], source="OS_DIRECT", request_id=request_id)
        tx.finish(proposal, status="target_changed", receipt_request_id=request_id)
    assert ledger.receipt(ctx(), request_id).receipt.status == "target_changed"


def test_an_import_receipt_has_no_records_and_a_full_projection_outbox_row(ledger):
    fresh_workspace(ledger, "ws-i")
    _, receipt = commit_import(ledger, ctx(workspace="ws-i"), [task()], api.digest({"m": 1}))
    assert (receipt.kind, receipt.status, receipt.records) == ("import", "committed", ())
    assert receipt.undo_until is None
    outbox = ledger.rows("os_outbox")
    assert [o["body"] for o in outbox] == [{"authority_epoch": 1, "refs": [], "full": True}]
