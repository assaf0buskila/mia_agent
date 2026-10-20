"""The V2-18 read model on PostgreSQL (V2_PG_CONTRACTS.md §8, §10.2, §22 E7 (d) and E14 (1)).

``ReadModelLedger`` is the ``PgLedger`` that ``build_ledger()`` returns from V2-18 on (the
V2-16 -> V2-18 handoff). It keeps V2-16's transactions, core and admission, and replaces the
readers' OFFSET windows with keyset pages:

* every reader runs in ``PgLedger._read``: one REPEATABLE READ READ ONLY snapshot, behind the §5.5
  guard, at the context's epoch, core active or fenced (shadow too for the WORKER channel). A read
  never writes (pg_stat_xact is asserted in ``tests/services/os_ledger/test_reads.py``);
* ``page``: live rows only; tasks by open statuses first (לביצוע, בתהליך, ממתין), then בוצע, then
  בוטל, then ``due_date ASC NULLS LAST``, then ``record_id``; every other entity by
  ``business_date DESC NULLS LAST, record_id ASC``. ``total`` counts live rows;
* ``deleted``: tombstones by ``deleted_at DESC``, then ``record_id`` (``total`` still counts live
  rows, as V2-16 and the FakeLedger do);
* ``pending``: ``kind='proposal' AND status='pending' AND expires_at >`` the read's ``now()`` AND
  ``epoch`` = the context's (§22 E7 (d)), by ``created_at``, then ``proposal_id``;
* ``proposal`` (§22 E14 (1)): one stored proposal by id, any status or kind, or ``None`` when it is
  absent or of another epoch (the I12 filter of ``pending()``);
* ``head``, ``labels`` and ``monthly_summary`` are V2-16's, already one snapshot each.
  ``monthly_summary`` reads only ``os_summary_monthly`` (§8, §14).

Cursors are ``CursorCodec`` tokens (``app/services/owner_os_reads.py:109``). The codec signs a
boundary row id and a 32-bit checksum, bound to the owner, the workspace and a context. Here the
context names the listing, the entity and the epoch; the checksum is the observed version (low 32
bits); the row id is the last record (or proposal) on the page, whose sort key the next page reads
back in the same snapshot. So a cursor is signed over {v: observed_version, e: entity, k: last sort
key}: a forged, altered, foreign or malformed one is ``invalid_cursor``; a version change, an anchor
row that left its listing, or a cursor past the codec's 300 s TTL (on the ledger's clock) is
``cursor_invalidated`` (restart from the top). The key is ``owner_os_cursor_secret`` when it has at
least 32 bytes, else a per-process random key, like V2-16's cursors. Logs carry reason codes only.
"""

from __future__ import annotations

import secrets
from typing import Any

from app.services.os_ledger import api
from app.services.os_ledger.core import _DONE_TASK, _OPEN_TASKS, PgLedger
from app.services.os_ledger.tx import _PROPOSAL_COLUMNS, proposal_row, record_columns, wire
from app.services.owner_os_reads import CursorCodec, OwnerOsCursorInvalid, OwnerOsReadError

__all__ = ["KeysetCursors", "ReadModelLedger"]

_PROCESS_SECRET = secrets.token_hex(32)
_VERSION_BITS = 0xFFFFFFFF
_TASK_RANK = "(CASE WHEN status = ANY(:open) THEN 0 WHEN status = :done THEN 1 ELSE 2 END)"
_ID_AFTER = 'record_id COLLATE "C" > :k_id'


def _limit(limit: Any) -> int:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise api.LedgerError("invalid_query")
    return limit


def _invalidated(tx: Any) -> api.LedgerError:
    return api.LedgerError("cursor_invalidated",
                           details={"observed_version": tx.current_version})


