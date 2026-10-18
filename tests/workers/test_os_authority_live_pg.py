"""V2-31: the live probes of ``mia os-authority`` against the REAL assaf_os tables.

``LiveProbes.mirror`` and ``LiveProbes.non_import_receipts`` are SQL on the privileged DSN; the
fakes cannot tell whether a column name or a status value is wrong. These tests run only when
``MIA_OS_LEDGER_TEST_POSTGRES_URL`` is set (the V2-16f rule: the case does not exist otherwise, so
it is never skipped), on the disposable database the ledger fixture migrates.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace

from app.workers import os_authority as cli
from sqlalchemy import create_engine, text

from tests.fakes.os_ledger_builders import commit_direct, commit_import, ctx, task
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE
from tests.services.os_ledger.conftest import PG_URL_ENV, build_pg_env, pg_enabled

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def live_probes(url: str) -> cli.LiveProbes:
    return cli.LiveProbes(
        SimpleNamespace(), OWNER_ID, main_url="", ledger_url="", privileged_url=url,
        now=lambda: NOW,
    )


if pg_enabled():

    def mirror_row(connection, **values):
        row = {
            "ws": WORKSPACE, "binding": "mirror-book", "status": "synced", "projected": 3,
            "verified": 3, "error": None,
        } | values
        connection.execute(text(
            "INSERT INTO assaf_os.os_mirror_state (workspace_id, destination_binding, status, "
            "projected_version, verified_version, last_error_code) "
            "VALUES (:ws, :binding, :status, :projected, :verified, :error) "
            "ON CONFLICT (workspace_id) DO UPDATE SET status = :status, "
            "projected_version = :projected, verified_version = :verified, "
            "last_error_code = :error"
        ), row)

    def test_the_mirror_probe_reads_the_real_state_table():
        url = os.environ[PG_URL_ENV]
        env = build_pg_env(url)
        env.ensure_workspace(WORKSPACE, OWNER_ID, 1)
        probes, engine = live_probes(url), create_engine(url)
        try:
            assert probes.mirror(WORKSPACE) == "empty"
            cases = [
                ({"status": "synced", "projected": 3, "verified": 3, "error": None}, "verified"),
                ({"status": "synced", "projected": 3, "verified": 2, "error": None}, "unverified"),
                ({"status": "synced", "projected": 3, "verified": 3, "error": "x"}, "unverified"),
                ({"status": "delayed", "projected": 3, "verified": 3, "error": None}, "unverified"),
                ({"status": "unknown", "projected": 3, "verified": 3, "error": None}, "unverified"),
                ({"status": "conflict", "projected": 3, "verified": 3, "error": None},
                 "unverified"),
                ({"status": "pending", "projected": 0, "verified": 0, "error": None},
                 "unverified"),
                ({"status": "synced", "projected": 0, "verified": 0, "error": None}, "verified"),
            ]
            for values, expected in cases:
                with engine.begin() as connection:
                    mirror_row(connection, **values)
                assert probes.mirror(WORKSPACE) == expected, values
            assert probes.mirror("another-workspace") == "empty"
        finally:
            probes.close()
            engine.dispose()

    def test_the_receipt_probe_counts_only_non_import_receipts_at_the_epoch():
        url = os.environ[PG_URL_ENV]
        env = build_pg_env(url)
        env.ensure_workspace(WORKSPACE, OWNER_ID, 1)
        probes = live_probes(url)
        try:
            assert probes.non_import_receipts(WORKSPACE, 1) == 0
            commit_import(env, ctx(), [task("TAS_i1")], "e" * 64)  # an import runs at `shadow`
            assert probes.non_import_receipts(WORKSPACE, 1) == 0, "an import is not a write"
            env.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
            commit_direct(env, ctx(), [task("TAS_w1")])
            assert probes.non_import_receipts(WORKSPACE, 1) == 1
            assert probes.non_import_receipts(WORKSPACE, 2) == 0, "another epoch is another run"
            assert probes.non_import_receipts("another-workspace", 1) == 0
        finally:
            probes.close()

    def test_the_probe_engines_connect_with_a_bounded_timeout_and_dispose():
        url = os.environ[PG_URL_ENV]
        env = build_pg_env(url)
        env.ensure_workspace(WORKSPACE, OWNER_ID, 1)
        probes = live_probes(url)
        probes.mirror(WORKSPACE)
        assert list(probes._engines) == ["privileged"]
        probes.close()
        assert probes._engines == {}
