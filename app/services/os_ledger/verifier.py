"""The ``os_verifier`` check (V2-18, §8): recompute in full, flag drift (``summary_drift``).

``verify(settings)`` reads ONE REPEATABLE READ READ ONLY snapshot of the workspace
``settings.assaf_os_workspace_id`` on the runtime DSN (``owner_os_ledger_database_url``, role
``assaf_os_ledger``), behind the §5.5 guard, and rolls it back. It is context-free like
``api.core_authority_state``: a job has no owner context, and it returns only a report of counts
and reason codes.

In that snapshot it loads every live record, then:
* runs the full ``validate_state`` (the scoped validator's oracle) -> ``state_invalid``;
* runs the full ``build_summaries`` and compares each month with ``os_summary_monthly``, the
  authoritative store: a missing, extra (non-empty) or different month row, or an
  ``os_workspaces.summaries`` header other than ``{coverage: "complete", calculated_version <=
  version}`` (or ``{}`` before the first apply) -> ``summary_drift``;
* asserts that ``os_summary_cycle`` is empty (reserved and unused, §8) ->
  ``summary_cycle_not_empty``.

It never writes and never repairs: drift is for a human. It logs one line, ``os_verifier op=verify
outcome=<ok|drift|refused> reason=<code> ms=<int>`` plus counts and booleans, never a record field
or an amount (I7). ``app/workers/os_verifier.main()`` turns the report into an exit code; V2-31
registers the job.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.services.os_ledger import api
from app.services.os_ledger.domain import DomainError, validate_state
from app.services.os_ledger.guard import check_identity, engine_for, guard_for, refuse
from app.services.os_ledger.summaries import SummaryInputError, _month_start, build_summaries
from app.services.os_ledger.tx import record_columns, wire

__all__ = ["VerifyReport", "drifted_months", "verify", "verify_snapshot"]

_LOG = logging.getLogger("mia.os_ledger")
EMPTY = {"rows": []}


@dataclass(frozen=True)
class VerifyReport:
    workspace_present: bool
    observed_version: int
    state_valid: bool
    months_checked: int
    drift_months: int
    cycle_rows: int
    reasons: tuple[str, ...]  # in priority order; () when clean

    @property
    def ok(self) -> bool:
        return not self.reasons


def drifted_months(expected: Mapping[date, Mapping[str, Any]],
                   stored: Mapping[date, Mapping[str, Any]]) -> list[date]:
    """Months whose stored row differs from the full recompute. A month the recompute does not
    produce may keep an empty row (the runtime role cannot DELETE), never a non-empty one."""
    drift = [month for month, content in expected.items() if stored.get(month) != content]
    drift += [month for month, content in stored.items()
              if month not in expected and content != EMPTY]
    return sorted(drift)


def _header_ok(header: Any, version: int, has_months: bool) -> bool:
    if header == {}:
        return not has_months
    return (isinstance(header, Mapping) and set(header) == {"coverage", "calculated_version"}
            and header["coverage"] == "complete"
            and type(header["calculated_version"]) is int
            and 0 < header["calculated_version"] <= version)


def verify_snapshot(conn: Any, workspace_id: str) -> VerifyReport:
    """The checks, inside an already open REPEATABLE READ READ ONLY transaction on ``conn``."""
    params = {"ws": workspace_id}
    head = conn.execute(text("SELECT version, summaries FROM assaf_os.os_workspaces "
                             "WHERE workspace_id = :ws"), params).first()
    if head is None:
        return VerifyReport(False, 0, False, 0, 0, 0, ("workspace_missing",))
    version = int(head.version)
    records = {}
    for entity in api.ENTITIES:
        rows = conn.execute(text(
            f"SELECT {record_columns(entity)} FROM assaf_os.os_{entity} "
            "WHERE workspace_id = :ws AND deleted_at IS NULL"), params).all()
        records[entity] = sorted((wire(entity, row) for row in rows),
                                 key=lambda record, e=entity: record[api.ID_KEYS[e]])
    reasons: list[str] = []
    try:
        validate_state(records)
        state_valid = True
    except DomainError:
        state_valid = False
    stored = {row.month_start: row.summary for row in conn.execute(text(
        "SELECT month_start, summary FROM assaf_os.os_summary_monthly "
        "WHERE workspace_id = :ws"), params)}
    expected: dict[date, dict[str, list]] = {}
    try:
        for row in build_summaries(records)["monthly"]:
            expected.setdefault(_month_start(row["month"]), {"rows": []})["rows"].append(row)
        drift = drifted_months(expected, stored)
        if drift or not _header_ok(head.summaries, version, bool(stored)):
            reasons.append("summary_drift")
    except SummaryInputError:
        state_valid, drift = False, []
    cycle_rows = int(conn.execute(text(
        "SELECT count(*) FROM assaf_os.os_summary_cycle WHERE workspace_id = :ws"),
        params).scalar())
    if cycle_rows:
        reasons.append("summary_cycle_not_empty")
    if not state_valid:
        reasons.append("state_invalid")
    return VerifyReport(True, version, state_valid, len(set(expected) | set(stored)), len(drift),
                        cycle_rows, tuple(reasons))


def verify(settings: Any) -> VerifyReport:
    """One read-only verification of ``settings.assaf_os_workspace_id`` (see the module doc)."""
    started = time.monotonic()
    dsn = getattr(settings, "owner_os_ledger_database_url", "") or ""
    main = getattr(settings, "database_url", "") or ""
    if not dsn:
        raise refuse("ledger_dsn_missing")
    if not main:
        raise refuse("main_dsn_missing")
    workspace_id = getattr(settings, "assaf_os_workspace_id", "") or ""
    guard_for(dsn, main).ensure()
    try:
        with engine_for(dsn).connect() as conn:
            trans = conn.begin()
            try:
                conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                identity = conn.execute(text(
                    "SELECT current_user::text AS current_role, "
                    "session_user::text AS session_role")).one()
                check_identity(identity.current_role, identity.session_role)
                report = verify_snapshot(conn, workspace_id)
            finally:
                trans.rollback()
    except SQLAlchemyError:
        _LOG.warning("os_verifier op=verify outcome=refused reason=read_failed ms=%d",
                     _ms(started))
        raise api.LedgerError("ledger_unavailable", reason="read_failed") from None
    outcome = "ok" if report.ok else ("refused" if not report.workspace_present else "drift")
    log = _LOG.info if report.ok else _LOG.warning
    log("os_verifier op=verify outcome=%s reason=%s ms=%d months=%d drift_months=%d "
        "cycle_empty=%s state_valid=%s", outcome, report.reasons[0] if report.reasons else "none",
        _ms(started), report.months_checked, report.drift_months,
        str(report.cycle_rows == 0).lower(), str(report.state_valid).lower())
    return report


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
