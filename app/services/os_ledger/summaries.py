"""Financial projections and the default full-recompute SummaryMaintainer (V2-16).

The projection code (everything down to ``build_summaries``) is a verbatim copy of
``app/prototypes/assaf_os_summaries.py``, which stays in place as V2-13's oracle and is never edited
(V2_PG_CONTRACTS.md section 3.1); ``tests/services/os_ledger/ported/`` runs the prototype's tests
against it. Money stays in integer minor units; the ``*_ils`` names match the Apps Script transport.

``FullSummaries`` is the default ``SummaryMaintainer`` of ``build_ledger()`` (section 8): inside
every ``apply`` it recomputes every month from the transaction's complete record state and writes
each month row of ``os_summary_monthly``. The runtime role has no DELETE, so a month that loses all
of its data is rewritten as an empty summary, never deleted. ``os_workspaces.summaries`` holds only
``{coverage, calculated_version}``. V2-18 replaces it with the incremental maintainer only after
its property test proves the two equal.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

MAX_SAFE_INTEGER = 9_007_199_254_740_991

ENTITY_ID_KEYS = {
    "leads": "lead_id",
    "clients": "person_id",
    "works": "work_id",
    "payments": "payment_id",
    "time": "time_id",
    "expenses": "expense_id",
    "tasks": "task_id",
}

PAYMENT_RECEIVED = "\u05d4\u05ea\u05e7\u05d1\u05dc"
PAYMENT_REFUND = "\u05d4\u05d7\u05d6\u05e8"
PAYMENT_CANCELLED = "\u05d1\u05d5\u05d8\u05dc"
TIME_ACTIVE = "\u05e4\u05e2\u05d9\u05dc"
TIME_CANCELLED = "\u05d1\u05d5\u05d8\u05dc"
EXPENSE_PAID = "\u05e9\u05d5\u05dc\u05dd"
EXPENSE_ACTUAL = "\u05ea\u05e9\u05dc\u05d5\u05dd \u05d1\u05e4\u05d5\u05e2\u05dc"
PATH_LEARNING = "\u05dc\u05d9\u05de\u05d5\u05d3"


class SummaryInputError(ValueError):
    """A canonical input violated an invariant needed for safe aggregation."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _checked_int(value: Any, code: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SummaryInputError(code)
    if value < minimum or value > MAX_SAFE_INTEGER:
        raise SummaryInputError(code)
    return value


def _safe_add(left: int, right: int) -> int:
    result = left + right
    if not -MAX_SAFE_INTEGER <= result <= MAX_SAFE_INTEGER:
        raise SummaryInputError("amount_overflow")
    return result


def _round_hourly(amount: int | None, minutes: int | None) -> int | None:
    if amount is None or minutes is None or minutes <= 0:
        return None
    # Math.round(amount * 60 / minutes), without binary-float loss.  This is
    # round-half-up (towards +infinity at an exact negative half), matching JS.
    numerator = amount * 60
    result = (2 * numerator + minutes) // (2 * minutes)
    if not -MAX_SAFE_INTEGER <= result <= MAX_SAFE_INTEGER:
        raise SummaryInputError("weighted_hourly_overflow")
    return result


def _month(value: Any) -> str:
    return str(value or "")[:7]


def _normalise_records(records: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, list[dict]]:
    if not isinstance(records, Mapping) or set(records) != set(ENTITY_ID_KEYS):
        raise SummaryInputError("invalid_records_shape")

    result: dict[str, list[dict]] = {}
    for entity, id_key in ENTITY_ID_KEYS.items():
        rows = records[entity]
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise SummaryInputError("invalid_records_shape")
        seen: set[str] = set()
        copied: list[dict] = []
        for source in rows:
            if not isinstance(source, Mapping):
                raise SummaryInputError("invalid_record")
            record_id = source.get(id_key)
            if not isinstance(record_id, str) or not record_id or record_id in seen:
                raise SummaryInputError("invalid_or_duplicate_record_id")
            _checked_int(source.get("revision"), "invalid_revision", minimum=1)
            seen.add(record_id)
            copied.append(dict(source))
        result[entity] = copied
    return result


def _normalise_coverage(coverage: Mapping[str, str] | None) -> dict[str, str]:
    result = dict.fromkeys(ENTITY_ID_KEYS, "complete")
    if coverage is not None:
        if not isinstance(coverage, Mapping) or set(coverage) - set(ENTITY_ID_KEYS):
            raise SummaryInputError("invalid_coverage_shape")
        result.update(coverage)
    if any(value not in {"complete", "partial", "unknown"} for value in result.values()):
        raise SummaryInputError("invalid_coverage")
    return result


