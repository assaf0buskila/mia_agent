"""V2-29 outbox lease and ``os_mirror_state`` rules, on the fake and (when a disposable URL is
set) on PostgreSQL 16 with the V2-14 table DDL taken verbatim from the merged migration.

"pg" cases are generated only when ``MIA_OS_LEDGER_TEST_POSTGRES_URL`` is set; none skip.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from app.services.os_ledger import outbox
from app.services.os_ledger.outbox import (
    LEASE_TTL,
    OutboxError,
    PgOutbox,
    backoff,
    destination_binding,
    retry_delay,
)

from tests.workers.mirror_fakes import T0, Clock, FakeOutbox, TxCounter, outbox_body

ROOT = Path(__file__).resolve().parents[2]
PG_URL = os.getenv("MIA_OS_LEDGER_TEST_POSTGRES_URL", "")
WS = "ws-1"
BINDING = destination_binding("mirror-sheet-1")
BACKENDS = ["fake"] + (["pg"] if PG_URL else [])


def _table_ddl(schema: str) -> list[str]:
    """The two V2-14 tables, verbatim except the schema name and the cross-table FKs."""
    sql = (ROOT / "migrations_assaf_os/20261015_assaf_os_ledger.sql").read_text(encoding="utf-8")
    statements = []
    for table in ("os_outbox", "os_mirror_state"):
        match = re.search(
            rf"CREATE TABLE assaf_os\.{table} \(.*?\n\);", sql, flags=re.DOTALL
        )
        assert match, table
        ddl = re.sub(
            r"FOREIGN KEY \([^)]*\)\s+REFERENCES [\w.]+\([^)]*\),\s*", "", match.group(0)
        )
        assert "FOREIGN KEY" not in ddl and "CHECK" in ddl
        statements.append(ddl.replace("assaf_os.", f"{schema}."))
    return statements


class PgHarness:
    def __init__(self) -> None:
        from sqlalchemy import create_engine, event, text
        from sqlalchemy.engine import make_url

        url = make_url(PG_URL)
        assert url.host in {"127.0.0.1", "localhost", "::1"}
        assert url.database in {"mia_ledger_test", "owner_os_test", "assaf_os_local_codex"}
        self.text = text
        self.schema = f"v2_29_mirror_{uuid4().hex[:12]}"
        self.engine = create_engine(PG_URL, pool_size=2, max_overflow=0)
        self.open_transactions = 0
        event.listen(self.engine, "begin", self._began)
        event.listen(self.engine, "commit", self._ended)
        event.listen(self.engine, "rollback", self._ended)
        with self.engine.begin() as conn:
            assert 160000 <= int(conn.scalar(text("SHOW server_version_num"))) < 170000
            conn.execute(text(f"CREATE SCHEMA {self.schema}"))
            for ddl in _table_ddl(self.schema):
                conn.execute(text(ddl))

    def _began(self, _conn: Any) -> None:
        self.open_transactions += 1

    def _ended(self, _conn: Any) -> None:
        self.open_transactions -= 1

    def outbox(self) -> PgOutbox:
        return PgOutbox(self.engine, WS, schema=self.schema)

    def add(self, commit_version: int, body: dict[str, Any], at: datetime) -> str:
        event_id = str(uuid4())
        with self.engine.begin() as conn:
            conn.execute(
                self.text(
                    f"INSERT INTO {self.schema}.os_outbox (workspace_id, event_id, "
                    "commit_version, body, next_attempt_at, created_at, updated_at) "
                    "VALUES (:ws, :id, :v, CAST(:body AS jsonb), :at, "
                    "LEAST(CAST(:at AS timestamptz), now()), "
                    "LEAST(CAST(:at AS timestamptz), now()))"
                ),
                {"ws": WS, "id": event_id, "v": commit_version, "body": json.dumps(body),
                 "at": at},
            )
        return event_id

    def row(self, event_id: str) -> dict[str, Any]:
        with self.engine.begin() as conn:
            row = conn.execute(
                self.text(f"SELECT * FROM {self.schema}.os_outbox WHERE event_id = :id"),
                {"id": event_id},
            ).mappings().one()
        return dict(row)

    def now(self) -> datetime:
        with self.engine.begin() as conn:
            return conn.scalar(self.text("SELECT now()"))

    def make_due(self, event_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                self.text(f"UPDATE {self.schema}.os_outbox SET next_attempt_at=now() "
                          "WHERE event_id=:id"), {"id": event_id},
            )

    def expire_lease(self, event_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                self.text(f"UPDATE {self.schema}.os_outbox "
                          "SET leased_at=now() - CAST(:ttl AS interval) - interval '1 second' "
                          "WHERE event_id=:id"), {"id": event_id, "ttl": LEASE_TTL},
            )

    def close(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(self.text(f"DROP SCHEMA {self.schema} CASCADE"))
        self.engine.dispose()


class FakeHarness:
    def __init__(self) -> None:
        self.clock = Clock()
        self.store = FakeOutbox(TxCounter(), self.clock, WS)

    def outbox(self) -> FakeOutbox:
        return self.store

    def add(self, commit_version: int, body: dict[str, Any], at: datetime) -> str:
        return self.store.add(commit_version, body, at=at)

    def row(self, event_id: str) -> dict[str, Any]:
        return dict(self.store.row(event_id))

    def close(self) -> None:
        return None


@pytest.fixture(params=BACKENDS)
def harness(request) -> Iterator[Any]:
    built = PgHarness() if request.param == "pg" else FakeHarness()
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def pg() -> Iterator[PgHarness]:
    assert PG_URL, "PG cases must only be registered with a disposable URL"
    built = PgHarness()
    try:
        yield built
    finally:
        built.close()


def _pg_due_row_is_claimable_with_application_clock_behind(pg) -> None:
    before = pg.now()
    event_id = pg.add(1, outbox_body(), before)
    (event,) = pg.outbox().claim(1, "behind", before - timedelta(hours=1))
    row = pg.row(event_id)
    assert event.event_id == event_id
    assert (event.lease_generation, event.attempts) == (1, 1)
    assert before <= row["leased_at"] == row["updated_at"] <= pg.now()
    assert row["updated_at"] >= row["created_at"]


def _pg_application_clock_ahead_does_not_bypass_backoff_or_stamp_future(pg) -> None:
    before = pg.now()
    due = pg.add(1, outbox_body(), before)
    future = pg.add(2, outbox_body(), before)
    with pg.engine.begin() as conn:
        conn.execute(pg.text(f"UPDATE {pg.schema}.os_outbox "
                             "SET next_attempt_at=now() + interval '30 minutes' "
                             "WHERE event_id=:id"), {"id": future})
    store = pg.outbox()
    (event,) = store.claim(10, "ahead", before + timedelta(hours=1))
    assert event.event_id == due
    assert store.claim(10, "ahead", before + timedelta(hours=1)) == []
    row = pg.row(due)
    assert before <= row["leased_at"] == row["updated_at"] <= pg.now()
    assert pg.row(future)["attempts"] == 0


def _pg_lease_takeover_uses_database_time_and_fences_stale_generation(pg) -> None:
    before = pg.now()
    event_id = pg.add(1, outbox_body(), before)
    store = pg.outbox()
    (first,) = store.claim(1, "first", before)
    assert store.claim(1, "ahead", before + timedelta(hours=1)) == []
    assert pg.row(event_id)["lease_generation"] == 1
    pg.expire_lease(event_id)
    (second,) = store.claim(1, "behind", before - timedelta(hours=1))
    assert (second.lease_generation, second.attempts) == (2, 2)
    assert not store.finish(first, first.lease_generation, "sent", None, before)
    assert store.finish(second, second.lease_generation, "sent", None, before)
    assert not store.finish(second, second.lease_generation, "unknown", "late", before)
    row = pg.row(event_id)
    assert row["status"] == "sent"
    assert row["leased_at"] is None and row["lease_owner"] is None
    assert row["created_at"] <= row["updated_at"] <= pg.now()


def _pg_lease_is_not_reclaimed_at_exact_database_ttl_boundary(pg) -> None:
    before = pg.now()
    event_id = pg.add(1, outbox_body(), before)
    (first,) = pg.outbox().claim(1, "first", before)
    # Reuse one transaction only in this test to hold PostgreSQL now() exactly still.
    # This makes both sides of the strict TTL boundary testable without sleeps.
    with pg.engine.begin() as conn:
        scoped_engine = SimpleNamespace(begin=lambda: nullcontext(conn))
        store = PgOutbox(scoped_engine, WS, schema=pg.schema)
        conn.execute(pg.text(f"UPDATE {pg.schema}.os_outbox "
                             "SET leased_at=now() - CAST(:ttl AS interval) WHERE event_id=:id"),
                     {"id": event_id, "ttl": LEASE_TTL})
        assert store.claim(1, "ahead", before + timedelta(hours=1)) == []
        conn.execute(pg.text(f"UPDATE {pg.schema}.os_outbox "
                             "SET leased_at=leased_at - interval '1 microsecond' "
                             "WHERE event_id=:id"), {"id": event_id})
        (second,) = store.claim(1, "behind", before - timedelta(hours=1))
        assert second.event_id == first.event_id
        assert (second.lease_generation, second.attempts) == (2, 2)


@pytest.mark.parametrize("status", ["pending", "unknown"])
def _pg_claim_includes_exact_database_retry_boundary(pg, status) -> None:
    before = pg.now()
    event_id = pg.add(1, outbox_body(), before)
    # Hold now() still for the deadline updates and both claims, as in the TTL test.
    with pg.engine.begin() as conn:
        scoped_engine = SimpleNamespace(begin=lambda: nullcontext(conn))
        store = PgOutbox(scoped_engine, WS, schema=pg.schema)
        conn.execute(pg.text(f"UPDATE {pg.schema}.os_outbox "
                             "SET status=:status, next_attempt_at=now() WHERE event_id=:id"),
                     {"id": event_id, "status": status})
        (event,) = store.claim(1, "exact", before)
        assert event.event_id == event_id
        conn.execute(pg.text(f"UPDATE {pg.schema}.os_outbox "
                             "SET status=:status, lease_owner=NULL, leased_at=NULL, "
                             "next_attempt_at=now() + interval '1 microsecond' "
                             "WHERE event_id=:id"), {"id": event_id, "status": status})
        assert store.claim(1, "future", before) == []


@pytest.mark.parametrize("status", ["pending", "unknown"])
@pytest.mark.parametrize("attempts", [1, 2, 6, 7, 20])
def test_retry_delay_retryable_outcomes_use_backoff(status, attempts) -> None:
    assert retry_delay(status, attempts) == backoff(attempts)


@pytest.mark.parametrize("status", ["sent", "conflict"])
@pytest.mark.parametrize("attempts", [1, 2, 6, 7, 20])
def test_retry_delay_terminal_outcomes_have_no_delay(status, attempts) -> None:
    assert retry_delay(status, attempts) == timedelta(0)


@pytest.mark.parametrize("status", ["pending", "unknown", "sent", "conflict"])
@pytest.mark.parametrize("attempts", [1, 7])
def test_publisher_and_pg_finish_use_the_shared_retry_delay(monkeypatch, status, attempts):
    from app.workers import os_mirror

    calls = []
    delay = timedelta(seconds=123)

    def policy(result_status, result_attempts):
        calls.append((result_status, result_attempts))
        return delay

    monkeypatch.setattr(outbox, "retry_delay", policy)
    fake = FakeOutbox(TxCounter(), Clock(), WS)
    event_id = fake.add(1, outbox_body())
    fake.row(event_id)["attempts"] = attempts - 1
    (event,) = fake.claim(1, "worker", T0)
    os_mirror._finish(SimpleNamespace(outbox=fake), event, status, None, T0)
    assert fake.row(event_id)["next_attempt_at"] == T0 + delay

    parameters = []

    def execute(_statement, params):
        parameters.append(params)
        return SimpleNamespace(rowcount=1)

    conn = SimpleNamespace(execute=execute)
    store = PgOutbox(SimpleNamespace(begin=lambda: nullcontext(conn)), WS)
    assert store.finish(event, event.lease_generation, status, None, T0)
    assert parameters[0]["retry"] == delay
    assert calls == [(status, attempts), (status, attempts)]


@pytest.mark.parametrize("status", ["pending", "unknown", "sent", "conflict"])
@pytest.mark.parametrize("attempts", [1, 2, 7, 20])
@pytest.mark.parametrize("skew_hours", [-1, 1])
def _pg_finish_uses_database_clock_for_backoff_and_metadata(pg, status, attempts,
                                                        skew_hours) -> None:
    before = pg.now()
    event_id = pg.add(1, outbox_body(), before)
    with pg.engine.begin() as conn:
        conn.execute(pg.text(f"UPDATE {pg.schema}.os_outbox SET attempts=:attempts "
                             "WHERE event_id=:id"), {"id": event_id, "attempts": attempts - 1})
    store = pg.outbox()
    (event,) = store.claim(1, "worker", before)
    # Simulate metadata left in the future by the old, skewed application clock.
    with pg.engine.begin() as conn:
        conn.execute(pg.text(f"UPDATE {pg.schema}.os_outbox "
                             "SET updated_at=now() + interval '1 hour' WHERE event_id=:id"),
                     {"id": event_id})
    delay = retry_delay(status, attempts)
    app_retry = before + timedelta(hours=skew_hours) + delay
    assert store.finish(event, event.lease_generation, status, "mirror_timeout", app_retry)
    row = pg.row(event_id)
    assert row["status"] == status and row["last_error_code"] == "mirror_timeout"
    assert row["leased_at"] is None and row["lease_owner"] is None
    assert before <= row["updated_at"] <= pg.now()
    assert row["updated_at"] >= row["created_at"]
    assert row["next_attempt_at"] == row["updated_at"] + delay
    assert store.claim(1, "ahead", before + timedelta(hours=1)) == []


@pytest.mark.parametrize("skew_hours", [-1, 1])
def _pg_mirror_state_stamps_use_database_clock(pg, skew_hours) -> None:
    store = pg.outbox()
    before = pg.now()
    app_now = before + timedelta(hours=skew_hours)
    store.load_state(BINDING, app_now)
    store.record_attempt(7, app_now)
    state = store.load_state(BINDING, app_now)
    assert before <= state.last_attempt_at <= pg.now()
    store.record_result(status="synced", version=7, verified=True, error_code=None,
                        now=app_now, coverage={"x": [1, 1]})
    verified = store.load_state(BINDING, app_now)
    assert before <= verified.last_verified_at <= pg.now()
    store.record_result(status="synced", version=5, verified=True, error_code=None,
                        now=app_now, coverage={"x": [9, 9]})
    assert store.load_state(BINDING, app_now) == verified
    with pg.engine.begin() as conn:
        updated_at = conn.scalar(pg.text(f"SELECT updated_at FROM {pg.schema}.os_mirror_state"))
    assert before <= updated_at <= pg.now()


def test_claim_orders_by_commit_version_honours_limit_and_next_attempt(harness) -> None:
    store = harness.outbox()
    start = harness.now() if isinstance(harness, PgHarness) else T0
    later = harness.add(3, outbox_body(), start)
    first = harness.add(1, outbox_body(), start)
    future = harness.add(2, outbox_body(), start + timedelta(seconds=30))
    claimed = store.claim(1, "w1", start)
    assert [e.event_id for e in claimed] == [first]
    assert (claimed[0].lease_generation, claimed[0].attempts) == (1, 1)
    assert claimed[0].body == outbox_body() and claimed[0].commit_version == 1
    assert [e.event_id for e in store.claim(10, "w1", start)] == [later]
    assert store.claim(10, "w1", start) == []
    if isinstance(harness, PgHarness):
        harness.make_due(future)
    assert [e.event_id for e in store.claim(10, "w1", start + timedelta(seconds=30))] == [future]
    row = harness.row(first)
    assert (row["status"], row["lease_owner"]) == ("leased", "w1")
    if isinstance(harness, PgHarness):
        assert start <= row["leased_at"] == row["updated_at"] <= harness.now()
    else:
        assert row["leased_at"] == T0


def test_a_lease_is_reclaimed_only_after_lease_ttl_with_a_new_generation(harness) -> None:
    store = harness.outbox()
    event_id = harness.add(1, outbox_body(), T0)
    (first,) = store.claim(5, "crashed", T0)
    assert store.claim(5, "w2", T0 + LEASE_TTL) == []
    if isinstance(harness, PgHarness):
        harness.expire_lease(event_id)
    (second,) = store.claim(5, "w2", T0 + LEASE_TTL + timedelta(seconds=1))
    assert (second.lease_generation, second.attempts) == (2, 2)
    assert not store.finish(first, first.lease_generation, "sent", None, T0)
    assert store.finish(second, second.lease_generation, "sent", None, T0 + LEASE_TTL)
    row = harness.row(event_id)
    assert row["status"] == "sent"
    assert row["lease_owner"] is None and row["leased_at"] is None
    assert not store.finish(second, second.lease_generation, "conflict", "late", T0)
    assert harness.row(event_id)["status"] == "sent"


def test_unknown_rows_are_claimable_and_finish_records_the_retry(harness) -> None:
    store = harness.outbox()
    event_id = harness.add(1, outbox_body(), T0)
    (claimed,) = store.claim(5, "w1", T0)
    retry_at = T0 + backoff(claimed.attempts)
    assert store.finish(claimed, claimed.lease_generation, "unknown", "mirror_timeout", retry_at)
    row = harness.row(event_id)
    assert (row["status"], row["last_error_code"]) == ("unknown", "mirror_timeout")
    if isinstance(harness, PgHarness):
        assert row["next_attempt_at"] == row["updated_at"] + backoff(claimed.attempts)
    else:
        assert row["next_attempt_at"] == retry_at
    assert store.claim(5, "w1", retry_at - timedelta(seconds=1)) == []
    if isinstance(harness, PgHarness):
        harness.make_due(event_id)
    (again,) = store.claim(5, "w1", retry_at)
    assert again.attempts == 2
    with pytest.raises(OutboxError):
        store.finish(again, again.lease_generation, "leased", None, T0)


def test_verified_version_never_moves_backwards_under_out_of_order_results(harness) -> None:
    store = harness.outbox()
    state = store.load_state(BINDING, T0)
    assert (state.status, state.verified_version, state.projected_version) == ("pending", 0, 0)
    store.record_attempt(5, T0)
    store.record_attempt(7, T0)
    later = T0 + timedelta(seconds=10)
    before = harness.now() if isinstance(harness, PgHarness) else later
    store.record_result(status="synced", version=7, verified=True, error_code=None, now=later,
                        coverage={"x": [1, 1]})
    verified_at = store.load_state(BINDING, T0).last_verified_at
    if isinstance(harness, PgHarness):
        assert before <= verified_at <= harness.now()
    else:
        assert verified_at == later
    store.record_result(status="synced", version=5, verified=True, error_code=None,
                        now=later + timedelta(seconds=5), coverage={"x": [9, 9]})
    store.record_result(status="conflict", version=5, verified=False, error_code="drift",
                        now=later + timedelta(seconds=6), coverage=None)
    state = store.load_state(BINDING, T0)
    assert (state.status, state.verified_version, state.projected_version) == ("synced", 7, 7)
    assert state.last_verified_at == verified_at
    assert state.last_error_code is None
    assert state.scan_coverage == {"x": [1, 1]}
    store.record_result(status="conflict", version=7, verified=False, error_code="drift",
                        now=later, coverage=None)
    assert store.load_state(BINDING, T0).status == "conflict"
    store.record_result(status="synced", version=9, verified=True, error_code=None, now=later,
                        coverage=None)
    state = store.load_state(BINDING, T0)
    assert (state.status, state.verified_version, state.projected_version) == ("synced", 9, 9)


def test_record_attempt_marks_pending_without_erasing_unknown_or_conflict(harness) -> None:
    store = harness.outbox()
    store.load_state(BINDING, T0)
    store.record_result(status="synced", version=1, verified=True, error_code=None, now=T0,
                        coverage=None)
    store.record_attempt(2, T0)
    assert store.load_state(BINDING, T0).status == "pending"
    store.record_result(status="conflict", version=2, verified=False, error_code="drift",
                        now=T0, coverage=None)
    before = harness.now() if isinstance(harness, PgHarness) else T0
    store.record_attempt(3, T0)
    state = store.load_state(BINDING, T0)
    assert (state.status, state.projected_version) == ("conflict", 3)
    if isinstance(harness, PgHarness):
        assert before <= state.last_attempt_at <= harness.now()
    else:
        assert state.last_attempt_at == T0


@pytest.mark.parametrize("status,projected,verified,error,attempted,expected", [
    ("synced", 3, 3, None, True, "authority_fenced"),
    ("fenced", 3, 3, "authority_fenced", True, "authority_fenced"),
    ("pending", 0, 0, None, False, "authority_fenced"),
    ("synced", 4, 3, None, True, "authority_fenced_unclean"),
    ("synced", 3, 3, "readback_failed", True, "authority_fenced_unclean"),
    ("conflict", 3, 3, "drift", True, "authority_fenced_unclean"),
    ("unknown", 3, 3, "mirror_timeout", True, "authority_fenced_unclean"),
    ("pending", 3, 3, None, True, "authority_fenced_unclean"),
    ("delayed", 3, 3, None, True, "authority_fenced_unclean"),
    ("pending", 0, 0, None, True, "authority_fenced_unclean"),
    ("pending", 0, 0, "mirror_timeout", False, "authority_fenced_unclean"),
    ("fenced", 3, 3, "authority_fenced_unclean", True, "authority_fenced_unclean"),
])
def test_record_fence_decides_from_current_row(
    harness, status, projected, verified, error, attempted, expected,
):
    store = harness.outbox()
    store.load_state(BINDING, T0)  # The worker's earlier read is deliberately stale.
    values = dict(
        status=status, projected_version=projected, verified_version=verified,
        last_error_code=error, last_attempt_at=T0 if attempted else None,
        last_verified_at=T0 if verified else None,
    )
    if isinstance(harness, PgHarness):
        with harness.engine.begin() as conn:
            conn.execute(harness.text(
                f"UPDATE {harness.schema}.os_mirror_state SET "
                + ", ".join(f"{key}=:{key}" for key in values)
                + " WHERE workspace_id=:ws"
            ), {**values, "ws": WS})
    else:
        harness.store.state.update(values)
    for _ in range(2):
        code = store.record_result(
            status="fenced", version=max(projected, 1), verified=False,
            error_code=None, now=T0, coverage=None,
        )
        state = store.load_state(BINDING, T0)
        assert (state.status, state.last_error_code) == ("fenced", expected)
        assert code == expected
        assert (state.projected_version, state.verified_version) == (projected, verified)


def test_attempt_after_fenced_clean_is_pending_even_at_same_version(harness):
    store = harness.outbox()
    store.load_state(BINDING, T0)
    store.record_result(status="synced", version=3, verified=True, error_code=None,
                        now=T0, coverage=None)
    store.record_result(status="fenced", version=3, verified=False,
                        error_code="authority_fenced", now=T0, coverage=None)
    assert store.load_state(BINDING, T0).status == "fenced"
    before = harness.now() if isinstance(harness, PgHarness) else T0
    store.record_attempt(3, T0)  # Simulated crash: no record_result follows.
    state = store.load_state(BINDING, T0)
    assert state.status == "pending"
    assert state.projected_version == state.verified_version == 3
    if isinstance(harness, PgHarness):
        assert before <= state.last_attempt_at <= harness.now()
    else:
        assert state.last_attempt_at == T0


def test_the_destination_binding_is_pinned(harness) -> None:
    store = harness.outbox()
    store.load_state(BINDING, T0)
    with pytest.raises(OutboxError) as caught:
        store.load_state(destination_binding("another-sheet"), T0)
    assert caught.value.code == "mirror_destination_changed"
    assert BINDING.startswith("sheets_v4:") and len(BINDING) <= 256
    assert "mirror-sheet-1" not in BINDING


def _pg_no_transaction_or_connection_is_held_during_a_transport_call() -> None:
    from app.services.os_ledger import mirror_projection as mp
    from app.workers import os_mirror

    from tests.workers.mirror_fakes import FakeReader, FakeSheets
    from tests.workers.test_os_mirror import CLIENT, TASK, mirror_settings

    pg = PgHarness()
    try:
        checks: list[tuple[int, int]] = []

        def guard() -> None:
            checks.append((pg.open_transactions, pg.engine.pool.checkedout()))
            assert pg.open_transactions == 0 and pg.engine.pool.checkedout() == 0

        reader = FakeReader(TxCounter())
        sheets = FakeSheets(mp.MIRROR_TABS, guard=guard)
        deps = os_mirror.MirrorDeps(
            reader=reader, context=lambda: object(), outbox=pg.outbox(), transport=sheets,
            destination_binding=destination_binding("mirror-sheet-1"),
        )
        clock = Clock()
        event_ids = []
        for record, entity in ((CLIENT, "clients"), (TASK, "tasks")):
            version = reader.commit(entity, record)
            event_ids.append(pg.add(version, outbox_body(), clock()))
        sheets.fail_next_write = "lost_ack"
        settings = mirror_settings()
        outcomes = []
        for anti_entropy in (False, False, True):
            outcomes.append(os_mirror.run_once(
                deps, settings, now=clock(), lease_owner="w1", anti_entropy=anti_entropy
            ).outcome)
            clock.advance(60)
            # Advancing the fake worker clock cannot advance PostgreSQL's retry clock.
            for event_id in event_ids:
                pg.make_due(event_id)
        assert outcomes == ["unknown", "verified", "verified"]
        assert len(checks) >= 3 and set(checks) == {(0, 0)}
        state = pg.outbox().load_state(destination_binding("mirror-sheet-1"), clock())
        assert (state.status, state.verified_version) == ("synced", reader.version)
    finally:
        pg.close()


if PG_URL:  # generated only with a disposable URL, so the PG-less job never skips it
    test_pg_due_row_is_claimable_with_application_clock_behind = (
        _pg_due_row_is_claimable_with_application_clock_behind
    )
    test_pg_application_clock_ahead_does_not_bypass_backoff_or_stamp_future = (
        _pg_application_clock_ahead_does_not_bypass_backoff_or_stamp_future
    )
    test_pg_lease_takeover_uses_database_time_and_fences_stale_generation = (
        _pg_lease_takeover_uses_database_time_and_fences_stale_generation
    )
    test_pg_lease_is_not_reclaimed_at_exact_database_ttl_boundary = (
        _pg_lease_is_not_reclaimed_at_exact_database_ttl_boundary
    )
    test_pg_claim_includes_exact_database_retry_boundary = (
        _pg_claim_includes_exact_database_retry_boundary
    )
    test_pg_finish_uses_database_clock_for_backoff_and_metadata = (
        _pg_finish_uses_database_clock_for_backoff_and_metadata
    )
    test_pg_mirror_state_stamps_use_database_clock = _pg_mirror_state_stamps_use_database_clock
    test_pg_no_transaction_or_connection_is_held_during_a_transport_call = (
        _pg_no_transaction_or_connection_is_held_during_a_transport_call
    )