class KeysetCursors:
    """``CursorCodec`` cursors over {v: observed_version, e: entity, k: the last row's id}."""

    def __init__(self, secret: Any) -> None:
        ok = isinstance(secret, str) and len(secret.encode("utf-8")) >= 32
        self._codec = CursorCodec(secret if ok else _PROCESS_SECRET)

    @staticmethod
    def _context(listing: str, entity: str, epoch: int) -> str:
        return f"os-ledger.v1\0{listing}\0{entity}\0{epoch}"

    def encode(self, tx: Any, listing: str, entity: str, anchor: str) -> str:
        try:
            return self._codec.encode(
                owner_id=str(tx.ctx.numeric_owner), workspace_id=tx.ctx.workspace,
                context=self._context(listing, entity, tx.ctx.authority_epoch), row_id=anchor,
                checksum=tx.current_version & _VERSION_BITS, now=int(tx.now.timestamp()))
        except OwnerOsReadError:
            raise api.LedgerError("ledger_unavailable", reason="cursor_unencodable") from None

    def decode(self, tx: Any, listing: str, entity: str, token: Any) -> str:
        if not isinstance(token, str):
            raise api.LedgerError("invalid_cursor")
        try:
            boundary = self._codec.decode(
                owner_id=str(tx.ctx.numeric_owner), workspace_id=tx.ctx.workspace,
                context=self._context(listing, entity, tx.ctx.authority_epoch), token=token,
                now=int(tx.now.timestamp()))
        except OwnerOsCursorInvalid:  # past the codec's TTL: a stale page, start again
            raise _invalidated(tx) from None
        except (OwnerOsReadError, ValueError, TypeError):
            raise api.LedgerError("invalid_cursor") from None
        if boundary.checksum != tx.current_version & _VERSION_BITS:
            raise _invalidated(tx)
        return boundary.row_id


def _task_rank(status: Any) -> int:
    return 0 if status in _OPEN_TASKS else (1 if status == _DONE_TASK else 2)


def _keyset(entity: str, deleted: bool, anchor: Any) -> tuple[str, dict[str, Any]]:
    """The predicate of the rows strictly after ``anchor`` in the listing's order."""
    params: dict[str, Any] = {"k_id": anchor.record_id}
    if deleted:
        params["k_at"] = anchor.deleted_at
        return f"AND (deleted_at < :k_at OR (deleted_at = :k_at AND {_ID_AFTER}))", params
    if entity == "tasks":
        params.update(k_rank=_task_rank(anchor.status), k_due=anchor.due_date)
        due = (f"(due_date IS NULL AND {_ID_AFTER})" if anchor.due_date is None else
               f"(due_date > :k_due OR (due_date = :k_due AND {_ID_AFTER}) OR due_date IS NULL)")
        return f"AND ({_TASK_RANK} > :k_rank OR ({_TASK_RANK} = :k_rank AND {due}))", params
    if anchor.business_date is None:
        return f"AND business_date IS NULL AND {_ID_AFTER}", params
    params["k_date"] = anchor.business_date
    return (f"AND (business_date < :k_date OR (business_date = :k_date AND {_ID_AFTER}) "
            "OR business_date IS NULL)"), params


def _order(entity: str, deleted: bool) -> str:
    if deleted:
        return 'deleted_at DESC, record_id COLLATE "C"'
    if entity == "tasks":
        return f'{_TASK_RANK}, due_date ASC NULLS LAST, record_id COLLATE "C"'
    return 'business_date DESC NULLS LAST, record_id COLLATE "C"'