def _validate_measures(records: Mapping[str, list[dict]]) -> None:
    # Validate every stored measure, including rows that do not contribute
    # because they are cancelled, unpaid, or estimated.  The primary's
    # checked-minor-unit guarantee must not depend on summary filtering.
    for payment in records["payments"]:
        _payment_amount(payment)
    for expense in records["expenses"]:
        _expense_amount(expense)
    for entry in records["time"]:
        _minutes(entry)


def _payment_amount(record: Mapping[str, Any]) -> int:
    return _checked_int(record.get("actual_amount_ils"), "invalid_payment_amount", minimum=1)


def _expense_amount(record: Mapping[str, Any]) -> int:
    return _checked_int(record.get("amount_ils"), "invalid_expense_amount", minimum=1)


def _minutes(record: Mapping[str, Any]) -> int:
    return _checked_int(record.get("minutes"), "invalid_minutes")


def _signed_payment(record: Mapping[str, Any]) -> int:
    amount = _payment_amount(record)
    return -amount if record.get("kind") == PAYMENT_REFUND else amount


def _paid_work_missing_time(records: Mapping[str, list[dict]], month: str, currency: str) -> bool:
    work_cash: dict[str, int] = {}
    for payment in records["payments"]:
        if (
            payment.get("kind") == PAYMENT_CANCELLED
            or _month(payment.get("date")) != month
            or payment.get("currency") != currency
        ):
            continue
        work_id = payment.get("work_id")
        work_cash[work_id] = _safe_add(work_cash.get(work_id, 0), _signed_payment(payment))

    work_by_id = {work["work_id"]: work for work in records["works"]}
    timed_works: set[str] = set()
    for entry in records["time"]:
        work = work_by_id.get(entry.get("work_id"))
        if (
            entry.get("status") != TIME_CANCELLED
            and work
            and _month(entry.get("date")) == month
            and work.get("currency") == currency
            and _minutes(entry) > 0
        ):
            timed_works.add(entry["work_id"])
    return any(amount != 0 and work_id not in timed_works for work_id, amount in work_cash.items())


def _monthly(records: Mapping[str, list[dict]], coverage: Mapping[str, str]) -> list[dict]:
    groups: dict[tuple[str, str], dict[str, int]] = {}

    def group(month: str, currency: Any) -> dict[str, int]:
        key = (month, str(currency))
        return groups.setdefault(key, {"income": 0, "expenses": 0, "minutes": 0})

    for payment in records["payments"]:
        target = group(_month(payment.get("date")), payment.get("currency"))
        if payment.get("kind") != PAYMENT_CANCELLED:
            target["income"] = _safe_add(target["income"], _signed_payment(payment))
    for expense in records["expenses"]:
        target = group(_month(expense.get("date")), expense.get("currency"))
        if (
            expense.get("payment_status") == EXPENSE_PAID
            and expense.get("source_type") == EXPENSE_ACTUAL
        ):
            target["expenses"] = _safe_add(target["expenses"], _expense_amount(expense))

    work_by_id = {work["work_id"]: work for work in records["works"]}
    for entry in records["time"]:
        work = work_by_id.get(entry.get("work_id"))
        currency = (work.get("currency") or "ILS") if work else "ILS"
        target = group(_month(entry.get("date")), currency)
        if entry.get("status") != TIME_CANCELLED:
            target["minutes"] = _safe_add(target["minutes"], _minutes(entry))

    result: list[dict] = []
    for month, currency in sorted(groups):
        values = groups[(month, currency)]
        income = values["income"] if coverage["payments"] == "complete" else None
        expenses = values["expenses"] if coverage["expenses"] == "complete" else None
        minutes = (
            values["minutes"]
            if coverage["time"] == "complete" and coverage["works"] == "complete"
            else None
        )
        reasons = []
        if income is None:
            reasons.append("payments_coverage_partial")
        if expenses is None:
            reasons.append("expenses_coverage_partial")
        missing_time = False
        if minutes is None:
            reasons.append("time_coverage_partial")
        else:
            missing_time = _paid_work_missing_time(records, month, currency)
            if missing_time or (income is not None and income != 0 and minutes == 0):
                reasons.append("missing_time")
        net = None if income is None or expenses is None else _safe_add(income, -expenses)
        hourly = None
        if income is not None and minutes is not None and minutes > 0 and not missing_time:
            hourly = _round_hourly(income, minutes)
        result.append(
            {
                "month": month,
                "currency": currency,
                "paid_income_ils": income,
                "paid_expenses_ils": expenses,
                "net_cash_ils": net,
                "recorded_minutes": minutes,
                "effective_hourly_ils": hourly,
                "coverage": "partial" if reasons else "complete",
                "reason_codes": reasons,
            }
        )
    return result


