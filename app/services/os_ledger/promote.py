"""Promote a validated import batch into the ledger (contract section 15, erratum E1-rev, E20).

One ``kind="import"`` transaction (``core=shadow``) does everything: it replays or refuses,
finds the orphans, writes every record through the shared ``apply`` at its manifest revision,
applies the signed cancellations, settles the batch ``validated -> imported`` and finishes one
receipt. Any exception rolls the whole transaction back, so a refusal leaves no trace.

Idempotency: ``request_id = api.import_request_uuid(epoch, manifest_digest)`` and the request digest
is ``{"import": manifest_digest, "epoch": epoch}``. A second promote of the same signed manifest at
the same epoch returns the stored receipt and writes nothing. The same workbook after a rollback
(epoch E+1) has a different manifest (its binding carries the epoch) and a different request id, so
it is a new import; records that already match are left alone instead of re-written.

Contract gap (recorded in the PR): the frozen ``LedgerTx`` has no read for import batches or rows,
so promote cannot reload the staged rows by ``batch_id``. It takes the ``StagedWorkbook`` produced
from the same workbook bytes instead; that preparation is deterministic, so the rows are the ones
``stage`` persisted, and ``set_import_status(batch_id, "imported")`` still refuses unless the
persisted batch is ``validated``.

Nothing here logs a record, an id or an amount. Log lines are reason codes and counts.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.services.os_ledger import api
from app.services.os_ledger.import_workbook import (
    StagedWorkbook,
    WorkbookImportError,
    _require_worker,
    manifest,
    quarantine_count,
    require_batch_binding,
    staged_records,
)

_LOG = logging.getLogger("mia.os_import")
_GET_CHUNK = 200
# Parents before children for writes, children before parents for cancellations (E20: the shared
# apply refuses ``has_dependents`` for any update into a FORK-DELETE end state).
_UPSERT_ORDER = ("clients", "works", "payments", "time", "expenses", "tasks", "leads")
_CANCEL_ORDER = ("tasks", "time", "expenses", "payments", "works", "clients", "leads")


@dataclass(frozen=True)
class PromoteResult:
    request_id: str
    batch_id: str
    receipt: api.ReceiptRow
    replayed: bool
    created: int
    updated: int
    unchanged: int
    cancelled: int


def _comparable(record: Mapping[str, Any]) -> dict[str, Any]:
    """A wire record without its null optionals: an absent key and an explicit null are equal."""
    return {key: value for key, value in record.items() if value is not None}


def _payment_depth(payments: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    """Refund-chain depth inside ``payments``: 0 for an original, 1 for its refund, and so on."""
    depth: dict[str, int] = {}
    for payment_id in payments:
        steps, cursor, seen = 0, payment_id, set()
        while cursor in payments and payments[cursor].get("original_payment_id") in payments:
            if cursor in seen:  # a cycle is for the ledger's domain validation to refuse
                break
            seen.add(cursor)
            cursor = payments[cursor]["original_payment_id"]
            steps += 1
        depth[payment_id] = steps
    return depth


def _ordered(entity: str, records: Mapping[str, Mapping[str, Any]], *, reverse: bool) -> list[str]:
    """Ids in write order: sorted, and for payments a refund chain's original before its refund
    (``reverse`` flips that, so a cancellation takes the refund first)."""
    if entity != "payments":
        return sorted(records)
    depth = _payment_depth(records)
    return sorted(records, key=lambda key: (-depth[key] if reverse else depth[key], key))


def _fetch(tx: api.LedgerTx, refs: Sequence[api.RecordRef], *, include_deleted: bool):
    found: dict[api.RecordRef, api.RecordDict] = {}
    for start in range(0, len(refs), _GET_CHUNK):
        found.update(tx.get_many(refs[start:start + _GET_CHUNK], include_deleted=include_deleted))
    return found


def _cancellation(entity: str, current: api.RecordDict) -> api.Mutation:
    """The entity's FORK-DELETE end state at the next revision (a tombstone for a task)."""
    end = api.FORK_DELETE_END_STATE[entity]
    record_id = current[api.ID_KEYS[entity]]
    after = {**current, "revision": current["revision"] + 1}
    if end is None:
        return api.Mutation(entity, record_id, "tombstone", current, after)
    after[end[0]] = end[1]
    if entity == "payments" and after.get("original_payment_id"):
        # A cancelled payment is no refund: the domain refuses an original on a non-refund.
        after["original_payment_id"] = None
    return api.Mutation(entity, record_id, "upsert", current, after)


def promote(
    ledger: api.LedgerStore,
    ctx_worker: api.TrustedOwnerContext,
    batch: StagedWorkbook,
    approved_manifest_digest: str,
    *,
    cancellations: Sequence[Mapping[str, str]] = (),
    undo_window_seconds: int | None = None,
) -> PromoteResult:
    """Promote ``batch`` once the owner's signed ``approved_manifest_digest`` matches it.

    Refusals, in order: ``import_quarantine_unresolved``, ``manifest_digest_mismatch``, then inside
    the transaction ``authority_not_active`` / ``authority_changed`` (admission), the replay check,
    and ``import_orphan_rows``.
    """
    try:
        result = _promote(
            ledger, ctx_worker, batch, approved_manifest_digest, cancellations,
            undo_window_seconds,
        )
    except api.LedgerError as exc:
        _LOG.info("os_import op=promote outcome=refused reason=%s", exc.code)
        raise
    _LOG.info(
        "os_import op=promote outcome=ok reason=%s created=%d updated=%d unchanged=%d cancelled=%d",
        "replayed" if result.replayed else "committed",
        result.created, result.updated, result.unchanged, result.cancelled,
    )
    return result


