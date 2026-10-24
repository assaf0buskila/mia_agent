"""V2-26 os-retention job (§12.2, §18.2, §22 E6, E8): ``expire_due``, then the fixed deletes.

The unit tests drive ``run`` with stand-in service and engine objects. The PostgreSQL test runs the
real job against the disposable server: the runtime role expires, the retention role deletes, and
nothing outside the three fixed statements' predicates is touched.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from app.services.os_ledger.role_policy import RETENTION_DELETE_SQL
from app.workers import os_retention

from tests.fakes.os_ledger_env import NOW, WORKSPACE
from tests.services.os_ledger.statements.conftest import CASES
from tests.services.os_ledger.statements.support import (
    World,
    batch,
    make_settings,
    pg_url,
    three_lines,
)

WEEK = 7 * 24 * 3600


class Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class Connection:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def execute(self, clause, params=None):
        sql = str(clause)
        self.engine.executed.append((sql, dict(params or {})))
        failure = self.engine.fail.get(sql)
        if failure is not None:
            raise failure
        return Result(self.engine.counts.get(sql, 0))


class Engine:
    def __init__(self, counts=None, fail=None) -> None:
        self.executed: list[tuple[str, dict]] = []
        self.counts = counts or {}
        self.fail = fail or {}
        self.disposed = False

    @contextmanager
    def begin(self):
        yield Connection(self)

    def dispose(self):
        self.disposed = True


class Service:
    def __init__(self, result=2, error=None) -> None:
        self.result, self.error, self.calls = result, error, []

    def expire_due(self, ctx):
        self.calls.append(ctx)
        if self.error is not None:
            raise self.error
        return self.result


def test_it_runs_exactly_the_three_fixed_statements_with_only_the_workspace_bound():
    engine = Engine(counts={RETENTION_DELETE_SQL["purge"]: 5, RETENTION_DELETE_SQL["audit"]: 2})
    service = Service()
    summary = os_retention.run(make_settings(), service=service, context=lambda s: "ctx",
                               retention_engine=engine)
    assert summary == {"expired": 2, "purge": 5, "backstop": 0, "audit": 2, "errors": []}
    assert [sql for sql, _ in engine.executed] == [RETENTION_DELETE_SQL[k]
                                                   for k in ("purge", "backstop", "audit")]
    assert all(params == {"workspace_id": WORKSPACE} for _, params in engine.executed)
    assert service.calls == ["ctx"] and not engine.disposed  # a passed engine stays the caller's


def test_the_module_holds_no_sql_of_its_own():
    tree = ast.parse(inspect.getsource(os_retention))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
                  and node.body and isinstance(node.body[0], ast.Expr)
                  and isinstance(node.body[0].value, ast.Constant)}
    words = re.compile(r"\b(DELETE|UPDATE|INSERT|DROP|TRUNCATE|SELECT|ALTER|GRANT)\b", re.I)
    literals = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                and isinstance(node.value, str) and id(node) not in docstrings]
    assert not [text for text in literals if words.search(text)]


def test_a_failed_expiry_does_not_stop_the_deletes():
    engine = Engine()
    summary = os_retention.run(make_settings(), service=Service(error=RuntimeError("down")),
                               context=lambda s: "ctx", retention_engine=engine)
    assert summary["errors"] == ["expire_due:runtimeerror"] and summary["expired"] == 0
    assert len(engine.executed) == 3  # the backstop still runs when the runtime path is down


def test_a_failed_delete_does_not_stop_the_others():
    engine = Engine(fail={RETENTION_DELETE_SQL["backstop"]: RuntimeError("locked")})
    summary = os_retention.run(make_settings(), service=Service(), context=lambda s: "ctx",
                               retention_engine=engine)
    assert summary["errors"] == ["backstop:runtimeerror"]
    assert [sql for sql, _ in engine.executed] == [RETENTION_DELETE_SQL[k]
                                                   for k in ("purge", "backstop", "audit")]


def test_a_missing_authorize_module_fails_closed_but_the_deletes_run(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, os_retention.AUTHORIZE_MODULE, None)
    engine = Engine()
    summary = os_retention.run(make_settings(), service=Service(), retention_engine=engine)
    assert summary["errors"] == ["expire_due:authorize_missing"] and len(engine.executed) == 3


def test_a_missing_retention_dsn_is_reported_after_the_expiry(monkeypatch):
    monkeypatch.delenv(os_retention.RETENTION_DSN_ENV, raising=False)
    service = Service(result=1)
    summary = os_retention.run(make_settings(), service=service, context=lambda s: "ctx")
    assert summary["errors"] == ["retention_dsn_missing"] and summary["expired"] == 1


def test_main_prints_counts_only_and_exits_non_zero_on_any_failure(monkeypatch, capsys):
    monkeypatch.setattr(os_retention, "configure_logging", lambda: None)
    monkeypatch.setattr(os_retention, "get_settings", make_settings)
    monkeypatch.setattr(os_retention, "run", lambda settings: {
        "expired": 0, "purge": 0, "backstop": 0, "audit": 0, "errors": ["audit:x"]})
    with pytest.raises(SystemExit) as caught:
        os_retention.main()
    assert caught.value.code == 1
    assert json.loads(capsys.readouterr().out)["errors"] == ["audit:x"]


# ---- PostgreSQL ---------------------------------------------------------------------------------


@pytest.mark.skipif("pg" not in CASES, reason="needs MIA_OS_LEDGER_TEST_POSTGRES_URL")
class TestOnPostgres:
    @pytest.fixture
    def pg(self, request):
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url

        world = World("pg", request=request)
        parsed = make_url(pg_url())
        engine = create_engine(parsed.set(username="assaf_os_retention", password=None))
        yield world, engine
        engine.dispose()

    @staticmethod
    def count(world, table: str, where: str = "true") -> int:
        from sqlalchemy import text
        with world._admin_engine.connect() as conn:
            return int(conn.scalar(text(f"SELECT count(*) FROM assaf_os.{table} WHERE {where}")))

    @staticmethod
    def seed_old_batch_and_audit(world) -> None:
        """What the runtime path left behind: a proposal that expired 3 days ago with live
        detail, and assistant audit rows of 200 and 2 days ago."""
        from sqlalchemy import text
        created = NOW - timedelta(days=10)
        with world._admin_engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO assaf_os.os_proposals (workspace_id, proposal_id, request_id, "
                "request_digest, envelope_digest, owner_id, epoch, approval_id, rejection_id, "
                "entity, record_id, expected_revision, mutations, dependency_digest, source, "
                "kind, status, created_at, expires_at) VALUES (:ws, 'PRO_OLD', :r, :d, :d, '123', "
                "1, :a, :j, 'statements', 'STM_old', 0, '[]'::jsonb, :d, 'ASSISTANT_PASTE', "
                "'statement_batch', 'pending', :created, :expires)"),
                {"ws": WORKSPACE, "r": str(uuid4()), "a": str(uuid4()), "j": str(uuid4()),
                 "d": "0" * 64, "created": created, "expires": created + timedelta(days=7)})
            conn.execute(text(
                "INSERT INTO assaf_os.os_statement_batch_lines (workspace_id, proposal_id, "
                "line_key, occurrence_index, detail) VALUES (:ws, 'PRO_OLD', :k, 0, "
                "'{\"merchant_display\":\"STALE DETAIL\"}'::jsonb)"),
                {"ws": WORKSPACE, "k": "a" * 64})
            for days in (200, 2):
                conn.execute(text(
                    "INSERT INTO assaf_os.os_assistant_audit (workspace_id, audit_id, "
                    "occurred_at, operation, reason, ok, client_ip_hmac) VALUES (:ws, :id, "
                    "now() - make_interval(days => :days), 'submit', 'ok', true, :h)"),
                    {"ws": WORKSPACE, "id": str(uuid4()), "days": days, "h": "b" * 64})

    def test_the_job_expires_as_runtime_then_deletes_as_retention(self, pg):
        world, retention = pg
        rejected = world.paste(batch(three_lines(), number=1))
        world.reject(rejected.proposal_id)  # three purged lines wait for the retention role
        world.paste(batch(three_lines(), number=2, charge_date="2026-11-15"))  # live detail
        self.seed_old_batch_and_audit(world)
        settings = world.config

        first = os_retention.run(settings, service=world.service,
                                 context=lambda s: world.worker, retention_engine=retention)
        # PRO_OLD has no registry row, yet the runtime path finds it by its detail and expires it:
        # its line is purged (not left to the backstop) and the retention role then deletes it.
        assert first == {"expired": 1, "purge": 4, "backstop": 0, "audit": 1, "errors": []}
        assert self.count(world, "os_proposals", "proposal_id = 'PRO_OLD' AND status = 'expired'"
                          ) == 1
        assert self.count(world, "os_receipts", "status = 'expired'") == 1
        assert self.count(world, "os_statement_batch_lines", "detail IS NOT NULL") == 3
        assert self.count(world, "os_assistant_audit") == 1  # the 2-day-old row stays
        assert self.count(world, "os_statements") == 2 and self.count(world, "os_proposals") == 3

        world.advance(WEEK + 1)  # the live batch is now overdue
        second = os_retention.run(settings, service=world.service,
                                  context=lambda s: world.worker, retention_engine=retention)
        assert second == {"expired": 1, "purge": 3, "backstop": 0, "audit": 0, "errors": []}
        assert self.count(world, "os_statement_batch_lines") == 0
        assert self.count(world, "os_receipts", "status = 'expired'") == 2

    def test_the_backstop_deletes_old_detail_when_the_runtime_path_is_down(self, pg):
        """I11: whatever the runtime path does, detail never outlives expires_at + 1 day."""
        from app.services.os_ledger import api
        world, retention = pg
        self.seed_old_batch_and_audit(world)

        def down(_settings):
            raise api.LedgerError("ledger_unavailable", reason="runtime_down")

        summary = os_retention.run(world.config, service=world.service, context=down,
                                   retention_engine=retention)
        assert summary == {"expired": 0, "purge": 0, "backstop": 1, "audit": 1,
                           "errors": ["expire_due:runtime_down"]}
        assert self.count(world, "os_statement_batch_lines") == 0
        assert self.count(world, "os_proposals", "proposal_id = 'PRO_OLD' AND status = 'pending'"
                          ) == 1

    def test_the_retention_role_can_do_nothing_else(self, pg):
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError
        world, retention = pg
        world.paste(batch(three_lines()))
        for statement in ("DELETE FROM assaf_os.os_statements",
                          "DELETE FROM assaf_os.os_proposals",
                          "UPDATE assaf_os.os_statement_batch_lines SET detail = NULL",
                          "DELETE FROM assaf_os.os_merchant_rules",
                          "SELECT * FROM assaf_os.os_statements"):
            with pytest.raises(DBAPIError), retention.begin() as conn:
                conn.execute(text(statement))

    def test_it_does_nothing_outside_its_predicates(self, pg):
        world, retention = pg
        submitted = world.paste(batch(three_lines()))
        world.approve(submitted.proposal_id)
        before = {t: self.count(world, t) for t in (
            "os_statements", "os_statement_lines", "os_merchant_rules", "os_proposals",
            "os_receipts", "os_events", "os_outbox", "os_expenses")}
        os_retention.run(world.config, service=world.service, context=lambda s: world.worker,
                         retention_engine=retention)
        after = {t: self.count(world, t) for t in before}
        assert after == before
