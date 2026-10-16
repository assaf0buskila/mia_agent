"""Mirror projection (§14): one consistent ledger snapshot rendered as the mirror workbook.

Tabs and headers are the OS workbook's own: the seven entity tabs with their schema labels
(``app/prototypes/assaf_os_export_schema.json``) plus the ``סיכום`` summary tab
(``ops/assaf-os/schema.js``). Money is stored in minor units and shown in major units, minutes are
shown as hours, and the client and work calculated columns are filled exactly as the Apps Script
fills them today.

Only those eight tabs and only schema columns are ever produced. Statement lines are never read,
and a record key that is not a schema column never reaches a cell, so no personal line or
personal detail can be mirrored (I11). Values are plain scalars written RAW by the transport.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from app.prototypes.assaf_os_summaries import SummaryInputError, build_summaries
from app.services.os_ledger import api

Cell = str | int | float | bool
Row = tuple[Cell, ...]

PAGE_LIMIT = 200
MAX_ROWS = 100_000  # across every entity; a larger snapshot is refused, never truncated
MAX_PAD_ROWS = MAX_ROWS + 1_000
MAX_PAD_COLS = 200
MAX_CELL_CHARS = 50_000
SNAPSHOT_ATTEMPTS = 3

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "prototypes" / "assaf_os_export_schema.json"
_EXPORT = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))["entities"]

# entity -> ((key, label, type, role), ...), in the workbook's column order.
_FIELDS: Mapping[str, tuple[tuple[str, str, str, str], ...]] = MappingProxyType({
    entity: tuple(
        (field["key"], field["label"], field["type"], field["role"])
        for field in _EXPORT[entity]["fields"]
    )
    for entity in api.ENTITIES
})
ENTITY_TABS: Mapping[str, str] = MappingProxyType(
    {entity: _EXPORT[entity]["name"] for entity in api.ENTITIES}
)
SUMMARY_TAB = "סיכום"
# ops/assaf-os/schema.js, tab "summary", in order.
_SUMMARY_FIELDS: tuple[tuple[str, str], ...] = (
    ("summary_scope", "סוג סיכום"),
    ("month", "חודש"),
    ("person_id", "מזהה אדם"),
    ("client_name", "לקוח"),
    ("path", "מסלול"),
    ("currency", "מטבע"),
    ("net_received_ils", "תקבולים נטו"),
    ("hours", "שעות מתועדות"),
    ("weighted_hourly_ils", "הכנסה משוקללת לשעה"),
    ("missing_time", "חסר דיווח זמן"),
    ("paid_expenses_ils", "הוצאות ששולמו בחודש"),
    ("net_cash_ils", "תזרים נטו בחודש"),
    ("coverage", "כיסוי נתונים"),
    ("calculated_at", "חושב בתאריך"),
)
MIRROR_TABS: tuple[str, ...] = (SUMMARY_TAB, *(ENTITY_TABS[e] for e in api.ENTITIES))
_BUSINESS_LABEL = "תזרים עסקי"


class ProjectionError(Exception):
    """The snapshot could not be projected. ``code`` is a reason code; no value is kept."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Snapshot:
    observed_version: int
    authority_epoch: int
    states: Mapping[str, str]
    records: Mapping[str, tuple[Mapping[str, Any], ...]]
    monthly: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class Projection:
    observed_version: int
    tabs: Mapping[str, tuple[Row, ...]]  # tab -> (header, *rows)


def headers(name: str) -> tuple[str, ...]:
    if name == "summary":
        return tuple(label for _, label in _SUMMARY_FIELDS)
    return tuple(label for _, label, _, _ in _FIELDS[name])


def label(entity: str, key: str) -> str:
    for field_key, field_label, _, _ in _FIELDS[entity]:
        if field_key == key:
            return field_label
    raise KeyError(key)


# ---------------------------------------------------------------------------------------------
# A1 ranges