def _promote(ledger, ctx_worker, batch, approved_manifest_digest, cancellations, window):
    _require_worker(ctx_worker)
    require_batch_binding(ctx_worker, batch)
    # Pure refusals first: nothing touches the ledger until the signed manifest is the batch's.
    blocked = quarantine_count(batch)
    if blocked:
        raise api.LedgerError("import_quarantine_unresolved", details={"count": blocked})
    try:
        signed = manifest(batch, cancellations=cancellations)
    except WorkbookImportError as exc:
        raise api.LedgerError("validation_error", details={"reason": str(exc)}) from None
    manifest_digest = api.digest(signed)
    if not isinstance(approved_manifest_digest, str) or approved_manifest_digest != manifest_digest:
        raise api.LedgerError("manifest_digest_mismatch")
    cancel_ids: dict[str, set[str]] = {entity: set() for entity in api.ENTITIES}
    for item in signed["cancellations"]:
        cancel_ids[item["entity"]].add(item["record_id"])

    epoch = ctx_worker.authority_epoch
    request_id = api.import_request_uuid(epoch, manifest_digest)
    request_digest = api.digest({"import": manifest_digest, "epoch": epoch})
    desired = staged_records(batch)
    if window is None:
        from app.core.config import get_settings

        window = get_settings().owner_undo_window_seconds

    with ledger.transaction(ctx_worker, domain="core", kind="import") as tx:
        existing = tx.proposal_by_request(request_id)
        if existing is not None:
            if existing.request_digest != request_digest:
                raise api.LedgerError("idempotency_mismatch")
            receipt = tx.receipt(request_id)
            if receipt is None:  # a proposal and its receipt are written in one transaction
                raise api.LedgerError("ledger_integrity_failed", reason="import_receipt_missing")
            return PromoteResult(request_id, batch.batch_id, receipt, True, 0, 0, 0, 0)

        # Orphans: ids live in the ledger that the workbook no longer has. Each must be signed
        # under ``cancellations``; a signed id that is not an orphan is a stale manifest.
        unsigned: dict[str, int] = {}
        stale = 0
        for entity in api.ENTITIES:
            live = tx.live_ids(entity)
            wanted = {record[api.ID_KEYS[entity]] for record in desired[entity]}
            orphans = live - wanted
            if orphans - cancel_ids[entity]:
                unsigned[entity] = len(orphans - cancel_ids[entity])
            stale += len(cancel_ids[entity] - orphans)
        if unsigned:
            raise api.LedgerError("import_orphan_rows", details={"counts": unsigned})
        if stale:
            raise api.LedgerError("validation_error", details={"reason": "cancellation_not_live"})

        mutations, tally = _plan(tx, desired, cancel_ids)
        proposal = tx.insert_proposal(api.ProposalDraft(
            kind="import", source="IMPORT", request_id=request_id, request_digest=request_digest,
            entity="import", record_id=batch.batch_id, expected_revision=0, mutations=(),
            dependency_digest=manifest_digest, expires_at=tx.now + timedelta(seconds=window),
        ))
        tx.set_import_status(batch.batch_id, "imported")
        if mutations:
            tx.apply(mutations, source="IMPORT", request_id=request_id)
        receipt = tx.finish(
            proposal, status="committed", receipt_request_id=request_id, applied=mutations,
        )
    return PromoteResult(request_id, batch.batch_id, receipt, False, **tally)


def _plan(tx, desired, cancel_ids):
    """The ordered mutations and the counts: creates, newer updates, signed cancellations."""
    refs = [api.RecordRef(entity, record[api.ID_KEYS[entity]])
            for entity in api.ENTITIES for record in desired[entity]]
    refs += [api.RecordRef(entity, record_id)
             for entity in api.ENTITIES for record_id in sorted(cancel_ids[entity])]
    visible = _fetch(tx, refs, include_deleted=False)
    anywhere = _fetch(tx, refs, include_deleted=True)

    upserts: list[api.Mutation] = []
    unchanged = created = updated = 0
    for entity in _UPSERT_ORDER:
        by_id = {record[api.ID_KEYS[entity]]: record for record in desired[entity]}
        for record_id in _ordered(entity, by_id, reverse=False):
            record = by_id[record_id]
            ref = api.RecordRef(entity, record_id)
            current = visible.get(ref)
            if current is None:
                if ref in anywhere:  # tombstoned in the ledger but present in the workbook
                    raise api.LedgerError(
                        "target_changed", details={"reason": "import_target_deleted"})
                upserts.append(api.Mutation(entity, record_id, "upsert", None, record))
                created += 1
            elif _comparable(current) == _comparable(record):
                unchanged += 1
            elif record["revision"] > current["revision"]:
                upserts.append(api.Mutation(entity, record_id, "upsert", current, record))
                updated += 1
            else:
                raise api.LedgerError(
                    "target_changed", details={"reason": "import_revision_not_newer"})

    cancels: list[api.Mutation] = []
    for entity in _CANCEL_ORDER:
        current_by_id = {
            record_id: visible[api.RecordRef(entity, record_id)]
            for record_id in cancel_ids[entity] if api.RecordRef(entity, record_id) in visible
        }
        if len(current_by_id) != len(cancel_ids[entity]):
            raise api.LedgerError("validation_error", details={"reason": "cancellation_not_live"})
        for record_id in _ordered(entity, current_by_id, reverse=True):
            cancels.append(_cancellation(entity, current_by_id[record_id]))
    tally = {"created": created, "updated": updated, "unchanged": unchanged,
             "cancelled": len(cancels)}
    return tuple(upserts + cancels), tally
