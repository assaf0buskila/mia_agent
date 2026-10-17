"""V2-18 read model (§8, §20): keyset pages, CursorCodec cursors, reads never write, defaults.

The conformance suite pins the reader contract on the fake and PG; these cases pin what V2-18 adds
on PG: keyset pagination over every sort key (NULLs included), cursors minted by ``CursorCodec``
(``owner_os_reads.py:109``) and bound to {version, entity, last sort key}, reads asserted
write-free through ``pg_stat_xact_user_tables`` inside the read's own snapshot, and the
``build_ledger()`` handoff (V2-16 -> V2-18). On the fake, "never writes" is asserted through the
version, ``pending_seq`` and a deep copy of every table.
"""

from __future__ import annotations

import copy
import os
import re
from contextlib import contextmanager

import pytest
from app.services.os_ledger import api, core
from app.services.owner_os_reads import CursorCodec
from sqlalchemy import create_engine, text

from tests.fakes.os_ledger_builders import (
    TASK_CANCELLED,
    TASK_DOING,
    TASK_DONE,
    TASK_OPEN,
    TASK_WAITING,
    client,
    commit_direct,
    ctx,
    expense,
    payment,
    propose_task,
    task,
    tombstone,
    work,
)
from tests.fakes.os_ledger_env import WORKSPACE, build_fake_env
from tests.services.os_ledger.conftest import PG_URL_ENV, _prepare, build_pg_env, pg_enabled

_SAFE = re.compile(r"[A-Za-z0-9_-]{1,128}")


@pytest.fixture
def pg():
    if not pg_enabled():
        pytest.skip("disposable MIA_OS_LEDGER_TEST_POSTGRES_URL required")
    return _prepare(build_pg_env(os.environ[PG_URL_ENV]))


def pg_ledger(env):
    return env.transaction.__self__


def every_read(env, proposal_id):
    reader = env._impl
    return [
        lambda: env.head(ctx()),
        lambda: env.page(ctx(), "tasks", after=None, limit=2),
        lambda: env.page(ctx(), "works", after=None, limit=1),
        lambda: env.deleted(ctx(), "tasks", after=None, limit=5),
        lambda: env.pending(ctx(), after=None, limit=1),
        lambda: env.labels(ctx(), [api.RecordRef("tasks", "TAS_a")]),
        lambda: env.monthly_summary(ctx()),
        lambda: reader.proposal(ctx(), proposal_id),
    ]


def seed(env):
    commit_direct(env, ctx(), [client(), work(), work("WOR_w2", start_date="2026-01-02"),
                               payment(), task("TAS_a"), task("TAS_b", due_date="2026-03-01"),
                               task("TAS_c", status=TASK_DONE)])
    commit_direct(env, ctx(), [tombstone("tasks", {"task_id": "TAS_c", "revision": 1,
                                                   "title": "Task", "status": TASK_DONE})])
    return propose_task(env), propose_task(env)


# --- the build_ledger() handoff (no database needed) ------------------------------------------


def test_build_ledger_returns_the_read_model_with_the_scoped_and_incremental_defaults():
    from app.services.os_ledger.reads import ReadModelLedger
    from app.services.os_ledger.summaries import FullSummaries
    from app.services.os_ledger.summaries_incremental import IncrementalSummaries
    from app.services.os_ledger.validate_full import FullStateValidator
    from app.services.os_ledger.validate_scoped import ScopedStateValidator

    class Settings:
        database_url = "postgresql://main@127.0.0.1:1/x"
        owner_os_ledger_database_url = "postgresql://ledger@127.0.0.1:1/x"
        owner_os_cursor_secret = ""

    try:
        ledger = core.build_ledger(Settings())
        assert type(ledger) is ReadModelLedger and isinstance(ledger, core.PgLedger)
        assert type(ledger.validator) is ScopedStateValidator
        assert type(ledger.summaries) is IncrementalSummaries
        full = core.build_ledger(Settings(), validator=FullStateValidator(),
                                 summaries=FullSummaries())
        assert type(full.validator) is FullStateValidator
        assert type(full.summaries) is FullSummaries
    finally:
        core.dispose_engines()


# --- the fake: reads never change the version, pending_seq or any table -----------------------


def test_fake_reads_including_proposal_never_write():
    env = _prepare(build_fake_env())
    first, _ = seed(env)
    fake = env._impl
    before = copy.deepcopy(fake._workspaces[WORKSPACE])
    for read in every_read(env, first.proposal_id):
        read()
    after = fake._workspaces[WORKSPACE]
    assert after["version"] == before["version"] and after["pending_seq"] == before["pending_seq"]
    assert after["tables"] == before["tables"]


