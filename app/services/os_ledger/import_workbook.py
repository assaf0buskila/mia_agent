"""Workbook staging for the Sheets-to-PostgreSQL import (contract section 15, erratum E1-rev).

``prepare`` is pure: it maps the physical rows of a local ``.xlsx`` through the prototype's
normalization helpers (the V2-13 oracle, imported read-only) into staged rows, quarantining every
unaccepted, id-less, drifted or duplicated row and never dropping one. ``manifest`` is the
deterministic review artifact the owner signs. ``stage`` and ``persist_staged_batch`` write the
batch and its rows through the frozen ledger API in one ``kind="import"`` transaction.

A file carries no Apps Script reader evidence, so its binding records only its own byte digest, the
workspace and the requested epoch. It never invents a Google workbook id, a source epoch, a capture
time or a human approval. Acceptance comes from the workbook's native accepted journal, and that is
not owner approval either: the signed manifest digest is.

Typed date cells (a date-formatted number, a ``t="d"`` cell) are read by the field they sit in
(erratum E27, ``xlsx_dates``): a ``date`` column becomes ``YYYY-MM-DD`` and a ``datetime``
column (``created_at``, ``updated_at``, the journal's ``accepted_at``) the UTC instant of its
Asia/Jerusalem wall-clock time. A date anywhere else, or an unsafe one, quarantines the row.
A string keeps its path.

Nothing here logs a cell value, a name, an amount or an id. Log lines are reason codes and counts.
"""

from __future__ import annotations

import decimal
import json
import logging
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid5

from app.prototypes import assaf_os_export_provenance as physical
from app.prototypes.assaf_os_domain import SCHEMA, _canonical_record, validate_state
from app.prototypes.assaf_os_primary import LocalPrimaryError
from app.services.os_ledger import api, xlsx_dates
from app.services.os_ledger.xlsx import Cell, Sheet, Workbook

_LOG = logging.getLogger("mia.os_import")
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,63}")
_HEX = re.compile(r"[0-9a-f]{64}")
OWNER_EXPORT = "owner_export:"
IMPORT_JOB_SCOPES = frozenset({"import"})  # what ``authorize.for_worker("import", ...)`` mints
# The accepted journal's one time column. Entity tabs say which of their columns are dates or
# datetimes in the physical descriptor (``NATIVE_FIELDS[entity][i]["type"]``).
_JOURNAL_KINDS = {"accepted_at": xlsx_dates.DATETIME}
_ROW_CHUNK = 500


class WorkbookImportError(ValueError):
    """Fixed reason codes only, suitable for the local CLI."""


@dataclass(frozen=True)
class StagedRow:
    row_number: int  # batch-wide ordinal for os_import_rows' primary key
    entity: str
    record_id: str | None = field(repr=False)  # None unless the id is well formed
    source_digest: str = field(repr=False)
    status: str
    raw_record: dict[str, Any] = field(repr=False)
    canonical_record: dict[str, Any] | None = field(repr=False)
    provenance: dict[str, Any] = field(repr=False)
    quarantine: tuple[str, ...]


@dataclass(frozen=True)
class StagedWorkbook:
    batch_id: str = field(repr=False)
    source_id: str = field(repr=False)
    source_digest: str = field(repr=False)
    source_binding: dict[str, Any] = field(repr=False)
    rows: tuple[StagedRow, ...] = field(repr=False)
    issues: tuple[str, ...]


@dataclass(frozen=True)
class StageResult:
    """What ``stage`` persisted. Codes and counts only; ``batch`` is the in-memory staging."""

    batch: StagedWorkbook = field(repr=False)
    status: str  # validated | quarantined | failed
    manifest_digest: str
    rows: int
    quarantined: int
    reasons: Mapping[str, int]


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _rows(sheet: Sheet) -> dict[int, dict[int, Cell]]:
    """Sparse rows: never allocate from a worksheet's claimed dimensions."""
    result: dict[int, dict[int, Cell]] = {}
    for ref, cell in sheet.cells.items():
        match = re.fullmatch(r"([A-Z]+)([0-9]+)", ref)
        if match is None:
            raise WorkbookImportError("invalid_cell_reference")
        column = 0
        for char in match[1]:
            column = 26 * column + ord(char) - 64
        result.setdefault(int(match[2]), {})[column] = cell
    return result


