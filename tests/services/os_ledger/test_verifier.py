"""§8/§20 V2-18: the os_verifier job recomputes in full and flags drift (``reason=summary_drift``).

It reads one REPEATABLE READ READ ONLY snapshot on the runtime DSN (behind the §5.5 guard), runs the
full ``validate_state`` and ``build_summaries`` over every live record, and compares the result with
``os_summary_monthly`` (the authoritative store) and ``os_workspaces.summaries``. It asserts that
``os_summary_cycle`` is empty (reserved, unused). It never writes and logs reason codes and counts
only (I7). ``app/workers/os_verifier.main()`` maps the report to an exit code; V2-31 registers it.
"""

from __future__ import annotations

import logging
import os
from datetime import date

import pytest
from app.services.os_ledger import api, verifier
from app.workers import os_verifier
from sqlalchemy import create_engine, text

from tests.fakes.os_ledger_builders import (
    client,
    commit_direct,
    ctx,
    expense,
    payment,
    task,
    time_entry,
    work,
)
from tests.fakes.os_ledger_env import WORKSPACE
from tests.services.os_ledger.conftest import PG_URL_ENV, _prepare, build_pg_env, pg_enabled


@pytest.fixture
def pg():
    if not pg_enabled():
        pytest.skip("disposable MIA_OS_LEDGER_TEST_POSTGRES_URL required")
    env = _prepare(build_pg_env(os.environ[PG_URL_ENV]))
    commit_direct(env, ctx(), [client(), work(), payment(), time_entry(),
                               expense(date="2026-08-02"), task()])
    commit_direct(env, ctx(), [payment("PAY_2", date="2026-09-05", actual_amount_ils=250)])
    return env


def settings_of(env):
    return env.transaction.__self__.settings


def admin(sql, params=None):
    engine = create_engine(os.environ[PG_URL_ENV])
    try:
        with engine.begin() as conn:
            conn.execute(text(sql), params or {})
    finally:
        engine.dispose()


# --- pure comparison (no database) -----------------------------------------------------------


def test_month_drift_compares_the_full_recompute_with_the_stored_rows():
    expected = {date(2026, 10, 1): {"rows": [{"month": "2026-10", "currency": "ILS"}]}}
    assert verifier.drifted_months(expected, dict(expected)) == []
    assert verifier.drifted_months(expected, {}) == [date(2026, 10, 1)]
    stale = {date(2026, 9, 1): {"rows": []}, **expected}  # an emptied month keeps an empty row
    assert verifier.drifted_months(expected, stale) == []
    ghost = {date(2026, 9, 1): {"rows": [{"month": "2026-09"}]}, **expected}
    assert verifier.drifted_months(expected, ghost) == [date(2026, 9, 1)]
    tampered = {date(2026, 10, 1): {"rows": [{"month": "2026-10", "currency": "USD"}]}}
    assert verifier.drifted_months(expected, tampered) == [date(2026, 10, 1)]


# --- PG --------------------------------------------------------------------------------------


def test_a_consistent_workspace_verifies_clean(pg, caplog):
    caplog.set_level(logging.INFO, logger="mia.os_ledger")
    report = verifier.verify(settings_of(pg))
    assert report.ok and report.reasons == () and report.drift_months == 0
    assert report.months_checked == 3 and report.cycle_rows == 0 and report.state_valid
    assert report.observed_version == pg.version()
    assert "os_verifier op=verify outcome=ok reason=none" in caplog.text
    assert os_verifier.main([], settings=settings_of(pg)) == 0


