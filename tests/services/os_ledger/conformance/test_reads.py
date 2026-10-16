"""Readers (§8, §10.2): admission, orders and limits, cursors, labels, pending, summaries."""

from datetime import timedelta

import pytest
from app.services.os_ledger import api

from tests.fakes.contexts import for_worker
from tests.fakes.os_ledger_builders import (
    TASK_CANCELLED,
    TASK_DOING,
    TASK_DONE,
    TASK_OPEN,
    TASK_WAITING,
    abandon_draft,
    client,
    commit_direct,
    counts,
    ctx,
    expense,
    fresh_workspace,
    payment,
    propose_task,
    rid,
    task,
    tombstone,
    window_s,
    work,
)
from tests.fakes.os_ledger_env import WORKSPACE

STATES = ("shadow", "active", "fenced", "restoring")


def every_read(env, context):
    return [
        lambda: env.head(context),
        lambda: env.page(context, "tasks", after=None, limit=10),
        lambda: env.deleted(context, "tasks", after=None, limit=10),
        lambda: env.pending(context, after=None, limit=10),
        lambda: env.labels(context, []),
        lambda: env.monthly_summary(context),
    ]


@pytest.mark.parametrize("channel", ("OS", "WORKER"))
@pytest.mark.parametrize("core", STATES)
def test_reads_need_active_or_fenced_and_shadow_only_for_the_worker(ledger, core, channel):
    fresh_workspace(ledger, "ws", core=core)
    context = ctx(channel, workspace="ws") if channel == "OS" else for_worker(
        "verifier", workspace="ws")
    allowed = core in {"active", "fenced"} or (channel == "WORKER" and core == "shadow")
    for read in every_read(ledger, context):
        if allowed:
            read()
        else:
            with pytest.raises(api.LedgerError, match="^authority_not_active$"):
                read()


def test_reads_check_the_epoch_and_the_workspace(ledger):
    ledger.knobs.settings_epoch = 2
    for read in every_read(ledger, ctx(epoch=1)):
        with pytest.raises(api.LedgerError, match="^authority_changed$"):
            read()
    for read in every_read(ledger, ctx(workspace="nowhere")):
        with pytest.raises(api.LedgerError, match="^ledger_unavailable$"):
            read()


def test_head_reports_the_snapshot(ledger):
    head = ledger.head(ctx())
    assert head.observed_version == 0 and head.authority_epoch == 1 and head.pending_seq == 0
    assert dict(head.states) == {"core": "active", "statements": "shadow"}
    assert head.mirror_verified_version == 0
    commit_direct(ledger, ctx(), [task()])
    head = ledger.head(ctx())
    assert head.observed_version == 1 and head.pending_seq == 2  # insert_proposal + finish


def test_reads_never_change_the_version_the_since_token_or_any_row(ledger):
    commit_direct(ledger, ctx(), [client(), task(), task("TAS_t2")])
    propose_task(ledger)
    before = (counts(ledger), api.since_token(ledger.head(ctx())))
    for read in every_read(ledger, ctx()):
        read()
    page = ledger.page(ctx(), "tasks", after=None, limit=1)
    ledger.page(ctx(), "tasks", after=page.next_cursor, limit=1)
    ledger.receipt(ctx(), rid())
    ledger.creation_request(ctx(), rid())
    assert (counts(ledger), api.since_token(ledger.head(ctx()))) == before


# --- pages: order, totals, limits ------------------------------------------------------------


def task_ids(page):
    return [r["task_id"] for r in page.records]


def test_task_pages_list_open_statuses_first_then_due_date_nulls_last_then_id(ledger):
    commit_direct(ledger, ctx(), [
        task("TAS_a", status=TASK_DONE, due_date="2026-01-01"),
        task("TAS_b", due_date="2026-03-01"),
        task("TAS_c"),
        task("TAS_d", status=TASK_CANCELLED, due_date="2026-01-01"),
        task("TAS_e", status=TASK_DOING, due_date="2026-02-01"),
        task("TAS_f", due_date="2026-02-01"),
        task("TAS_g", status=TASK_WAITING)])
    page = ledger.page(ctx(), "tasks", after=None, limit=50)
    assert task_ids(page) == ["TAS_e", "TAS_f", "TAS_b", "TAS_c", "TAS_g", "TAS_a", "TAS_d"]
    assert page.total == 7 and page.next_cursor is None and page.entity == "tasks"
    assert page.observed_version == 1