def _date(cell: Cell, kind: str | None, date_1904: bool) -> str:
    """A typed date cell, read by what its field is (erratum E27, ``xlsx_dates``).

    Only a ``date`` or ``datetime`` field has a binding. A date in any other field has none, so the
    row is quarantined rather than guessed at.
    """
    if kind not in {xlsx_dates.DATE_ONLY, xlsx_dates.DATETIME}:
        raise WorkbookImportError("date_serial_binding_unspecified")
    try:
        if cell.kind == "iso_date":
            return xlsx_dates.from_iso(cell.value, kind)
        return xlsx_dates.from_serial(cell.value, kind, date_1904=date_1904)
    except xlsx_dates.DateValueError:
        raise WorkbookImportError(xlsx_dates.REASON) from None


def _number(text: str) -> Any:
    number = Decimal(text)
    # Comparisons are exact; ``abs`` and arithmetic round in the context and overflow on
    # 1E+999999999.
    if not -physical.MAX_SAFE_INTEGER <= number <= physical.MAX_SAFE_INTEGER:
        raise WorkbookImportError("unsafe_numeric_value")
    if number == number.to_integral_value():
        return int(number)
    result = float(number)
    if Decimal(str(result)) != number:
        raise WorkbookImportError("unsafe_numeric_value")
    return result


def _value(cell: Cell | None, kind: str | None = None, *, date_1904: bool = False) -> Any:
    """One cell's native raw value. ``kind`` is the field's type in the physical descriptor."""
    if cell is None or cell.value is None:
        return ""
    if cell.date_format or cell.kind == "iso_date":
        return _date(cell, kind, date_1904)
    if cell.kind == "error":
        raise WorkbookImportError("cell_error")
    if cell.kind != "number":
        return cell.value
    try:
        return _number(cell.value)
    except (decimal.DecimalException, ArithmeticError):
        # An exponent beyond the decimal context (Overflow, InvalidOperation) is a number the
        # importer will not carry: a quarantine reason, never a crash.
        raise WorkbookImportError("unsafe_numeric_value") from None


def _journal(tables, issues, date_1904=False):
    history: dict[tuple[str, str], list[dict]] = {}
    invalid: set[tuple[str, str]] = set()
    for _, rows in tables:
        for number, cells in sorted(rows.items()):
            if number == 1 or not any(
                c.value is not None and c.value != "" for c in cells.values()
            ):
                continue
            try:
                if any(c.formula is not None for c in cells.values()):
                    raise WorkbookImportError("formula_in_journal")
                # A blank cell right of the journal's columns is formatting, not data.
                if any(col > len(physical.ACCEPTED_HEADERS) and c.value not in (None, "")
                       for col, c in cells.items()):
                    raise WorkbookImportError("invalid_journal_row")
                row = {key: _value(cells.get(index), _JOURNAL_KINDS.get(key),
                                   date_1904=date_1904)
                       for index, key in enumerate(physical.ACCEPTED_HEADERS, 1)}
                entity, identity, revision = row["entity"], row["record_id"], row["revision"]
                if (entity not in api.ENTITIES or not isinstance(identity, str)
                        or not _ID.fullmatch(identity)
                        or not physical._safe_int(revision, positive=True)):
                    raise WorkbookImportError("invalid_journal_row")
                key = (entity, identity)
                snapshot = physical._json_cell(row["snapshot_json"])
                if (not isinstance(snapshot, dict)
                        or set(snapshot) != {"entity", "record_id", "revision", "input"}
                        or snapshot["entity"] != entity or snapshot["record_id"] != identity
                        or type(snapshot["revision"]) is not int or snapshot["revision"] != revision
                        or not isinstance(snapshot["input"], dict)
                        or set(snapshot["input"]) != {f["key"] for f in SCHEMA["entities"][entity]}
                        or physical.canonical_input(entity, snapshot["input"]) != snapshot["input"]
                        or api.digest(snapshot["input"]) != row["input_fingerprint"]
                        or not isinstance(row["raw_fingerprint"], str)
                        or not _HEX.fullmatch(row["raw_fingerprint"])
                        or physical._parse_utc(row["accepted_at"]) is None
                        or not isinstance(row["source"], str) or not 1 <= len(row["source"]) <= 64
                        or not isinstance(row["request_id"], str)
                        or not 1 <= len(row["request_id"]) <= 256):
                    invalid.add(key)
                    raise WorkbookImportError("invalid_journal_acceptance")
                history.setdefault(key, []).append({**row, "snapshot": snapshot})
            except (WorkbookImportError, LocalPrimaryError, ValueError, TypeError):
                issues.append("invalid_journal_acceptance")
    accepted = {}
    for key, entries in history.items():
        revisions = sorted(entry["revision"] for entry in entries)
        if revisions != list(range(1, len(revisions) + 1)):
            invalid.add(key)
            issues.append("accepted_history_gap_or_duplicate")
        if key not in invalid:
            accepted[key] = max(entries, key=lambda entry: entry["revision"])
    return accepted


