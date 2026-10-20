"""``IncrementalSummaries``: ``os_summary_monthly`` maintained inside every apply (V2-18, §8).

``os_summary_monthly`` is the authoritative store (one row per month, ``{"rows": [<v1 monthly row
per currency>]}``). The full maintainer (``summaries.FullSummaries``) recomputes every month from
every record inside each apply; this one recomputes only the months an apply can change and writes
exactly the rows the full one would write, with the same ``version`` and ``calculated_at``. It
replaces the full default in ``build_ledger()`` only because a property test proves the two equal,
row for row and refusal for refusal, over random commit/undo/decide sequences on the FakeLedger
and on PostgreSQL (``tests/services/os_ledger/test_summaries_property.py``).

Which months. A v1 monthly row of (month, currency) reads the payments, expenses and time entries
dated in that month, plus the currency of each such time entry's work. So the affected months are
the months of every touched payment, expense and time entry, before AND after (``Mutation.before``
is the current wire record the CAS checked, §5.1), plus every month holding a live payment or time
entry of a touched work. Those months are rebuilt with the same ``build_summaries`` over exactly
their records (and the works their time entries name), so each rebuilt row equals the full one.

Refusals. The full recompute also refuses an apply whose sums overflow the safe-integer range
anywhere (a month, a currency total, a work, a client, an hourly rate). Every such sum is bounded by
the sum of all live payment and expense amounts (times 60 for an hourly rate) or of all live
minutes. When ``60 * money < MAX_SAFE_INTEGER`` and ``minutes <= MAX_SAFE_INTEGER`` no overflow is
possible and the months are rebuilt incrementally; otherwise the maintainer runs the full recompute
itself (``MonthStore.full``), so the outcome is the full one by construction.

Writes: a month row only when its content changed, and ``os_workspaces.summaries`` =
``{coverage, calculated_version}`` on every apply, exactly as ``FullSummaries``. The runtime role
has no DELETE, so an emptied month is rewritten as ``{"rows": []}``, never deleted. Nothing here
logs an amount (I7).
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from app.services.os_ledger import api
from app.services.os_ledger.summaries import (
    MAX_SAFE_INTEGER,
    FullSummaries,
    SummaryInputError,
    _json,
    _month_start,
    build_summaries,
)
from app.services.os_ledger.tx import record_columns, wire
from app.services.os_ledger.validate_full import domain_error

__all__ = ["IncrementalSummaries", "MonthStore", "PgMonthStore", "affected_months"]

DATED = ("payments", "expenses", "time")  # the entities a monthly row sums, by their "date"


class MonthStore(Protocol):
    def magnitudes(self) -> tuple[int, int]:
        """(sum of live payment and expense amounts, sum of live minutes)."""

    def work_months(self, work_ids: Sequence[str]) -> set[date]:
        """Months holding a live payment or time entry of one of these works."""

    def records_in(self, months: Sequence[date]) -> dict[str, list[api.RecordDict]]:
        """Live payments, expenses and time entries dated in ``months``, each sorted by id."""

    def stored(self, months: Sequence[date]) -> dict[date, Mapping[str, Any]]:
        """The stored summary of each of ``months`` that has a row."""

    def write(self, month: date, content: Mapping[str, Any], version: int,
              now: datetime) -> None: ...

    def header(self, value: Mapping[str, Any]) -> None: ...

    def full(self, applied: Sequence[api.Mutation]) -> None:
        """The full recompute (the exact fallback)."""


def _next_month(month: date) -> date:
    return month + timedelta(days=calendar.monthrange(month.year, month.month)[1])


class PgMonthStore:
    """``MonthStore`` on the transaction's connection (the runtime role's grants, §18.2)."""

    def __init__(self, tx: Any) -> None:
        self._tx = tx

    def magnitudes(self) -> tuple[int, int]:
        row = self._tx._exec(
            "SELECT (SELECT COALESCE(sum(amount_minor), 0) FROM assaf_os.os_payments "
            "WHERE workspace_id = :ws AND deleted_at IS NULL) + (SELECT COALESCE("
            "sum(amount_minor), 0) FROM assaf_os.os_expenses WHERE workspace_id = :ws "
            "AND deleted_at IS NULL) AS money, (SELECT COALESCE(sum(minutes), 0) "
            "FROM assaf_os.os_time WHERE workspace_id = :ws AND deleted_at IS NULL) AS minutes"
        ).one()
        return int(row.money), int(row.minutes)

    def work_months(self, work_ids: Sequence[str]) -> set[date]:
        rows = self._tx._exec(
            "SELECT CAST(date_trunc('month', CAST(business_date AS timestamp)) AS date) AS month "
            "FROM assaf_os.os_payments WHERE workspace_id = :ws AND deleted_at IS NULL "
            "AND business_date IS NOT NULL AND work_id = ANY(:ids) UNION "
            "SELECT CAST(date_trunc('month', CAST(business_date AS timestamp)) AS date) "
            "FROM assaf_os.os_time WHERE workspace_id = :ws AND deleted_at IS NULL "
            "AND business_date IS NOT NULL AND work_id = ANY(:ids)",
            {"ids": sorted(set(work_ids))}).all()
        return {row.month for row in rows}

    def records_in(self, months: Sequence[date]) -> dict[str, list[api.RecordDict]]:
        months = sorted(set(months))
        result: dict[str, list[api.RecordDict]] = {}
        for entity in DATED:
            rows = self._tx._exec(
                f"SELECT {record_columns(entity)} FROM assaf_os.os_{entity} "
                "WHERE workspace_id = :ws AND deleted_at IS NULL AND business_date >= :lo "
                "AND business_date < :hi AND CAST(date_trunc('month', CAST(business_date AS "
                "timestamp)) AS date) = ANY(:months)",
                {"lo": months[0], "hi": _next_month(months[-1]), "months": months}).all()
            result[entity] = sorted((wire(entity, row) for row in rows),
                                    key=lambda record, e=entity: record[api.ID_KEYS[e]])
        return result

    def stored(self, months: Sequence[date]) -> dict[date, Mapping[str, Any]]:
        rows = self._tx._exec(
            "SELECT month_start, summary FROM assaf_os.os_summary_monthly "
            "WHERE workspace_id = :ws AND month_start = ANY(:months)",
            {"months": sorted(set(months))}).all()
        return {row.month_start: row.summary for row in rows}

    def write(self, month: date, content: Mapping[str, Any], version: int,
              now: datetime) -> None:
        self._tx._exec(  # the statement FullSummaries writes with (the §18.2 column grants)
            "INSERT INTO assaf_os.os_summary_monthly (workspace_id, month_start, version, "
            "summary, calculated_at) VALUES (:ws, :month, :version, CAST(:summary AS jsonb), "
            ":now) ON CONFLICT (workspace_id, month_start) DO UPDATE SET "
            "version = EXCLUDED.version, summary = EXCLUDED.summary, "
            "calculated_at = EXCLUDED.calculated_at",
            {"month": month, "version": version, "summary": _json(dict(content)), "now": now})

    def header(self, value: Mapping[str, Any]) -> None:
        self._tx._exec(
            "UPDATE assaf_os.os_workspaces SET summaries = CAST(:summaries AS jsonb) "
            "WHERE workspace_id = :ws", {"summaries": _json(dict(value))})

    def full(self, applied: Sequence[api.Mutation]) -> None:
        FullSummaries().on_apply(self._tx, applied)


def _month_of(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 7:
        return None
    try:
        return _month_start(value[:7])
    except SummaryInputError:
        return None


def affected_months(applied: Sequence[api.Mutation]) -> tuple[set[date], list[str]]:
    """The months the touched dated records sit in (before and after), and the touched works."""
    months: set[date] = set()
    works: list[str] = []
    for mutation in applied:
        if mutation.entity in DATED:
            for image in (mutation.before, mutation.after):
                month = _month_of(image.get("date")) if image else None
                if month is not None:
                    months.add(month)
        elif mutation.entity == "works":
            works.append(mutation.record_id)
    return months, works


class IncrementalSummaries:
    """``api.SummaryMaintainer``: rebuild only the affected months; the full one when unsafe."""

    def __init__(self, store: Callable[[Any], MonthStore] = PgMonthStore) -> None:
        self._store = store

    def on_apply(self, tx: Any, applied: Sequence[api.Mutation]) -> None:
        applied = tuple(applied)
        store = self._store(tx)
        money, minutes = store.magnitudes()
        if not (60 * money < MAX_SAFE_INTEGER and minutes <= MAX_SAFE_INTEGER):
            store.full(applied)  # an overflow is possible: decide it exactly as the full one
            return
        calculated_version = tx.version + 1
        months, works = affected_months(applied)
        if works:
            months |= store.work_months(sorted(set(works)))
        if months:
            ordered = sorted(months)
            records: dict[str, list[api.RecordDict]] = {entity: [] for entity in api.ENTITIES}
            records.update(store.records_in(ordered))
            work_ids = sorted({entry.get("work_id") for entry in records["time"]
                               if isinstance(entry.get("work_id"), str)})
            found = tx.get_many([api.RecordRef("works", work_id) for work_id in work_ids])
            records["works"] = [found[ref] for ref in sorted(found, key=lambda r: r.record_id)]
            try:
                summary = build_summaries(records)
                rebuilt: dict[date, list[dict]] = {}
                for row in summary["monthly"]:
                    rebuilt.setdefault(_month_start(row["month"]), []).append(row)
            except SummaryInputError as exc:
                raise domain_error(exc) from None
            stored = store.stored(ordered)
            for month in ordered:
                content = {"rows": rebuilt.get(month, [])}
                previous = stored.get(month)
                if previous == content or (previous is None and not content["rows"]):
                    continue
                store.write(month, content, calculated_version, tx.now)
        store.header({"coverage": "complete", "calculated_version": calculated_version})