def test_other_entities_list_business_date_descending_nulls_last_then_id(ledger):
    commit_direct(ledger, ctx(), [
        client(), work("WOR_a", start_date="2026-01-01"), work("WOR_b", start_date="2026-02-01"),
        work("WOR_c"), work("WOR_d", start_date="2026-02-01"), work("WOR_e")])
    page = ledger.page(ctx(), "works", after=None, limit=50)
    assert [r["work_id"] for r in page.records] == ["WOR_b", "WOR_d", "WOR_a", "WOR_c", "WOR_e"]
    assert all(r["revision"] == 1 for r in page.records)


def test_a_page_record_is_the_wire_record(ledger):
    commit_direct(ledger, ctx(), [task(due_date="2026-02-01")])
    [record] = ledger.page(ctx(), "tasks", after=None, limit=5).records
    assert record == {"task_id": "TAS_t1", "revision": 1, "title": "Task", "status": TASK_OPEN,
                      "due_date": "2026-02-01"}


@pytest.mark.parametrize("limit", (0, -1, 201, None, 1.5))
def test_a_limit_outside_one_to_two_hundred_is_invalid_query(ledger, limit):
    for read in (lambda: ledger.page(ctx(), "tasks", after=None, limit=limit),
                 lambda: ledger.deleted(ctx(), "tasks", after=None, limit=limit),
                 lambda: ledger.pending(ctx(), after=None, limit=limit)):
        with pytest.raises(api.LedgerError, match="^invalid_query$"):
            read()


@pytest.mark.parametrize("limit", (1, 200))
def test_the_limit_bounds_are_inclusive(ledger, limit):
    assert ledger.page(ctx(), "tasks", after=None, limit=limit).records == ()


def test_an_unknown_entity_is_invalid_query(ledger):
    with pytest.raises(api.LedgerError, match="^invalid_query$"):
        ledger.page(ctx(), "statements", after=None, limit=10)


def test_pagination_walks_every_record_once_in_order(ledger):
    commit_direct(ledger, ctx(), [task(f"TAS_p{i}", due_date=f"2026-01-0{i}") for i in range(1, 6)])
    seen, cursor, pages = [], None, 0
    while True:
        page = ledger.page(ctx(), "tasks", after=cursor, limit=2)
        seen += task_ids(page)
        pages += 1
        assert page.total == 5
        cursor = page.next_cursor
        if cursor is None:
            break
        assert isinstance(cursor, str) and cursor
    assert seen == [f"TAS_p{i}" for i in range(1, 6)] and pages == 3


def test_deleted_lists_tombstones_newest_first_and_total_counts_live_rows_only(ledger):
    commit_direct(ledger, ctx(), [task("TAS_x1"), task("TAS_x2"), task("TAS_x3"), task("TAS_y")])
    for record_id in ("TAS_x1", "TAS_x3", "TAS_x2"):
        ledger.advance(5)
        commit_direct(ledger, ctx(), [tombstone("tasks", next(
            r for r in ledger.page(ctx(), "tasks", after=None, limit=50).records
            if r["task_id"] == record_id))])
    deleted = ledger.deleted(ctx(), "tasks", after=None, limit=10)
    assert task_ids(deleted) == ["TAS_x2", "TAS_x3", "TAS_x1"]
    page = ledger.page(ctx(), "tasks", after=None, limit=10)
    assert task_ids(page) == ["TAS_y"] and page.total == 1


# --- cursors ----------------------------------------------------------------------------------


def two_pages(env):
    commit_direct(env, ctx(), [task(f"TAS_c{i}") for i in range(3)])
    return env.page(ctx(), "tasks", after=None, limit=1)


def test_a_cursor_is_invalidated_when_the_version_moves(ledger):
    page = two_pages(ledger)
    commit_direct(ledger, ctx(), [task("TAS_new")])
    with pytest.raises(api.LedgerError) as caught:
        ledger.page(ctx(), "tasks", after=page.next_cursor, limit=1)
    assert caught.value.code == "cursor_invalidated"
    assert caught.value.details["observed_version"] == ledger.version()