def prepare(workbook: Workbook, *, workspace_id: str, authority_epoch: int) -> StagedWorkbook:
    """Prepare all physical rows in memory. Never persists or implies approval.

    Only exact physical headers identify business tabs. Unknown nonempty tabs and
    missing entity tabs are batch quarantine issues; known native audit/metadata
    tabs may be present but confer no reader evidence or human approval.
    """
    if (not isinstance(workspace_id, str) or not 1 <= len(workspace_id) <= 128
            or type(authority_epoch) is not int or authority_epoch < 1
            or not _HEX.fullmatch(workbook.source_digest)):
        raise WorkbookImportError("invalid_local_binding")
    issues: list[str] = []
    entity_tables: dict[str, list] = {entity: [] for entity in api.ENTITIES}
    journal_tables = []
    auxiliary = {physical.PROPOSAL_HEADERS, physical.RECEIPT_HEADERS,
                 physical.COVERAGE_HEADERS, physical.HEADER_HISTORY_HEADERS,
                 physical.SOURCE_METADATA_HEADERS}
    for sheet in workbook.sheets:
        rows = _rows(sheet)
        header = rows.get(1, {})
        # Blank trailing header cells (formatted but empty columns) are not part of the header.
        width = max((index for index, cell in header.items() if cell.value not in (None, "")),
                    default=0)
        headers = tuple(header.get(index, Cell(None)).value or ""
                        for index in range(1, width + 1))
        if any(cell.formula is not None for cell in header.values()):
            issues.append("formula_in_headers")
        entity = next((entity for entity in api.ENTITIES
                       if headers == physical.NATIVE_ENTITY_HEADERS[entity]), None)
        if entity:
            entity_tables[entity].append((sheet, rows))
        elif headers == physical.ACCEPTED_HEADERS:
            journal_tables.append((sheet, rows))
        elif headers not in auxiliary and any(
            c.value is not None and c.value != "" for c in sheet.cells.values()
        ):
            issues.append("unsupported_sheet_headers")
    if not journal_tables:
        issues.append("accepted_journal_missing")
    elif len(journal_tables) != 1:
        issues.append("accepted_journal_ambiguous")
    for tables in entity_tables.values():
        if not tables:
            issues.append("entity_sheet_missing")
        elif len(tables) > 1:
            issues.append("entity_sheet_ambiguous")
    accepted = _journal(journal_tables, issues, workbook.date_1904)
    pending = []
    for entity in api.ENTITIES:
        for sheet, rows in entity_tables[entity]:
            for row_number, cells in sorted(rows.items()):
                if row_number == 1 or not any(
                    c.value is not None and c.value != "" or c.formula is not None
                    for c in cells.values()
                ):
                    continue
                reasons = []
                values = []
                for index in range(1, len(physical.NATIVE_FIELDS[entity]) + 1):
                    cell = cells.get(index)
                    try:
                        values.append(_value(
                            cell, physical.NATIVE_FIELDS[entity][index - 1]["type"],
                            date_1904=workbook.date_1904,
                        ))
                    except WorkbookImportError as exc:
                        values.append(cell.value if cell else "")
                        reasons.append(str(exc))
                    if (cell and cell.formula is not None
                            and physical.NATIVE_FIELDS[entity][index - 1]["role"] != "calculated"):
                        reasons.append("formula_in_authoritative_field")
                if any(col > len(values) and (c.value not in (None, "") or c.formula is not None)
                       for col, c in cells.items()):
                    reasons.append("unexpected_row_columns")
                raw = physical._native_keyed_row(entity, values)
                identity = raw.get(api.ID_KEYS[entity])
                # Only a well-formed id is kept as the row's record id: it is bounded (<= 64) and
                # safe to carry into os_import_rows, the manifest and logs-by-count.
                if not isinstance(identity, str) or not _ID.fullmatch(identity):
                    identity = None
                    reasons.append("missing_or_invalid_record_id")
                pending.append((entity, sheet, row_number, cells, values, raw, identity, reasons))
    occurrences = Counter((item[0], item[6]) for item in pending if item[6])
    reference_ids = {
        entity: frozenset(identity for (kind, identity) in occurrences if kind == entity)
        for entity in api.ENTITIES
    }
    staged = []
    seen = set()
    for ordinal, (entity, sheet, number, cells, values, raw, identity, reasons) in enumerate(
        pending, 1
    ):
        key = (entity, identity)
        seen.add(key)
        if identity is not None and occurrences[key] > 1:
            reasons.append("duplicate_record_id")
        record = None
        try:
            record = physical._normalise_record(
                entity, raw, "native_major_decimal", reference_ids=reference_ids,
            )
            _canonical_record(entity, record)
        except LocalPrimaryError:
            # An invalid record is never kept as a canonical one, even on a quarantined row.
            record = None
            reasons.append("record_invalid")
        acceptance = accepted.get(key)
        if acceptance is None:
            reasons.append("unaccepted_record")
        elif record is not None:
            if acceptance["revision"] != record.get("revision"):
                reasons.append("accepted_revision_changed")
            if acceptance["input_fingerprint"] != physical.record_input_digest(entity, record):
                reasons.append("accepted_input_changed")
            try:
                raw_digest = physical.native_raw_owned_digest(entity, values, reference_ids)
                if acceptance["raw_fingerprint"] != raw_digest:
                    reasons.append("accepted_raw_changed")
            except LocalPrimaryError:
                reasons.append("accepted_raw_unresolved")
        raw_cells = {str(col): {"value": c.value, "kind": c.kind,
                               "date_format": c.date_format, "formula": c.formula}
                     for col, c in sorted(cells.items())}
        source_digest = api.digest({"file": workbook.source_digest, "sheet": sheet.name,
                                    "row": number, "cells": raw_cells})
        staged.append(StagedRow(
            ordinal, entity, identity, source_digest, "quarantined" if reasons else "staged",
            {"fields": raw, "cells": raw_cells}, record,
            {"sheet": sheet.name, "sheet_state": sheet.state, "source_row_number": number,
             "native_acceptance": acceptance, "native_acceptance_is_human_approval": False},
            tuple(sorted(set(reasons))),
        ))
    if set(accepted) - seen:
        issues.append("orphan_native_acceptance")
    canonical = {
        entity: [r.canonical_record for r in staged if r.entity == entity and r.status == "staged"]
        for entity in api.ENTITIES
    }
    try:
        validate_state(canonical)
    except LocalPrimaryError:
        issues.append("state_validation_failed")
    binding = {"kind": OWNER_EXPORT, "source_digest": workbook.source_digest,
               "workspace_id": workspace_id, "authority_epoch": authority_epoch}
    batch_id = str(uuid5(api.REQUEST_NS, "stage:" + api.digest(binding)))
    return StagedWorkbook(batch_id, OWNER_EXPORT + workbook.source_digest,
                          workbook.source_digest, binding, tuple(staged), tuple(sorted(issues)))