def _totals(monthly: Sequence[Mapping[str, Any]]) -> list[dict]:
    by_currency: dict[str, list[Mapping[str, Any]]] = {}
    for row in monthly:
        by_currency.setdefault(str(row["currency"]), []).append(row)

    result = []
    for currency in sorted(by_currency):
        rows = by_currency[currency]

        def total(field: str) -> int | None:
            if any(row[field] is None for row in rows):
                return None
            value = 0
            for row in rows:
                value = _safe_add(value, row[field])
            return value

        income = total("paid_income_ils")
        expenses = total("paid_expenses_ils")
        minutes = total("recorded_minutes")
        reasons = list(dict.fromkeys(code for row in rows for code in row["reason_codes"]))
        net = None if income is None or expenses is None else _safe_add(income, -expenses)
        hourly = None if "missing_time" in reasons else _round_hourly(income, minutes)
        result.append(
            {
                "currency": currency,
                "paid_income_ils": income,
                "paid_expenses_ils": expenses,
                "net_cash_ils": net,
                "recorded_minutes": minutes,
                "effective_hourly_ils": hourly,
                "coverage": "partial" if reasons else "complete",
                "reason_codes": reasons,
            }
        )
    return result


def _work_projections(records: Mapping[str, list[dict]], coverage: Mapping[str, str]) -> list[dict]:
    payments_complete = coverage["payments"] == "complete"
    time_complete = coverage["time"] == "complete"
    works_complete = coverage["works"] == "complete"
    result = []
    for work in records["works"]:
        payments = [
            row
            for row in records["payments"]
            if row.get("work_id") == work["work_id"] and row.get("kind") != PAYMENT_CANCELLED
        ]
        entries = [
            row
            for row in records["time"]
            if row.get("work_id") == work["work_id"] and row.get("status") != TIME_CANCELLED
        ]
        minutes = None
        if time_complete and works_complete:
            minutes = 0
            for entry in entries:
                minutes = _safe_add(minutes, _minutes(entry))
        received = None
        if payments_complete and works_complete and work.get("currency") == "ILS":
            received = 0
            for payment in payments:
                if payment.get("currency") == "ILS":
                    received = _safe_add(received, _signed_payment(payment))
        foreign = None
        if payments_complete and works_complete:
            foreign = any(payment.get("currency") != "ILS" for payment in payments)
        reasons = []
        if not works_complete:
            reasons.append("works_coverage_partial")
        if not payments_complete:
            reasons.append("payments_coverage_partial")
        if not time_complete:
            reasons.append("time_coverage_partial")
        missing_time = received is not None and received != 0 and minutes == 0
        if missing_time:
            reasons.append("missing_time")
        result.append(
            {
                "work_id": work["work_id"],
                "person_id": work.get("person_id"),
                "path": work.get("path"),
                "currency": work.get("currency"),
                "received_ils": received,
                "recorded_minutes": minutes,
                "hourly_ils": _round_hourly(received, minutes),
                "foreign_currency_flag": foreign,
                "coverage": "partial" if reasons else "complete",
                "reason_codes": reasons,
            }
        )
    return result


def _client_projections(
    records: Mapping[str, list[dict]],
    coverage: Mapping[str, str],
    works: Sequence[Mapping[str, Any]],
) -> list[dict]:
    clients_complete = coverage["clients"] == "complete"
    works_complete = coverage["works"] == "complete"
    payments_complete = coverage["payments"] == "complete"
    time_complete = coverage["time"] == "complete"
    result = []
    for client in records["clients"]:
        owned = [
            work
            for work in works
            if work.get("person_id") == client["person_id"] and work.get("currency") == "ILS"
        ]
        money_known = clients_complete and works_complete and payments_complete
        time_known = clients_complete and works_complete and time_complete
        learning = automation = total_received = None
        if money_known:
            learning = 0
            automation = 0
            for work in owned:
                target = "learning" if work.get("path") == PATH_LEARNING else "automation"
                if target == "learning":
                    learning = _safe_add(learning, work["received_ils"])
                else:
                    automation = _safe_add(automation, work["received_ils"])
            total_received = _safe_add(learning, automation)
        minutes = None
        if time_known:
            minutes = 0
            for work in owned:
                minutes = _safe_add(minutes, work["recorded_minutes"])
        missing_time = (
            money_known
            and time_known
            and any(work["received_ils"] and not work["recorded_minutes"] for work in owned)
        )
        foreign = None
        if clients_complete and payments_complete:
            foreign = any(
                payment.get("person_id") == client["person_id"]
                and payment.get("currency") != "ILS"
                and payment.get("kind") != PAYMENT_CANCELLED
                for payment in records["payments"]
            )
        reasons = []
        if not clients_complete:
            reasons.append("clients_coverage_partial")
        if not works_complete:
            reasons.append("works_coverage_partial")
        if not payments_complete:
            reasons.append("payments_coverage_partial")
        if not time_complete:
            reasons.append("time_coverage_partial")
        if missing_time:
            reasons.append("missing_time")
        result.append(
            {
                "person_id": client["person_id"],
                "client_name": client.get("name"),
                "received_learning_ils": learning,
                "received_automation_ils": automation,
                "total_received_ils": total_received,
                "recorded_minutes": minutes,
                "hourly_ils": None if missing_time else _round_hourly(total_received, minutes),
                "foreign_currency_flag": foreign,
                "coverage": "partial" if reasons else "complete",
                "reason_codes": reasons,
            }
        )
    return result