# --- PG: reads never write (pg_stat_xact) and run REPEATABLE READ READ ONLY ------------------


@contextmanager
def watched_reads(monkeypatch, ledger):
    """Probe every reader's own snapshot: isolation, read-only and its write counters."""
    seen = []
    original = ledger._read

    @contextmanager
    def probe(context):
        with original(context) as tx:
            yield tx
            row = tx._exec(
                "SELECT current_setting('transaction_isolation') AS iso, "
                "current_setting('transaction_read_only') AS ro, "
                "COALESCE(sum(n_tup_ins + n_tup_upd + n_tup_del), 0) AS writes, "
                "count(*) AS tables FROM pg_stat_xact_user_tables").one()
            seen.append((row.iso, row.ro, int(row.writes), int(row.tables)))

    monkeypatch.setattr(ledger, "_read", probe)
    yield seen


def test_pg_every_reader_writes_nothing_in_its_own_snapshot(pg, monkeypatch):
    first, _ = seed(pg)
    ledger = pg_ledger(pg)
    page = pg.page(ctx(), "tasks", after=None, limit=1)
    pending = pg.pending(ctx(), after=None, limit=1)
    reads = every_read(pg, first.proposal_id) + [
        lambda: pg.page(ctx(), "tasks", after=page.next_cursor, limit=1),
        lambda: pg.pending(ctx(), after=pending.next_cursor, limit=1),
    ]
    before = (pg.rows("os_workspaces"), pg.rows("os_proposals"), pg.rows("os_tasks"))
    with watched_reads(monkeypatch, ledger) as seen:
        for read in reads:
            read()
    assert len(seen) == len(reads)
    assert all(s[:3] == ("repeatable read", "on", 0) for s in seen), seen
    assert (pg.rows("os_workspaces"), pg.rows("os_proposals"), pg.rows("os_tasks")) == before


def test_pg_the_write_probe_sees_writes_in_its_transaction(pg):
    """The control: the same counters move inside a transaction that does write."""
    seed(pg)
    with create_engine(os.environ[PG_URL_ENV]).connect() as conn:
        conn.execute(text("UPDATE assaf_os.os_workspaces SET updated_at = updated_at "
                          "WHERE workspace_id = :ws"), {"ws": WORKSPACE})
        writes = conn.execute(text(
            "SELECT COALESCE(sum(n_tup_ins + n_tup_upd + n_tup_del), 0) "
            "FROM pg_stat_xact_user_tables")).scalar()
        conn.rollback()
    assert int(writes) >= 1


# --- PG: keyset pages over every sort key --------------------------------------------------


def walk(env, entity, limit, *, deleted=False):
    reader = env.deleted if deleted else env.page
    seen, cursor, totals = [], None, set()
    while True:
        page = reader(ctx(), entity, after=cursor, limit=limit)
        seen += [r[api.ID_KEYS[entity]] for r in page.records]
        totals.add(page.total)
        cursor = page.next_cursor
        if cursor is None:
            return seen, totals
        assert _SAFE.fullmatch(cursor), cursor  # a CursorCodec token


def test_pg_task_pages_walk_the_status_due_date_id_order_with_any_limit(pg):
    statuses = (None, TASK_DOING, TASK_WAITING, TASK_DONE, TASK_CANCELLED)
    dues = (None, "2026-01-01", "2026-02-01", None, "2026-01-01")
    tasks = [task(f"TAS_{i:02d}", **({"status": statuses[i % 5]} if statuses[i % 5] else {}),
                  **({"due_date": dues[(i * 3) % 5]} if dues[(i * 3) % 5] else {}))
             for i in range(23)]
    commit_direct(pg, ctx(), tasks)
    expected, _ = walk(pg, "tasks", 200)
    assert sorted(expected) == sorted(m.record_id for m in tasks)
    for limit in (1, 2, 3, 7):
        assert walk(pg, "tasks", limit) == (expected, {23})


def test_pg_business_date_pages_walk_descending_nulls_last_then_id(pg):
    dates = ("2026-01-01", None, "2026-03-01", "2026-01-01", None, "2026-02-01")
    works = [work(f"WOR_{i:02d}", **({"start_date": dates[i % 6]} if dates[i % 6] else {}))
             for i in range(17)]
    commit_direct(pg, ctx(), [client(), *works])
    expected, _ = walk(pg, "works", 200)
    nulls = [m.record_id for m in works if "start_date" not in m.after]
    dated = sorted((m for m in works if "start_date" in m.after),
                   key=lambda m: (-int(m.after["start_date"].replace("-", "")), m.record_id))
    assert expected == [m.record_id for m in dated] + sorted(nulls)
    for limit in (1, 4, 5):
        assert walk(pg, "works", limit) == (expected, {17})