def _cancellation_keys(cancellations) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for item in cancellations:
        if (not isinstance(item, dict) or set(item) != {"entity", "record_id"}
                or item["entity"] not in api.ENTITIES or not isinstance(item["record_id"], str)
                or not _ID.fullmatch(item["record_id"])):
            raise WorkbookImportError("invalid_cancellation")
        key = (item["entity"], item["record_id"])
        if key in keys:
            raise WorkbookImportError("duplicate_cancellation")
        keys.add(key)
    return keys


def staged_records(batch: StagedWorkbook) -> dict[str, list[dict[str, Any]]]:
    """The valid, accepted canonical records per entity, sorted by id."""
    return {
        entity: sorted((row.canonical_record for row in batch.rows
                        if row.entity == entity and row.status == "staged"),
                       key=lambda record: record[api.ID_KEYS[entity]])
        for entity in api.ENTITIES
    }


def quarantine_count(batch: StagedWorkbook) -> int:
    """Quarantined rows plus batch-level issues; promotion needs zero."""
    return sum(row.status == "quarantined" for row in batch.rows) + len(batch.issues)


def manifest(batch: StagedWorkbook, *, cancellations=()) -> dict[str, Any]:
    """Deterministic review artifact, not a signed manifest or promotion receipt.

    Counts cover every staged row. Totals, ids and input digests cover only valid,
    accepted rows; quarantine counts and source digests bind excluded candidates.
    Missing time stays null even when an exported time tab is empty.
    """
    cancellation_keys = _cancellation_keys(cancellations)
    records = staged_records(batch)
    if any(record[api.ID_KEYS[entity]] == identity for entity, identity in cancellation_keys
           for record in records[entity]):
        raise WorkbookImportError("cancellation_conflicts_with_record")
    money, refunds = physical._money(records)
    times = []
    time_complete = not batch.issues and not any(
        row.status == "quarantined" and row.entity in {"time", "works", "payments"}
        for row in batch.rows
    )
    for work in records["works"]:
        entries = [row for row in records["time"] if row["work_id"] == work["work_id"]
                   and row["status"] != "בוטל"]
        minutes = sum(row["minutes"] for row in entries) if entries and time_complete else None
        net = sum((-1 if row["kind"] == "החזר" else 1) * row["actual_amount_ils"]
                  for row in records["payments"] if row["work_id"] == work["work_id"]
                  and row["kind"] != "בוטל" and row["currency"] == work["currency"])
        hourly = (2 * net * 60 + minutes) // (2 * minutes) if minutes and time_complete else None
        times.append({"work_id": work["work_id"], "currency": work["currency"],
                      "recorded_minutes": minutes, "hourly_minor": hourly})
    return _copy({
        "batch_id": batch.batch_id, "source_id": batch.source_id,
        "source_digest": batch.source_digest, "source_binding": batch.source_binding,
        "counts": {entity: sum(r.entity == entity for r in batch.rows) for entity in api.ENTITIES},
        "accepted_counts": {entity: len(records[entity]) for entity in api.ENTITIES},
        "ids": {entity: [r[api.ID_KEYS[entity]] for r in records[entity]]
                for entity in api.ENTITIES},
        "revisions": {entity: {r[api.ID_KEYS[entity]]: r["revision"] for r in records[entity]}
                      for entity in api.ENTITIES},
        "input_digests": {entity: {r[api.ID_KEYS[entity]]: physical.record_input_digest(entity, r)
                                    for r in records[entity]} for entity in api.ENTITIES},
        "rows": [{"row_number": row.row_number, "entity": row.entity, "record_id": row.record_id,
                  "source_digest": row.source_digest, "status": row.status,
                  "quarantine": list(row.quarantine)} for row in batch.rows],
        "quarantine_count": quarantine_count(batch),
        "issues": list(batch.issues), "money_by_currency": money, "refunds": refunds,
        "references": physical._references(records), "time_by_work": times,
        "cancellations": [{"entity": entity, "record_id": identity}
                          for entity, identity in sorted(cancellation_keys)],
    })