def tab_range(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def tab_anchor(tab: str) -> str:
    return tab_range(tab) + "!A1"


def tab_of(a1: str) -> str:
    name = a1.split("!", 1)[0] if a1.endswith("!A1") else a1
    if len(name) < 2 or not (name.startswith("'") and name.endswith("'")):
        raise ProjectionError("range_invalid")
    return name[1:-1].replace("''", "'")


def read_ranges() -> list[str]:
    return [tab_range(tab) for tab in MIRROR_TABS]


# ---------------------------------------------------------------------------------------------
# Snapshot: every page and the monthly summary at one observed version, with no transaction
# held across reader calls (each reader call is its own short read).


def load_snapshot(reader: Any, ctx: Any) -> Snapshot:
    """Read every live record and ``monthly_summary`` at one version, or refuse.

    ``head`` brackets the reads: every page must carry the head's ``observed_version`` and the
    head must be unchanged after ``monthly_summary``. A moved ledger is re-read, up to
    ``SNAPSHOT_ATTEMPTS`` times, then ``snapshot_unstable``.
    """
    for _ in range(SNAPSHOT_ATTEMPTS):
        try:
            snapshot = _read_once(reader, ctx)
        except api.LedgerError as exc:
            if exc.code != "cursor_invalidated":
                raise
            continue
        if snapshot is not None:
            return snapshot
    raise ProjectionError("snapshot_unstable")


def _read_once(reader: Any, ctx: Any) -> Snapshot | None:
    head = reader.head(ctx)
    version = head.observed_version
    records: dict[str, tuple[Mapping[str, Any], ...]] = {}
    total = 0
    for entity in api.ENTITIES:
        rows: list[Mapping[str, Any]] = []
        after: str | None = None
        while True:
            page = reader.page(ctx, entity, after=after, limit=PAGE_LIMIT)
            if page.observed_version != version:
                return None
            if not page.records and page.next_cursor is not None:
                raise ProjectionError("reader_page_invalid")
            rows.extend(page.records)
            total += len(page.records)
            if total > MAX_ROWS:
                raise ProjectionError("mirror_too_large")
            if page.next_cursor is None:
                break
            after = page.next_cursor
        records[entity] = tuple(rows)
    monthly = tuple(reader.monthly_summary(ctx))
    if reader.head(ctx).observed_version != version:
        return None
    return Snapshot(
        observed_version=version,
        authority_epoch=head.authority_epoch,
        states=dict(head.states),
        records=records,
        monthly=monthly,
    )


# ---------------------------------------------------------------------------------------------
# Cells


def _major(minor: Any) -> Cell:
    if minor is None:
        return ""
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise ProjectionError("record_shape_invalid")
    return minor // 100 if minor % 100 == 0 else round(minor / 100, 2)


def _hours(minutes: Any) -> Cell:
    if minutes is None:
        return ""
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise ProjectionError("record_shape_invalid")
    return minutes // 60 if minutes % 60 == 0 else round(minutes / 60, 6)


def _flag(value: Any) -> Cell:
    if value is None:
        return ""
    if not isinstance(value, bool):
        raise ProjectionError("record_shape_invalid")
    return value


def _cell(kind: str, value: Any) -> Cell:
    if value is None:
        return ""
    if kind == "money_ils":
        return _major(value)
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProjectionError("record_shape_invalid")
        return value
    if kind == "boolean":
        return _flag(value)
    if kind == "decimal":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ProjectionError("record_shape_invalid")
        if not math.isfinite(value):
            raise ProjectionError("record_shape_invalid")
        return value
    if isinstance(value, datetime | date) and kind in {"date", "datetime"}:
        value = value.isoformat()
    if not isinstance(value, str):
        raise ProjectionError("record_shape_invalid")
    if len(value) > MAX_CELL_CHARS:
        raise ProjectionError("cell_too_long")
    return value


# ---------------------------------------------------------------------------------------------
# Projection


def _calculated(snapshot: Snapshot) -> dict[str, dict[str, Mapping[str, Any]]]:
    """Client and work calculated columns, from the same snapshot as the rows they sit on."""
    try:
        summaries = build_summaries({e: list(snapshot.records[e]) for e in api.ENTITIES})
    except SummaryInputError:
        raise ProjectionError("summary_input_invalid") from None
    clients = {row["person_id"]: row for row in summaries["clients"]}
    works = {row["work_id"]: row for row in summaries["works"]}
    return {"clients": clients, "works": works}


def _calculated_cell(entity: str, key: str, calc: Mapping[str, Any] | None) -> Cell:
    if calc is None:
        return ""
    if key in {"received_learning_ils", "received_automation_ils", "total_received_ils",
               "received_ils", "hourly_ils"}:
        return _major(calc.get(key))
    if key == "recorded_hours":
        return _hours(calc.get("recorded_minutes"))
    if key == "foreign_currency_flag":
        return _flag(calc.get(key))
    raise ProjectionError("calculated_column_unknown")


def _entity_rows(entity: str, snapshot: Snapshot, calculated: Mapping[str, Any]) -> list[Row]:
    id_key = api.ID_KEYS[entity]
    rows: list[Row] = [headers(entity)]
    for record in snapshot.records[entity]:
        if not isinstance(record, Mapping) or not isinstance(record.get(id_key), str):
            raise ProjectionError("record_shape_invalid")
        calc = calculated.get(entity, {}).get(record[id_key])
        row = tuple(
            _calculated_cell(entity, key, calc)
            if role == "calculated"
            else _cell(kind, record.get(key))
            for key, _, kind, role in _FIELDS[entity]
        )
        rows.append(row)
    return rows


def _summary_rows(snapshot: Snapshot) -> list[Row]:
    """The business rows exactly as ``assafOsProjectionRequests_`` writes them today.

    ``calculated_at`` stays blank: a time stamp would make an unchanged summary differ on every
    projection, and the mirror's own "verified" time is the honest one (I2).
    """
    rows: list[Row] = [headers("summary")]
    for month in snapshot.monthly:
        if not isinstance(month, Mapping):
            raise ProjectionError("summary_shape_invalid")
        reasons = month.get("reason_codes")
        if not isinstance(reasons, Sequence) or isinstance(reasons, str):
            raise ProjectionError("summary_shape_invalid")
        try:
            rows.append((
                "business",
                _cell("text", month.get("month")),
                "",
                _BUSINESS_LABEL,
                "",
                _cell("text", month.get("currency")),
                _major(month.get("paid_income_ils")),
                _hours(month.get("recorded_minutes")),
                _major(month.get("effective_hourly_ils")),
                "missing_time" in reasons,
                _major(month.get("paid_expenses_ils")),
                _major(month.get("net_cash_ils")),
                _cell("text", month.get("coverage")),
                "",
            ))
        except ProjectionError:
            raise ProjectionError("summary_shape_invalid") from None
    return rows


def project(snapshot: Snapshot) -> Projection:
    calculated = _calculated(snapshot)
    tabs: dict[str, tuple[Row, ...]] = {SUMMARY_TAB: tuple(_summary_rows(snapshot))}
    for entity in api.ENTITIES:
        tabs[ENTITY_TABS[entity]] = tuple(_entity_rows(entity, snapshot, calculated))
    return Projection(observed_version=snapshot.observed_version, tabs=tabs)


# ---------------------------------------------------------------------------------------------
# Write ranges, readback equality and extents


def _extent(coverage: Mapping[str, Any] | None, tab: str) -> tuple[int, int]:
    value = (coverage or {}).get(tab)
    if (
        isinstance(value, Sequence)
        and not isinstance(value, str)
        and len(value) == 2
        and all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in value)
    ):
        return min(value[0], MAX_PAD_ROWS), min(value[1], MAX_PAD_COLS)
    return 0, 0


