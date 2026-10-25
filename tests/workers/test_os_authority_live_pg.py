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

from app.db.os_ledger_migrate import RUNTIME_ROLE
from app.workers import os_authority as cli
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.fakes.os_ledger_builders import commit_direct, commit_import, ctx, task
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE
from tests.services.os_ledger.conftest import PG_URL_ENV, build_pg_env, pg_enabled

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
# The demoted main role that build_pg_env creates (tests/services/os_ledger/conftest.py).
FIXTURE_MAIN_ROLE = "v216_main"


def live_probes(url: str) -> cli.LiveProbes:
    return cli.LiveProbes(
        SimpleNamespace(), OWNER_ID, main_url="", ledger_url="", privileged_url=url,
        now=lambda: NOW,
    )


if pg_enabled():

    def mirror_row(connection, **values):
        row = {
            "ws": WORKSPACE, "binding": "mirror-book", "status": "synced", "projected": 3,
            "verified": 3, "error": None, "attempt": None,
        } | values
        connection.execute(text(
            "INSERT INTO assaf_os.os_mirror_state (workspace_id, destination_binding, status, "
            "projected_version, verified_version, last_error_code, last_attempt_at) "
            "VALUES (:ws, :binding, :status, :projected, :verified, :error, :attempt) "
            "ON CONFLICT (workspace_id) DO UPDATE SET status = :status, "
            "projected_version = :projected, verified_version = :verified, "
            "last_error_code = :error, last_attempt_at = :attempt"
        ), row)

    def test_the_mirror_probe_reads_the_real_state_table():
        url = os.environ[PG_URL_ENV]
        env = build_pg_env(url)
        env.ensure_workspace(WORKSPACE, OWNER_ID, 1)
        probes, engine = live_probes(url), create_engine(url)
        try:
            assert probes.mirror(WORKSPACE, 3) == "empty"
            cases = [
                ({"status": "synced", "projected": 3, "verified": 3, "error": None},
                 "verified", "verified"),
                ({"status": "synced", "projected": 3, "verified": 2, "error": None},
                 "unverified", "unverified"),
                ({"status": "synced", "projected": 3, "verified": 3, "error": "x"},
                 "unverified", "unverified"),
                ({"status": "delayed", "projected": 3, "verified": 3, "error": None},
                 "unverified", "unverified"),
                ({"status": "unknown", "projected": 3, "verified": 3, "error": None},
                 "unverified", "unverified"),
                ({"status": "conflict", "projected": 3, "verified": 3, "error": None},
                 "unverified", "unverified"),
                ({"status": "pending", "projected": 0, "verified": 0, "error": None},
                 "unverified", "empty"),
                ({"status": "pending", "projected": 0, "verified": 0, "attempt": NOW},
                 "unverified", "unverified"),
                ({"status": "synced", "projected": 0, "verified": 0, "error": None},
                 "verified", "verified"),
                ({"status": "fenced", "error": "authority_fenced"}, "unverified", "verified"),
                ({"status": "fenced", "error": "authority_fenced_unclean"},
                 "unverified", "unverified"),
                ({"status": "fenced", "verified": 2, "error": "authority_fenced"},
                 "unverified", "unverified"),
                ({"status": "fenced", "error": "readback_failed"}, "unverified", "unverified"),
            ]
            for values, expected, cleanliness in cases:
                with engine.begin() as connection:
                    mirror_row(connection, **values)
                version = values.get("projected", 3)
                assert probes.mirror(WORKSPACE, version) == expected, values
                assert probes.mirror(WORKSPACE, None) == cleanliness, values
                assert probes.mirror(WORKSPACE, version + 1) == "unverified", values
            assert probes.mirror("another-workspace", 3) == "empty"
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

    def test_the_promote_probe_reads_the_import_receipt_and_proposal_promote_leaves():
        # V2-31b finding 8: `import_not_promoted` stands on this SQL. `commit_import` is the
        # service-shaped import `promote` makes (same request id, request digest, digest binding).
        url = os.environ[PG_URL_ENV]
        env = build_pg_env(url)
        env.ensure_workspace(WORKSPACE, OWNER_ID, 1)
        probes = live_probes(url)
        digest = "d" * 64
        try:
            assert probes.import_promoted(WORKSPACE, 1, digest) is False, "nothing promoted yet"
            commit_import(env, ctx(), [task("TAS_i1")], "e" * 64)  # another manifest
            assert probes.import_promoted(WORKSPACE, 1, digest) is False
            commit_import(env, ctx(), [task("TAS_i2")], digest)
            assert probes.import_promoted(WORKSPACE, 1, digest) is True
            assert probes.import_promoted(WORKSPACE, 1, "e" * 64) is True
            assert probes.import_promoted(WORKSPACE, 1, "f" * 64) is False, "another manifest"
            assert probes.import_promoted(WORKSPACE, 2, digest) is False, "another epoch"
            assert probes.import_promoted("another-workspace", 1, digest) is False
            # A business write is no promote, whatever it is bound to.
            env.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
            commit_direct(env, ctx(), [task("TAS_w1")])
            assert probes.import_promoted(WORKSPACE, 1, "9" * 64) is False
            assert probes.import_promoted(WORKSPACE, 1, digest) is True
        finally:
            probes.close()

    def role_probes(url: str, *, main: str | None = None) -> cli.LiveProbes:
        """Probes over the fixture's demoted main role and ledger runtime role."""
        parsed = make_url(url)
        dsn = {
            role: parsed.set(username=role, password=None).render_as_string(hide_password=False)
            for role in (FIXTURE_MAIN_ROLE, RUNTIME_ROLE)
        }
        return cli.LiveProbes(
            SimpleNamespace(), OWNER_ID, main_url=main or dsn[FIXTURE_MAIN_ROLE],
            ledger_url=dsn[RUNTIME_ROLE], privileged_url=url, now=lambda: NOW,
        )

    def test_the_role_probes_read_the_real_catalogs():
        # `main_role_unsafe` and `ledger_role_unsafe` stand on these two probes. The fakes cannot
        # tell whether the real posture check and the real grant-map validator are wired to the
        # right engines, so they run here against the fixture's demoted main role and runtime role.
        url = os.environ[PG_URL_ENV]
        build_pg_env(url)  # the V2-14/14b migrations, the three ledger roles, the demoted main role
        probes, superuser = role_probes(url), role_probes(url, main=url)
        try:
            assert probes.main_role_safe() is True
            assert probes.ledger_role_safe() is True
            # The privileged DSN is not a demoted application role.
            assert superuser.main_role_safe() is False
        finally:
            probes.close()
            superuser.close()

    def test_the_role_probes_fail_when_the_main_role_can_reach_the_ledger():
        url = os.environ[PG_URL_ENV]
        build_pg_env(url)
        admin, probes = create_engine(url), role_probes(url)
        try:
            assert (probes.main_role_safe(), probes.ledger_role_safe()) == (True, True)
            with admin.begin() as connection:
                connection.execute(text(f"GRANT USAGE ON SCHEMA assaf_os TO {FIXTURE_MAIN_ROLE}"))
            try:
                assert probes.main_role_safe() is False
                assert probes.ledger_role_safe() is False
            finally:
                with admin.begin() as connection:
                    connection.execute(text(
                        f"REVOKE USAGE ON SCHEMA assaf_os FROM {FIXTURE_MAIN_ROLE}"))
            assert (probes.main_role_safe(), probes.ledger_role_safe()) == (True, True)
        finally:
            probes.close()
            admin.dispose()

    def test_the_probe_engines_connect_with_a_bounded_timeout_and_dispose():
        url = os.environ[PG_URL_ENV]
        env = build_pg_env(url)
        env.ensure_workspace(WORKSPACE, OWNER_ID, 1)
        probes = live_probes(url)
        probes.mirror(WORKSPACE, 0)
        assert list(probes._engines) == ["privileged"]
        probes.close()
        assert probes._engines == {}