def manifest_digest(batch: StagedWorkbook, *, cancellations=()) -> str:
    return api.digest(manifest(batch, cancellations=cancellations))


def quarantine_reasons(batch: StagedWorkbook) -> dict[str, int]:
    """Reason code to row count (a batch-level issue counts once per occurrence)."""
    counts: Counter[str] = Counter()
    for row in batch.rows:
        counts.update(row.quarantine)
    counts.update(batch.issues)
    return dict(sorted(counts.items()))


def import_rows(batch: StagedWorkbook) -> list[api.ImportRow]:
    """The ``os_import_rows`` inserts for a batch: every row, quarantined ones included."""
    return [
        api.ImportRow(
            row_number=row.row_number, entity=row.entity, record_id=row.record_id,
            source_digest=row.source_digest, status=row.status, raw_record=row.raw_record,
            canonical_record=row.canonical_record, provenance=row.provenance,
            quarantine={"reasons": list(row.quarantine)} if row.status == "quarantined" else None,
        )
        for row in batch.rows
    ]


def _require_worker(ctx_worker: api.TrustedOwnerContext) -> None:
    """The WORKER channel, minted for the import job and for nothing else.

    ``authorize.for_worker("import", ...)`` binds ``scopes == frozenset({"import"})``; a worker
    minted for another job (retention, mirror, ...) must not stage or promote an import.
    """
    if ctx_worker.channel != "WORKER":
        raise api.LedgerError("channel_operation_denied", reason="import_requires_worker")
    if ctx_worker.scopes != IMPORT_JOB_SCOPES:
        raise api.LedgerError("scope_denied", reason="worker_job_denied")