def test_pg_deleted_pages_walk_newest_first_and_total_counts_live_rows(pg):
    commit_direct(pg, ctx(), [task(f"TAS_d{i}") for i in range(6)] + [task("TAS_live")])
    for i in (3, 0, 5, 1, 4, 2):
        pg.advance(1 if i % 2 else 0)  # ties in deleted_at fall back to the id
        commit_direct(pg, ctx(), [tombstone("tasks", {"task_id": f"TAS_d{i}", "revision": 1,
                                                      "title": "Task", "status": TASK_OPEN})])
    expected, totals = walk(pg, "tasks", 200, deleted=True)
    assert sorted(expected) == [f"TAS_d{i}" for i in range(6)] and totals == {1}
    for limit in (1, 2, 4):
        assert walk(pg, "tasks", limit, deleted=True) == (expected, {1})


def test_pg_pending_walks_creation_order_and_a_propose_between_pages_appends(pg):
    made = [propose_task(pg).proposal_id for _ in range(5)]  # one frozen clock: created_at ties
    first = pg.pending(ctx(), after=None, limit=2)
    late = propose_task(pg).proposal_id  # pending_seq moves, the version does not
    seen = [p.proposal_id for p in first.items]
    cursor = first.next_cursor
    while cursor is not None:
        page = pg.pending(ctx(), after=cursor, limit=2)
        seen += [p.proposal_id for p in page.items]
        cursor = page.next_cursor
    assert sorted(seen) == sorted([*made, late]) and len(seen) == len(set(seen))


# --- PG: the cursor (CursorCodec; {v, e, k}) -------------------------------------------------


def tokens(env):
    commit_direct(env, ctx(), [task(f"TAS_k{i}") for i in range(4)] + [client(), expense()])
    return env.page(ctx(), "tasks", after=None, limit=1).next_cursor


@pytest.mark.parametrize("mutate", [
    lambda t: t[:-1] + ("A" if t[-1] != "A" else "B"),
    lambda t: t[:20] + ("A" if t[20] != "A" else "B") + t[21:],
    lambda t: t[:-4],
    lambda t: t + "AAAA",
    lambda t: "c1." + t,
    lambda t: 12,
])
def test_pg_a_tampered_cursor_is_invalid_cursor(pg, mutate):
    cursor = tokens(pg)
    with pytest.raises(api.LedgerError, match="^invalid_cursor$"):
        pg.page(ctx(), "tasks", after=mutate(cursor), limit=1)


def test_pg_a_cursor_is_a_codec_token_signed_with_the_configured_secret(pg):
    cursor = tokens(pg)
    ledger = pg_ledger(pg)
    assert len(cursor) <= 128 and _SAFE.fullmatch(cursor)
    assert ledger.page(ctx(), "tasks", after=cursor, limit=1).records
    other = CursorCodec("o" * 32)  # another secret cannot mint a cursor this ledger accepts
    forged = other.encode(owner_id="123", workspace_id=WORKSPACE, context="x", row_id="TAS_k1",
                          checksum=pg.version())
    with pytest.raises(api.LedgerError, match="^invalid_cursor$"):
        pg.page(ctx(), "tasks", after=forged, limit=1)


def test_pg_a_cursor_is_bound_to_its_entity_and_listing(pg):
    cursor = tokens(pg)
    for read in (lambda: pg.page(ctx(), "clients", after=cursor, limit=1),
                 lambda: pg.deleted(ctx(), "tasks", after=cursor, limit=1),
                 lambda: pg.pending(ctx(), after=cursor, limit=1)):
        with pytest.raises(api.LedgerError, match="^invalid_cursor$"):
            read()


def test_pg_a_version_change_or_an_expired_cursor_is_cursor_invalidated(pg):
    cursor = tokens(pg)
    pg.advance(301)  # CursorCodec's TTL is 300 s, on the ledger's clock
    with pytest.raises(api.LedgerError) as caught:
        pg.page(ctx(), "tasks", after=cursor, limit=1)
    assert caught.value.code == "cursor_invalidated"
    cursor = pg.page(ctx(), "tasks", after=None, limit=1).next_cursor
    commit_direct(pg, ctx(), [task("TAS_new")])
    with pytest.raises(api.LedgerError) as caught:
        pg.page(ctx(), "tasks", after=cursor, limit=1)
    assert caught.value.code == "cursor_invalidated"
    assert caught.value.details["observed_version"] == pg.version()
