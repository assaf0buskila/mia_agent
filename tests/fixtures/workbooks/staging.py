"""Shared helpers for the V2-30 stage and promote tests, written against ``LedgerEnv`` only.

They use the ledger through ``api`` protocols and the ``rows``/``version`` observers, so the same
tests run on the FakeLedger now and on PostgreSQL once V2-16 supplies its ``LedgerEnv``.
"""

from __future__ import annotations

from contextlib import contextmanager

from app.services.os_ledger import api
from app.services.os_ledger.xlsx import read_xlsx
from tests.fakes.contexts import for_worker
from tests.fakes.os_ledger_env import OWNER_ID
from tests.fixtures.workbooks.synthetic import write_book

IMPORT_WS = "ws-import"
LEDGER_TABLES = ("os_proposals", "os_receipts", "os_events", "os_outbox", "os_import_batches",
                 "os_import_rows", "os_leads", "os_clients", "os_works", "os_payments",
                 "os_time", "os_expenses", "os_tasks")


def worker(epoch: int = 1, workspace: str = IMPORT_WS):
    return for_worker("import", workspace=workspace, epoch=epoch)


def workbook(tmp_path, tables, name: str = "synthetic.xlsx"):
    return read_xlsx(write_book(tmp_path / name, tables))


def snapshot(env, workspace: str = IMPORT_WS) -> dict:
    """Row counts of every table an import touches, plus the workspace version."""
    return {table: len(env.rows(table)) for table in LEDGER_TABLES} | {
        "version": env.version(workspace)}


def next_epoch(env, workspace: str = IMPORT_WS, *, epoch: int) -> int:
    """Roll the core domain back ``active -> fenced -> shadow`` (the epoch bump) and follow it.

    Legal only while no non-import receipt exists, which holds for these tests.
    """
    for state in ("active", "fenced", "shadow"):
        env.transition(workspace, "core", state, expected_epoch=epoch, actor_owner_id=OWNER_ID)
    env.knobs.settings_epoch = epoch + 1
    return epoch + 1


def record_rows(env, table: str) -> dict[str, dict]:
    return {row["record_id"]: row for row in env.rows(table)}


class E20Ledger:
    """A ledger wrapped with erratum E20: an update into a FORK-DELETE end state refuses
    ``has_dependents`` while a live dependent exists, applied one mutation at a time.

    It records the order in which records reach ``apply``. Everything else is the inner ledger.
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.order: list[tuple[str, str]] = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    @contextmanager
    def transaction(self, ctx, **kwargs):
        with self.inner.transaction(ctx, **kwargs) as tx:
            yield _E20Tx(tx, self.order)


class _E20Tx:
    def __init__(self, tx, order) -> None:
        self._tx, self._order = tx, order

    def __getattr__(self, name):
        return getattr(self._tx, name)

    def apply(self, mutations, *, source, request_id):
        applied: list = []
        for mutation in mutations:
            end = api.FORK_DELETE_END_STATE[mutation.entity]
            before = mutation.before or {}
            into_end = mutation.op == "tombstone" or (
                end is not None and mutation.after.get(end[0]) == end[1]
                and before.get(end[0]) != end[1])
            if into_end:
                dependents = self._tx.dependents(mutation.entity, mutation.record_id)
                if dependents:
                    raise api.LedgerError("has_dependents", details={"dependents": [
                        {"entity": ref.entity, "record_id": ref.record_id} for ref in dependents]})
            self._order.append((mutation.entity, mutation.record_id))
            applied.extend(self._tx.apply([mutation], source=source, request_id=request_id))
        return tuple(applied)