def build_summaries(
    records: Mapping[str, Sequence[Mapping[str, Any]]],
    coverage: Mapping[str, str] | None = None,
    aggregate_coverage: str = "complete",
) -> dict[str, Any]:
    """Build canonical monthly data plus deterministic total/work/client projections."""

    if aggregate_coverage not in {"complete", "partial", "unknown"}:
        raise SummaryInputError("invalid_aggregate_coverage")
    canonical = _normalise_records(records)
    _validate_measures(canonical)
    entity_coverage = _normalise_coverage(coverage)
    monthly = _monthly(canonical, entity_coverage)
    works = _work_projections(canonical, entity_coverage)
    return {
        "coverage": aggregate_coverage,
        "monthly": monthly,
        "totals": _totals(monthly),
        "works": works,
        "clients": _client_projections(canonical, entity_coverage, works),
    }


# ---- the default SummaryMaintainer (V2-16; not part of the prototype copy) --------------------


def _month_start(month: str) -> date:
    """``"YYYY-MM"`` (the projection's month key) as the first day of that month."""
    if not isinstance(month, str) or len(month) != 7 or month[4] != "-":
        raise SummaryInputError("invalid_month")
    try:
        return date(int(month[:4]), int(month[5:]), 1)
    except ValueError:
        raise SummaryInputError("invalid_month") from None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class FullSummaries:
    """``api.SummaryMaintainer``: a full ``build_summaries`` recompute inside every ``apply``.

    Each month row holds ``{"rows": [<v1 monthly rows of that month, by currency>]}``; a month
    whose data is gone keeps ``{"rows": []}``. Only rows whose content changed are written, each
    with ``version`` = the commit version this apply belongs to.
    """

    def on_apply(self, tx: Any, applied: Sequence[Any]) -> None:
        from app.services.os_ledger.validate_full import domain_error

        try:
            summary = build_summaries(tx.snapshot_records())
            months: dict[date, list[dict]] = {}
            for row in summary["monthly"]:
                months.setdefault(_month_start(row["month"]), []).append(row)
        except SummaryInputError as exc:
            raise domain_error(exc) from None
        calculated_version = tx.version + 1
        stored = {
            row.month_start: row.summary for row in tx._exec(
                "SELECT month_start, summary FROM assaf_os.os_summary_monthly "
                "WHERE workspace_id = :ws").all()
        }
        for month in sorted(set(stored) | set(months)):
            content = {"rows": months.get(month, [])}
            if stored.get(month) == content:
                continue
            tx._exec(
                "INSERT INTO assaf_os.os_summary_monthly (workspace_id, month_start, version, "
                "summary, calculated_at) VALUES (:ws, :month, :version, CAST(:summary AS jsonb), "
                ":now) ON CONFLICT (workspace_id, month_start) DO UPDATE SET "
                "version = EXCLUDED.version, summary = EXCLUDED.summary, "
                "calculated_at = EXCLUDED.calculated_at",
                {"month": month, "version": calculated_version, "summary": _json(content),
                 "now": tx.now})
        tx._exec(
            "UPDATE assaf_os.os_workspaces SET summaries = CAST(:summaries AS jsonb) "
            "WHERE workspace_id = :ws",
            {"summaries": _json({"coverage": summary["coverage"],
                                 "calculated_version": calculated_version})})


def monthly_rows(stored: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """The v1 monthly rows of ``os_summary_monthly`` summaries, in month order."""
    return tuple(dict(row) for summary in stored for row in summary.get("rows", ()))