def require_batch_binding(ctx_worker: api.TrustedOwnerContext, batch: StagedWorkbook) -> None:
    """The staged batch must have been prepared for this context's workspace and epoch."""
    binding = batch.source_binding
    if (binding.get("workspace_id") != ctx_worker.workspace
            or binding.get("authority_epoch") != ctx_worker.authority_epoch):
        raise api.LedgerError("authority_changed", reason="import_binding_mismatch")


def persist_staged_batch(
    ledger: api.LedgerStore, ctx_worker: api.TrustedOwnerContext, batch: StagedWorkbook
) -> StageResult:
    """Write the batch and its rows in one import transaction and settle the batch status.

    ``validated`` when no row is quarantined and there is no batch-level issue; ``quarantined``
    (terminal) when any row is; ``failed`` for a structurally broken workbook with no quarantined
    row (a missing tab, say). A quarantined or failed batch can never be promoted: the owner fixes
    the source and stages again, which is a new batch because the unique key is
    ``(workspace, epoch, source digest)``.
    """
    _require_worker(ctx_worker)
    require_batch_binding(ctx_worker, batch)
    rows = import_rows(batch)
    quarantined = sum(row.status == "quarantined" for row in rows)
    reasons = quarantine_reasons(batch)
    digest = manifest_digest(batch)
    status = "quarantined" if quarantined else ("failed" if batch.issues else "validated")
    provenance = {
        "importer": "os_workbook", "source_binding": batch.source_binding,
        "rows": len(rows), "quarantined": quarantined, "issues": list(batch.issues),
        "native_acceptance_is_human_approval": False,
    }
    with ledger.transaction(ctx_worker, domain="core", kind="import") as tx:
        tx.insert_import_batch(
            batch_id=batch.batch_id, authority_epoch=ctx_worker.authority_epoch,
            source_digest=batch.source_digest, source_binding=batch.source_id,
            provenance=provenance,
        )
        for start in range(0, len(rows), _ROW_CHUNK):
            tx.insert_import_rows(batch.batch_id, rows[start:start + _ROW_CHUNK])
        if status == "quarantined":
            tx.set_import_status(batch.batch_id, "quarantined", quarantine={
                "rows": quarantined, "reasons": reasons, "issues": list(batch.issues)})
        else:
            tx.set_import_status(batch.batch_id, status)
    _LOG.info(
        "os_import op=stage outcome=%s reason=persisted rows=%d quarantined=%d issues=%d",
        "ok" if status == "validated" else status, len(rows), quarantined, len(batch.issues),
    )
    return StageResult(batch, status, digest, len(rows), quarantined, reasons)


def stage(
    ledger: api.LedgerStore, ctx_worker: api.TrustedOwnerContext, workbook: Workbook,
    source_binding: str | None = None,
) -> StageResult:
    """Map ``workbook`` into ``os_import_*`` for ``ctx_worker``'s workspace and epoch.

    ``source_binding`` is optional because a file's binding is derived from the file itself
    (``owner_export:`` + the sha256 of its bytes). A value that disagrees with that is refused.
    """
    _require_worker(ctx_worker)
    if source_binding is not None and source_binding != OWNER_EXPORT + workbook.source_digest:
        raise api.LedgerError("validation_error", details={"reason": "source_binding_mismatch"})
    try:
        batch = prepare(
            workbook, workspace_id=ctx_worker.workspace,
            authority_epoch=ctx_worker.authority_epoch,
        )
    except WorkbookImportError as exc:
        raise api.LedgerError("validation_error", details={"reason": str(exc)}) from None
    return persist_staged_batch(ledger, ctx_worker, batch)