def test_a_cursor_survives_a_propose_because_only_finish_moves_the_version(ledger):
    page = two_pages(ledger)
    propose_task(ledger)
    assert ledger.page(ctx(), "tasks", after=page.next_cursor, limit=1).records


@pytest.mark.parametrize("cursor", ("", "garbage", "fake:", "fake:AAAA", "x" * 600))
def test_a_forged_or_malformed_cursor_is_invalid_cursor(ledger, cursor):
    two_pages(ledger)
    for read in (lambda: ledger.page(ctx(), "tasks", after=cursor, limit=1),
                 lambda: ledger.deleted(ctx(), "tasks", after=cursor, limit=1),
                 lambda: ledger.pending(ctx(), after=cursor, limit=1)):
        with pytest.raises(api.LedgerError, match="^invalid_cursor$"):
            read()


def test_a_cursor_is_bound_to_its_entity_and_listing(ledger):
    commit_direct(ledger, ctx(), [client(), task("TAS_a"), task("TAS_b")])
    page = ledger.page(ctx(), "tasks", after=None, limit=1)
    with pytest.raises(api.LedgerError, match="^invalid_cursor$"):
        ledger.page(ctx(), "clients", after=page.next_cursor, limit=1)
    with pytest.raises(api.LedgerError, match="^invalid_cursor$"):
        ledger.deleted(ctx(), "tasks", after=page.next_cursor, limit=1)


def test_a_cursor_is_not_usable_from_another_workspace(ledger):
    page = two_pages(ledger)
    fresh_workspace(ledger, "ws-other", core="active")
    with pytest.raises(api.LedgerError) as caught:
        ledger.page(ctx(workspace="ws-other"), "tasks", after=page.next_cursor, limit=1)
    assert caught.value.code in {"invalid_cursor", "cursor_invalidated"}


def test_a_cursor_of_an_earlier_epoch_is_refused_after_a_bump(ledger):
    page = two_pages(ledger)
    ledger.bump_epoch(WORKSPACE, expected_epoch=1)
    ledger.knobs.settings_epoch = 2
    with pytest.raises(api.LedgerError) as caught:
        ledger.page(ctx(epoch=2), "tasks", after=page.next_cursor, limit=1)
    assert caught.value.code in {"invalid_cursor", "cursor_invalidated"}


# --- labels ----------------------------------------------------------------------------------


def test_labels_return_only_the_requested_refs(ledger):
    commit_direct(ledger, ctx(), [client("PER_c1", name="First"), client("PER_c2", name="Second"),
                                  work(title="A work")])
    result = ledger.labels(ctx(), [api.RecordRef("clients", "PER_c1"),
                                   api.RecordRef("works", "WOR_w1"),
                                   api.RecordRef("clients", "PER_missing")])
    assert result == {"clients.person_id": {"PER_c1": "First"},
                      "works.work_id": {"WOR_w1": "A work"}}


def test_a_label_is_the_first_of_name_title_category_cut_to_120(ledger):
    commit_direct(ledger, ctx(), [
        client("PER_c1", name="N" * 200), task("TAS_t1", title="T" * 130),
        work(title="Work title"), expense(category="C" * 125),
        payment(), ])
    result = ledger.labels(ctx(), [
        api.RecordRef("clients", "PER_c1"), api.RecordRef("tasks", "TAS_t1"),
        api.RecordRef("expenses", "EXP_e1"), api.RecordRef("payments", "PAY_p1")])
    assert result["clients.person_id"] == {"PER_c1": "N" * 120}
    assert result["tasks.task_id"] == {"TAS_t1": "T" * 120}
    assert result["expenses.expense_id"] == {"EXP_e1": "C" * 120}
    assert result.get("payments.payment_id", {}) == {}  # no name, title or category: omitted


def test_labels_for_no_refs_is_empty(ledger):
    assert ledger.labels(ctx(), []) == {}


# --- pending ----------------------------------------------------------------------------------