class ReadModelLedger(PgLedger):
    """``PgLedger`` with the V2-18 keyset readers and ``proposal()`` (§22 E14 (1))."""

    def __init__(self, settings: Any, **kwargs: Any) -> None:
        super().__init__(settings, **kwargs)
        self._keyset = KeysetCursors(getattr(settings, "owner_os_cursor_secret", "") or "")

    def _listing(self, ctx: api.TrustedOwnerContext, entity: Any, after: Any, limit: Any,
                 deleted: bool) -> api.Page:
        if entity not in api.ENTITIES:
            raise api.LedgerError("invalid_query")
        listing = "deleted" if deleted else "page"
        with self._read(ctx) as tx:
            limit = _limit(limit)
            params: dict[str, Any] = {"fetch": limit + 1, "open": list(_OPEN_TASKS),
                                      "done": _DONE_TASK}
            keyset = ""
            if after is not None:
                anchor_id = self._keyset.decode(tx, listing, entity, after)
                extra = ", status, due_date" if entity == "tasks" else ""
                anchor = tx._one(
                    f"SELECT record_id, business_date, deleted_at{extra} FROM "
                    f"assaf_os.os_{entity} WHERE workspace_id = :ws AND record_id = :id",
                    {"id": anchor_id})
                if anchor is None or (anchor.deleted_at is not None) != deleted:
                    raise _invalidated(tx)
                keyset, more = _keyset(entity, deleted, anchor)
                params.update(more)
            rows = tx._exec(
                f"SELECT {record_columns(entity)} FROM assaf_os.os_{entity} "
                f"WHERE workspace_id = :ws AND deleted_at IS {'NOT ' if deleted else ''}NULL "
                f"{keyset} ORDER BY {_order(entity, deleted)} LIMIT :fetch", params).all()
            total = int(tx._exec(
                f"SELECT count(*) FROM assaf_os.os_{entity} "
                "WHERE workspace_id = :ws AND deleted_at IS NULL").scalar())
            cursor = (self._keyset.encode(tx, listing, entity, rows[limit - 1].record_id)
                      if len(rows) > limit else None)
            return api.Page(entity, tuple(wire(entity, row) for row in rows[:limit]), cursor,
                            total, tx.current_version)

    def page(self, ctx: api.TrustedOwnerContext, entity: api.Entity, *, after: str | None,
             limit: int) -> api.Page:
        return self._listing(ctx, entity, after, limit, False)

    def deleted(self, ctx: api.TrustedOwnerContext, entity: api.Entity, *, after: str | None,
                limit: int) -> api.Page:
        return self._listing(ctx, entity, after, limit, True)

    def pending(self, ctx: api.TrustedOwnerContext, *, after: str | None,
                limit: int) -> api.ProposalPage:
        """kind proposal, status pending, expires_at > the read's now, at the context's epoch."""
        with self._read(ctx) as tx:
            limit = _limit(limit)
            params: dict[str, Any] = {"now": tx.now, "epoch": ctx.authority_epoch,
                                      "fetch": limit + 1}
            keyset = ""
            if after is not None:
                anchor_id = self._keyset.decode(tx, "pending", "proposals", after)
                anchor = tx._one("SELECT created_at FROM assaf_os.os_proposals "
                                 "WHERE workspace_id = :ws AND proposal_id = :id",
                                 {"id": anchor_id})
                if anchor is None:
                    raise _invalidated(tx)
                keyset = ('AND (created_at > :k_at OR (created_at = :k_at '
                          'AND proposal_id COLLATE "C" > :k_id))')
                params.update(k_at=anchor.created_at, k_id=anchor_id)
            rows = tx._exec(
                f"SELECT {_PROPOSAL_COLUMNS} FROM assaf_os.os_proposals WHERE workspace_id = :ws "
                "AND kind = 'proposal' AND status = 'pending' AND expires_at > :now "
                f'AND epoch = :epoch {keyset} ORDER BY created_at, proposal_id COLLATE "C" '
                "LIMIT :fetch", params).all()
            cursor = (self._keyset.encode(tx, "pending", "proposals", rows[limit - 1].proposal_id)
                      if len(rows) > limit else None)
            return api.ProposalPage(tuple(proposal_row(row) for row in rows[:limit]), cursor,
                                    tx.current_version)

    def proposal(self, ctx: api.TrustedOwnerContext, proposal_id: str) -> api.ProposalRow | None:
        """§22 E14 (1): one stored proposal, read-only, at the context's epoch (I12)."""
        with self._read(ctx) as tx:
            row = tx.proposal(proposal_id) if isinstance(proposal_id, str) else None
            if row is None or row.epoch != ctx.authority_epoch:
                return None
            return row
