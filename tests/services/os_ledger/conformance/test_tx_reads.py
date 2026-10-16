"""LedgerTx record reads: ``get``, ``get_many`` and ``find_expenses`` (§5.1).

``find_expenses`` semantics, named here because §5.1 gives only the signature (erratum E7 (c)):

* The three filters are a UNION. An expense is returned when it matches ANY given filter: its id is
  in ``ids``, OR its ``source_key`` starts with ``source_key_prefix``, OR its ``business_date`` lies
  in ``manual_window`` (both bounds inclusive).
* With no filter at all it returns ``[]`` (a union of nothing).
* A tombstoned expense is never returned. An id that does not exist is ignored.
* The order of the result is unspecified, so these cases compare sets.
"""

from datetime import date

from app.services.os_ledger import api

from tests.fakes.os_ledger_builders import (
    client,
    commit_direct,
    ctx,
    current,
    expense,
    fresh_workspace,
    task,
    tombstone,
)

OPEN = dict(domain="core", kind="owner_direct")
WINDOW = (date(2026, 10, 1), date(2026, 10, 15))


def seed(env):
    commit_direct(env, ctx(), [
        expense("EXP_a", "stmt:1", date="2026-10-01"),
        expense("EXP_b", "hand:1", date="2026-10-15"),
        expense("EXP_c", "stmt:2", date="2026-11-20"),
        expense("EXP_d", "other", date="2026-09-01"),
        expense("EXP_e", "stmt:3", date="2026-10-16"),
    ])


def found(env, **filters):
    with env.transaction(ctx(), **OPEN) as tx:
        result = tx.find_expenses(**filters)
    assert isinstance(result, list)
    return {r["expense_id"] for r in result}


def test_find_expenses_by_ids(ledger):
    seed(ledger)
    assert found(ledger, ids=["EXP_b", "EXP_d"]) == {"EXP_b", "EXP_d"}
    assert found(ledger, ids=["EXP_b", "EXP_missing"]) == {"EXP_b"}
    assert found(ledger, ids=["EXP_missing"]) == set()


def test_find_expenses_by_source_key_prefix(ledger):
    seed(ledger)
    assert found(ledger, source_key_prefix="stmt:") == {"EXP_a", "EXP_c", "EXP_e"}
    assert found(ledger, source_key_prefix="hand:") == {"EXP_b"}
    assert found(ledger, source_key_prefix="nothing:") == set()


def test_find_expenses_by_window_is_inclusive_on_both_bounds(ledger):
    seed(ledger)
    assert found(ledger, manual_window=WINDOW) == {"EXP_a", "EXP_b"}
    assert found(ledger, manual_window=(date(2026, 10, 2), date(2026, 10, 14))) == set()


def test_find_expenses_filters_are_a_union(ledger):
    seed(ledger)
    assert found(ledger, ids=["EXP_c"], manual_window=WINDOW) == {"EXP_a", "EXP_b", "EXP_c"}
    assert found(ledger, ids=["EXP_d"], source_key_prefix="hand:") == {"EXP_b", "EXP_d"}
    assert found(ledger, source_key_prefix="stmt:", manual_window=WINDOW) == {
        "EXP_a", "EXP_b", "EXP_c", "EXP_e"}
    assert found(ledger, ids=["EXP_d"], source_key_prefix="hand:", manual_window=WINDOW) == {
        "EXP_a", "EXP_b", "EXP_d"}


def test_find_expenses_with_no_filter_returns_nothing(ledger):
    seed(ledger)
    assert found(ledger) == set()
    assert found(ledger, ids=(), source_key_prefix=None, manual_window=None) == set()


def test_find_expenses_never_returns_a_tombstoned_expense(ledger):
    seed(ledger)
    commit_direct(ledger, ctx(), [tombstone("expenses", current(ledger, "expenses", "EXP_a"))])
    assert found(ledger, ids=["EXP_a"]) == set()
    assert found(ledger, source_key_prefix="stmt:") == {"EXP_c", "EXP_e"}
    assert found(ledger, manual_window=WINDOW) == {"EXP_b"}


def test_find_expenses_returns_wire_records(ledger):
    seed(ledger)
    with ledger.transaction(ctx(), **OPEN) as tx:
        [record] = tx.find_expenses(ids=["EXP_b"])
    assert record["expense_id"] == "EXP_b" and record["revision"] == 1
    assert record["source_key"] == "hand:1" and record["date"] == "2026-10-15"
    record["source_key"] = "mutated"
    with ledger.transaction(ctx(), **OPEN) as tx:
        assert tx.find_expenses(ids=["EXP_b"])[0]["source_key"] == "hand:1"


def test_find_expenses_is_scoped_to_the_workspace(ledger):
    seed(ledger)
    fresh_workspace(ledger, "ws-other", core="active")
    with ledger.transaction(ctx(workspace="ws-other"), **OPEN) as tx:
        assert tx.find_expenses(ids=["EXP_a"], source_key_prefix="stmt:",
                                manual_window=WINDOW) == []


def test_get_many_returns_only_the_refs_that_exist(ledger):
    commit_direct(ledger, ctx(), [client(), task("TAS_a"), task("TAS_b")])
    refs = [api.RecordRef("tasks", "TAS_a"), api.RecordRef("clients", "PER_c1"),
            api.RecordRef("tasks", "TAS_missing"), api.RecordRef("works", "WOR_missing")]
    with ledger.transaction(ctx(), **OPEN) as tx:
        result = tx.get_many(refs)
    assert set(result) == {refs[0], refs[1]}
    assert result[refs[0]]["task_id"] == "TAS_a" and result[refs[1]]["person_id"] == "PER_c1"
    with ledger.transaction(ctx(), **OPEN) as tx:
        assert tx.get_many([]) == {}


def test_get_and_get_many_hide_tombstones_unless_asked(ledger):
    commit_direct(ledger, ctx(), [task("TAS_a"), task("TAS_b")])
    commit_direct(ledger, ctx(), [tombstone("tasks", current(ledger, "tasks", "TAS_a"))])
    refs = [api.RecordRef("tasks", "TAS_a"), api.RecordRef("tasks", "TAS_b")]
    with ledger.transaction(ctx(), **OPEN) as tx:
        assert tx.get("tasks", "TAS_a") is None
        assert tx.get("tasks", "TAS_a", include_deleted=True)["revision"] == 2
        assert set(tx.get_many(refs)) == {refs[1]}
        assert set(tx.get_many(refs, include_deleted=True)) == set(refs)
        assert tx.get("tasks", "TAS_missing") is None


def test_returned_records_are_copies(ledger):
    commit_direct(ledger, ctx(), [task("TAS_a")])
    with ledger.transaction(ctx(), **OPEN) as tx:
        tx.get("tasks", "TAS_a")["title"] = "mutated"
        tx.get_many([api.RecordRef("tasks", "TAS_a")])[api.RecordRef("tasks", "TAS_a")][
            "title"] = "mutated"
        assert tx.get("tasks", "TAS_a")["title"] == "Task"