def test_a_tampered_month_row_is_flagged_summary_drift(pg, caplog):
    caplog.set_level(logging.INFO, logger="mia.os_ledger")
    admin("UPDATE assaf_os.os_summary_monthly SET summary = jsonb_set(summary, "
          "'{rows,0,paid_income_ils}', '999999') WHERE workspace_id = :ws "
          "AND month_start = DATE '2026-10-01'", {"ws": WORKSPACE})
    report = verifier.verify(settings_of(pg))
    assert not report.ok and report.reasons == ("summary_drift",) and report.drift_months == 1
    line = next(r.getMessage() for r in caplog.records if "op=verify" in r.getMessage())
    assert "outcome=drift reason=summary_drift" in line and "999999" not in caplog.text
    assert os_verifier.main([], settings=settings_of(pg)) == 1


def test_a_missing_month_row_or_header_is_summary_drift(pg):
    admin("DELETE FROM assaf_os.os_summary_monthly WHERE workspace_id = :ws "
          "AND month_start = DATE '2026-08-01'", {"ws": WORKSPACE})
    assert verifier.verify(settings_of(pg)).reasons == ("summary_drift",)


def test_the_header_must_name_complete_coverage_and_a_reached_version(pg):
    admin("UPDATE assaf_os.os_workspaces SET summaries = "
          "'{\"coverage\":\"complete\",\"calculated_version\":99}' WHERE workspace_id = :ws",
          {"ws": WORKSPACE})
    assert verifier.verify(settings_of(pg)).reasons == ("summary_drift",)


def test_a_row_in_the_reserved_cycle_table_is_flagged(pg):
    admin("INSERT INTO assaf_os.os_summary_cycle (workspace_id, cycle_id, version, starts_on, "
          "ends_on, summary) VALUES (:ws, 'c1', 1, DATE '2026-10-01', DATE '2026-10-31', "
          "'{}'::jsonb)", {"ws": WORKSPACE})
    report = verifier.verify(settings_of(pg))
    assert report.reasons == ("summary_cycle_not_empty",) and report.cycle_rows == 1


def test_an_invalid_record_state_is_flagged_state_invalid(pg):
    admin("UPDATE assaf_os.os_works SET deleted_at = now(), "
          "deleted_by_request_id = '00000000-0000-4000-8000-000000000001' "
          "WHERE workspace_id = :ws", {"ws": WORKSPACE})
    report = verifier.verify(settings_of(pg))
    assert "state_invalid" in report.reasons and not report.state_valid


def test_the_verifier_reads_one_read_only_snapshot_and_writes_nothing(pg, monkeypatch):
    seen = []
    original = verifier.verify_snapshot

    def probe(conn, workspace_id):
        report = original(conn, workspace_id)
        row = conn.execute(text(
            "SELECT current_setting('transaction_isolation') AS iso, "
            "current_setting('transaction_read_only') AS ro, current_user AS who, "
            "COALESCE(sum(n_tup_ins + n_tup_upd + n_tup_del), 0) AS writes "
            "FROM pg_stat_xact_user_tables")).one()
        seen.append((row.iso, row.ro, row.who, int(row.writes)))
        return report

    monkeypatch.setattr(verifier, "verify_snapshot", probe)
    before = (pg.rows("os_summary_monthly"), pg.rows("os_workspaces"))
    assert verifier.verify(settings_of(pg)).ok
    assert seen == [("repeatable read", "on", "assaf_os_ledger", 0)]
    assert (pg.rows("os_summary_monthly"), pg.rows("os_workspaces")) == before


def test_a_missing_workspace_or_dsn_is_refused_with_a_reason(pg):
    settings = settings_of(pg)

    class Elsewhere:
        def __getattr__(self, name):
            return "nowhere" if name == "assaf_os_workspace_id" else getattr(settings, name)

    report = verifier.verify(Elsewhere())
    assert not report.ok and report.reasons == ("workspace_missing",)

    class NoDsn:
        owner_os_ledger_database_url = ""
        database_url = ""
        assaf_os_workspace_id = WORKSPACE

    with pytest.raises(api.LedgerError) as caught:
        verifier.verify(NoDsn())
    assert (caught.value.code, caught.value.reason) == ("ledger_unavailable", "ledger_dsn_missing")
    assert os_verifier.main([], settings=NoDsn()) == 2