def test_pending_lists_only_unexpired_pending_proposals_of_kind_proposal(ledger):
    stale = propose_task(ledger)
    ledger.advance(601)  # past the ten minutes
    live = propose_task(ledger)
    decided = propose_task(ledger)
    ledger.decide(ctx(), decided.proposal_id, decided.rejection_id, "reject")
    with ledger.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        tx.insert_proposal(api.ProposalDraft(
            kind="owner_direct", source="OS_DIRECT", request_id=rid(),
            request_digest=api.digest({"x": 1}), entity="abandon", record_id="x",
            expected_revision=0, mutations=(), dependency_digest=api.digest([]),
            expires_at=tx.now + timedelta(seconds=window_s(ledger))))
    page = ledger.pending(ctx(), after=None, limit=50)
    assert [p.proposal_id for p in page.items] == [live.proposal_id]
    assert page.items[0] == live and page.next_cursor is None
    assert page.observed_version == ledger.version()
    still = next(r for r in ledger.rows("os_proposals") if r["proposal_id"] == stale.proposal_id)
    assert still["status"] == "pending"  # reads never write: it waits for a decide or expiry


def test_pending_paginates_and_invalidates_its_cursor_with_the_version(ledger):
    made = {propose_task(ledger).proposal_id for _ in range(3)}
    first = ledger.pending(ctx(), after=None, limit=2)
    second = ledger.pending(ctx(), after=first.next_cursor, limit=2)
    assert {p.proposal_id for p in first.items + second.items} == made
    assert second.next_cursor is None
    commit_direct(ledger, ctx(), [task()])
    with pytest.raises(api.LedgerError, match="^cursor_invalidated$"):
        ledger.pending(ctx(), after=first.next_cursor, limit=2)


# --- summaries and receipt reads ---------------------------------------------------------------


def test_monthly_summary_is_the_v1_monthly_row_shape(ledger):
    assert tuple(ledger.monthly_summary(ctx())) == ()
    commit_direct(ledger, ctx(), [client(), work(), payment()])
    [row] = ledger.monthly_summary(ctx())
    assert set(row) == {"month", "currency", "paid_income_ils", "paid_expenses_ils",
                        "net_cash_ils", "recorded_minutes", "effective_hourly_ils", "coverage",
                        "reason_codes"}
    assert (row["month"], row["currency"], row["paid_income_ils"]) == ("2026-10", "ILS", 1000)


def test_receipt_and_creation_request_report_unknown_ids_as_not_found(ledger):
    missing = rid()
    result = ledger.receipt(ctx(), missing)
    assert not result.found and result.receipt is None and result.proposal is None
    assert result.records == () and result.undo is None
    created = ledger.creation_request(ctx(), missing)
    assert not created.found and created.proposal is None and created.decision is None
    assert created.past_expiry is None


@pytest.mark.parametrize("bad", ("", "not-a-uuid", "A" * 36,
                                 "12345678-1234-1234-1234-123456789ABC"))
def test_receipt_and_creation_request_refuse_a_non_canonical_uuid(ledger, bad):
    with pytest.raises(api.LedgerError, match="^invalid_request_id$"):
        ledger.receipt(ctx(), bad)
    with pytest.raises(api.LedgerError, match="^invalid_request_id$"):
        ledger.creation_request(ctx(), bad)


def test_creation_request_finds_a_pending_proposal_and_flags_the_expiry(ledger):
    proposal = propose_task(ledger)
    found = ledger.creation_request(ctx(), proposal.request_id)
    assert found.found and found.proposal == proposal and found.decision is None
    assert found.past_expiry is False
    ledger.advance(601)
    assert ledger.creation_request(ctx(), proposal.request_id).past_expiry is True


def test_creation_request_returns_the_decision_receipt(ledger):
    proposal = propose_task(ledger)
    decided = ledger.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
    found = ledger.creation_request(ctx(), proposal.request_id)
    assert found.proposal.status == "committed" and found.decision == decided.receipt
    assert found.observed_version == 1


def test_an_unused_abandon_draft_is_not_a_pending_proposal(ledger):
    request_id = rid()
    with ledger.transaction(ctx(), domain="core", kind="owner_direct") as tx:
        tx.insert_proposal(abandon_draft(tx, request_id, window_s(ledger)))
    assert ledger.pending(ctx(), after=None, limit=10).items == ()
