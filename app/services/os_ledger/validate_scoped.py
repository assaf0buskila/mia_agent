"""``ScopedStateValidator``: the post-apply state check limited to what an apply can break (V2-18).

V2_PG_CONTRACTS.md §8: it checks only the touched records' references, the touched payments'
refund graph and the expense source key (through the unique index), and it replaces the full
``FullStateValidator`` in ``build_ledger()`` only because a property test proves scoped
accept/reject equal to full accept/reject over random commit/undo/decide sequences, on the
FakeLedger and on PostgreSQL (``tests/services/os_ledger/test_validate_scoped_property.py``).

Why the scope is exact. Every committed state passed the full check (each apply runs one of the two
equal validators), so in the post-apply state only a record whose own inputs, or whose referenced
rows, the apply changed can fail ``validate_state``. The checks run in ``validate_state``'s own
order, through the same ``domain`` functions, so the first failure, and its reason code, is the one
the full check would raise:

1. the canonical record check of every touched live record (``_record_indexes``);
2. ``_validate_work_dependency`` for works, payments, time and tasks, in id order, over the
   touched live records plus every live referrer of a client that is gone after the apply and of
   any touched work (a referrer's check reads the work's existence, person, path and currency);
3. ``_validate_payment_graph`` over every live refund of X, where X is the touched payments plus
   the originals of the touched live refunds, with those refunds' live non-refund originals (a
   refund-kind original is left out, so its refunds read it as not found, as in the full check);
4. the expense source key: a touched live expense whose (source_type, source_key) also belongs to
   another live expense, found through the ``(workspace_id, source_type, sha256(source_key))``
   unique index. ``apply`` already refuses such a write, so this check is the backstop.

The post-apply state is read from the database (``tx.get_many``), never from ``Mutation.after``.
Reads beyond the ``LedgerTx`` protocol go through a ``ScopeReader``: ``PgScope`` on PostgreSQL; the
property test passes the FakeLedger's adapter. Nothing here logs a record field (I7).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Protocol

from app.services.os_ledger import api
from app.services.os_ledger.domain import (
    ENUMS,
    DomainError,
    _canonical_record,
    _validate_payment_graph,
    _validate_work_dependency,
)
from app.services.os_ledger.tx import TYPED_COLUMNS, record_columns, wire
from app.services.os_ledger.validate_full import domain_error

__all__ = ["PgScope", "ScopeReader", "ScopedStateValidator"]

DEPENDENT_ENTITIES = ("works", "payments", "time", "tasks")  # validate_state's phase-2 order
_REFUND = ENUMS["payment_kind"][1]  # a refund; validate_state's payment graph reads only these


class ScopeReader(Protocol):
    def referrers(self, entity: str, field: str, ids: Sequence[str]) -> list[api.RecordDict]:
        """Live (non-tombstoned) ``entity`` wire records whose ``field`` is one of ``ids``."""

    def duplicate_source(self, expenses: Sequence[Mapping[str, Any]]) -> bool:
        """True when another live expense has the (source_type, source_key) of one of these."""


class PgScope:
    """``ScopeReader`` on the transaction's connection (the runtime role's SELECT grants)."""

    def __init__(self, tx: Any) -> None:
        self._tx = tx

    def referrers(self, entity: str, field: str, ids: Sequence[str]) -> list[api.RecordDict]:
        if entity not in api.ENTITIES or field not in TYPED_COLUMNS[entity]:
            raise api.LedgerError("ledger_integrity_failed", reason="scope_column")
        rows = self._tx._exec(
            f"SELECT {record_columns(entity)} FROM assaf_os.os_{entity} WHERE workspace_id = :ws "
            f'AND deleted_at IS NULL AND {field} = ANY(:ids) ORDER BY record_id COLLATE "C"',
            {"ids": sorted(set(ids))}).all()
        return [wire(entity, row) for row in rows]

    def duplicate_source(self, expenses: Sequence[Mapping[str, Any]]) -> bool:
        for expense in expenses:
            count = self._tx._exec(
                "SELECT count(*) FROM assaf_os.os_expenses WHERE workspace_id = :ws "
                "AND deleted_at IS NULL AND source_type = :type "
                "AND assaf_os.expense_source_key_sha256(source_key) = "
                "assaf_os.expense_source_key_sha256(CAST(:key AS text)) AND source_key = :key",
                {"type": expense.get("source_type"), "key": expense.get("source_key")}).scalar()
            if int(count) > 1:
                return True
        return False


def _live(tx: Any, entity: str, ids: Iterable[Any]) -> dict[str, api.RecordDict]:
    wanted = sorted({value for value in ids if isinstance(value, str) and value})
    if not wanted:
        return {}
    found = tx.get_many([api.RecordRef(entity, value) for value in wanted])
    return {ref.record_id: record for ref, record in found.items()}


class ScopedStateValidator:
    """``api.StateValidator``: ``validate_state`` restricted to what ``applied`` can break."""

    def __init__(self, scope: Callable[[Any], ScopeReader] = PgScope) -> None:
        self._scope = scope

    def validate(self, tx: Any, applied: Sequence[api.Mutation]) -> None:
        try:
            self._validate(tx, tuple(applied))
        except DomainError as exc:
            raise domain_error(exc) from None

    def _validate(self, tx: Any, applied: tuple[api.Mutation, ...]) -> None:
        if not applied:
            return
        touched: dict[str, set[str]] = {}
        for mutation in applied:
            touched.setdefault(mutation.entity, set()).add(mutation.record_id)
        live: dict[tuple[str, str], api.RecordDict] = {}
        for entity in api.ENTITIES:  # one get_many per entity, in validate_state's order
            for record_id, record in sorted(_live(tx, entity, touched.get(entity, ())).items()):
                live[(entity, record_id)] = record

        # 1. _record_indexes: the canonical record check, touched live records only.
        for (entity, _), record in live.items():
            _canonical_record(entity, record)

        # 2. references, over the touched live records and the referrers of gone clients and of
        #    touched works.
        scope = self._scope(tx)
        candidates = {key: record for key, record in live.items()
                      if key[0] in DEPENDENT_ENTITIES}
        gone_clients = sorted(record_id for record_id in touched.get("clients", ())
                              if ("clients", record_id) not in live)
        works = sorted(touched.get("works", ()))
        for entity in DEPENDENT_ENTITIES if gone_clients else ():
            for record in scope.referrers(entity, "person_id", gone_clients):
                candidates[(entity, record[api.ID_KEYS[entity]])] = record
        for entity in ("payments", "time", "tasks") if works else ():
            for record in scope.referrers(entity, "work_id", works):
                candidates[(entity, record[api.ID_KEYS[entity]])] = record
        clients = _live(tx, "clients", (r.get("person_id") for r in candidates.values()))
        work_rows = _live(tx, "works", (r.get("work_id") for (e, _), r in candidates.items()
                                        if e != "works"))
        for entity in DEPENDENT_ENTITIES:
            for key in sorted(k for k in candidates if k[0] == entity):
                _validate_work_dependency(entity, candidates[key], clients, work_rows)

        # 3. the refund graph of the touched payments.
        originals = set(touched.get("payments", ())) | {
            record.get("original_payment_id") for (entity, _), record in live.items()
            if entity == "payments" and record.get("kind") == _REFUND}
        originals = {value for value in originals if isinstance(value, str) and value}
        if originals:
            refunds = {r["payment_id"]: r for r in scope.referrers(
                "payments", "original_payment_id", sorted(originals))}
            graph = {key: record for key, record in _live(
                tx, "payments", (r.get("original_payment_id") for r in refunds.values())).items()
                if record.get("kind") != _REFUND}
            graph.update(refunds)
            _validate_payment_graph([graph[key] for key in sorted(graph)])

        # 4. the expense source key, through the unique index.
        expenses = [record for (entity, _), record in live.items() if entity == "expenses"]
        if expenses and scope.duplicate_source(expenses):
            raise DomainError("duplicate_expense_source")
