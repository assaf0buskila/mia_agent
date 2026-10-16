"""V2-29 outbox lease and ``os_mirror_state`` rules, on the fake and (when a disposable URL is
set) on PostgreSQL 16 with the V2-14 table DDL taken verbatim from the merged migration.

A "pg" case is generated only when ``MIA_OS_LEDGER_TEST_POSTGRES_URL`` is set, so the PG-less
``checks`` job generates none and never skips one.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from app.services.os_ledger.outbox import (
    LEASE_TTL,
    OutboxError,
    PgOutbox,
    backoff,
    destination_binding,
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
                    "VALUES (:ws, :id, :v, CAST(:body AS jsonb), :at, :at, :at)"
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


def test_claim_orders_by_commit_version_honours_limit_and_next_attempt(harness) -> None:
    store = harness.outbox()
    later = harness.add(3, outbox_body(), T0)
    first = harness.add(1, outbox_body(), T0)
    future = harness.add(2, outbox_body(), T0 + timedelta(seconds=30))
    claimed = store.claim(1, "w1", T0)
    assert [e.event_id for e in claimed] == [first]
    assert (claimed[0].lease_generation, claimed[0].attempts) == (1, 1)
    assert claimed[0].body == outbox_body() and claimed[0].commit_version == 1
    assert [e.event_id for e in store.claim(10, "w1", T0)] == [later]
    assert store.claim(10, "w1", T0) == []
    assert [e.event_id for e in store.claim(10, "w1", T0 + timedelta(seconds=30))] == [future]
    row = harness.row(first)
    assert (row["status"], row["lease_owner"]) == ("leased", "w1")
    assert row["leased_at"] == T0


def test_a_lease_is_reclaimed_only_after_lease_ttl_with_a_new_generation(harness) -> None:
    store = harness.outbox()
    event_id = harness.add(1, outbox_body(), T0)
    (first,) = store.claim(5, "crashed", T0)
    assert store.claim(5, "w2", T0 + LEASE_TTL) == []
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
    assert row["next_attempt_at"] == retry_at
    assert store.claim(5, "w1", retry_at - timedelta(seconds=1)) == []
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
    store.record_result(status="synced", version=7, verified=True, error_code=None, now=later,
                        coverage={"x": [1, 1]})
    store.record_result(status="synced", version=5, verified=True, error_code=None,
                        now=later + timedelta(seconds=5), coverage={"x": [9, 9]})
    store.record_result(status="conflict", version=5, verified=False, error_code="drift",
                        now=later + timedelta(seconds=6), coverage=None)
    state = store.load_state(BINDING, T0)
    assert (state.status, state.verified_version, state.projected_version) == ("synced", 7, 7)
    assert state.last_verified_at == later
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
    store.record_attempt(3, T0)
    state = store.load_state(BINDING, T0)
    assert (state.status, state.projected_version, state.last_attempt_at) == ("conflict", 3, T0)


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
        for record, entity in ((CLIENT, "clients"), (TASK, "tasks")):
            version = reader.commit(entity, record)
            pg.add(version, outbox_body(), clock())
        sheets.fail_next_write = "lost_ack"
        settings = mirror_settings()
        outcomes = []
        for anti_entropy in (False, False, True):
            outcomes.append(os_mirror.run_once(
                deps, settings, now=clock(), lease_owner="w1", anti_entropy=anti_entropy
            ).outcome)
            clock.advance(60)
        assert outcomes == ["unknown", "verified", "verified"]
        assert len(checks) >= 3 and set(checks) == {(0, 0)}
        state = pg.outbox().load_state(destination_binding("mirror-sheet-1"), clock())
        assert (state.status, state.verified_version) == ("synced", reader.version)
    finally:
        pg.close()


if PG_URL:  # generated only with a disposable URL, so the PG-less job never skips it
    test_pg_no_transaction_or_connection_is_held_during_a_transport_call = (
        _pg_no_transaction_or_connection_is_held_during_a_transport_call
    )