def write_ranges(
    projection: Projection, coverage: Mapping[str, Any] | None
) -> list[tuple[str, list[list[Cell]]]]:
    """Every tab in full from A1, padded with blanks over the last known extent so rows and
    columns that no longer exist are cleared in the same request."""
    ranges = []
    for tab in MIRROR_TABS:
        rows = projection.tabs[tab]
        old_rows, old_cols = _extent(coverage, tab)
        width = max(max((len(row) for row in rows), default=0), old_cols)
        height = max(len(rows), old_rows)
        padded = [list(row) + [""] * (width - len(row)) for row in rows]
        padded.extend([[""] * width for _ in range(height - len(rows))])
        ranges.append((tab_anchor(tab), padded))
    return ranges


def _blank(value: Any) -> bool:
    return value is None or value == "" and isinstance(value, str)


def _normalized(rows: Sequence[Sequence[Any]]) -> list[list[Any]]:
    out = []
    for row in rows:
        cells = list(row)
        while cells and _blank(cells[-1]):
            cells.pop()
        out.append(cells)
    while out and not out[-1]:
        out.pop()
    return out


def _cell_equal(left: Any, right: Any) -> bool:
    if _blank(left) or _blank(right):
        return _blank(left) and _blank(right)
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-6)
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    return False


def readback_equal(projection: Projection, readback: Mapping[str, Sequence[Sequence[Any]]]) -> bool:
    """True only when every mirror tab reads back cell-for-cell equal to the projection."""
    for tab in MIRROR_TABS:
        values = readback.get(tab_range(tab))
        if values is None:
            return False
        expected, actual = _normalized(projection.tabs[tab]), _normalized(values)
        if len(expected) != len(actual):
            return False
        for want, got in zip(expected, actual, strict=True):
            if len(want) != len(got):
                return False
            if not all(_cell_equal(a, b) for a, b in zip(want, got, strict=True)):
                return False
    return True


def coverage_of(readback: Mapping[str, Sequence[Sequence[Any]]]) -> dict[str, list[int]]:
    """Per tab, the extent the sheet holds now: ``[rows, columns]``."""
    coverage = {}
    for tab in MIRROR_TABS:
        values = readback.get(tab_range(tab))
        if values is not None:
            coverage[tab] = [len(values), max((len(row) for row in values), default=0)]
    return coverage
