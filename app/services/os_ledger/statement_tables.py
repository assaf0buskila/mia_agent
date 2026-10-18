"""The statement tables behind ``StatementBatchService`` (V2-26; V2_PG_CONTRACTS.md §12.2, §4).

One protocol, two implementations. ``PgTables`` runs the §4 SQL on ``tx.connection`` (the runtime
role; chunk-owned tables only, never a V2-14 core table, §22 E1). ``MemoryTables`` keeps the same
rows in ``tx.scratch`` so a fake-backed test rolls them back with the ledger transaction. Both are
built from the open ``LedgerTx`` and write inside it: a statement write is never a transaction of
its own.

Rows are plain frozen dataclasses with the column names of §4. Nothing here logs, and no method
renders a value into an exception: statement text is private (I7).

The review drafts table doubles as the **batch registry**. ``LedgerTx`` has no way to list proposals
(and ``tx.connection`` is barred from ``os_proposals``), but ``expire_due``, FORK-SUPERSEDE and
``revoke_pending_for_credential`` must find every pending batch of a statement, of a credential, or
of the workspace. So every batch gets one draft row at submit (``draft_revision`` 0), whose
``choices`` object also holds a reserved ``index`` entry ``{statement_id, credential_id, source,
open}``. It survives a restore (only ``os_statement_batch_lines`` data is excluded from a dump), so
a restored batch with no lines is still found and finished.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from sqlalchemy import text

from app.services.os_ledger import api

EMPTY_DIGEST = api.digest([])
INDEX_KEY = "index"  # the reserved entry of os_statement_review_drafts.choices


# ---------------------------------------------------------------------------------------------
# Rows


@dataclass(frozen=True)
class StatementRow:
    statement_id: str
    account_label: str = field(repr=False)
    charge_date: date
    cycle: str
    cycle_from: date
    cycle_to: date
    status: str = "active"
    line_count: int = 0
    lines_digest: str = EMPTY_DIGEST
    current_proposal_id: str | None = None
    approved_proposal_id: str | None = None
    imported_version: int | None = None


@dataclass(frozen=True)
class LineRow:
    """A committed statement line. A personal row keeps only key, tag, state and revision."""

    statement_id: str
    line_key: str
    tag: str
    state: str = "active"
    txn_date: date | None = None
    merchant_display: str | None = field(default=None, repr=False)
    merchant_key: str | None = field(default=None, repr=False)
    amount_minor: int | None = field(default=None, repr=False)
    kind: str | None = None
    currency: str | None = None
    original_amount_minor: int | None = field(default=None, repr=False)
    original_currency: str | None = None
    installment_n: int | None = None
    installment_of: int | None = None
    category: str | None = field(default=None, repr=False)
    path: str | None = None
    platform: str | None = None
    is_subscription: bool | None = None
    rule_id: str | None = None
    assistant_tag: str | None = None
    owner_override: dict[str, Any] | None = None
    expense_id: str | None = None
    matched_expense_id: str | None = None
    expense_revision_at_import: int | None = None
    revision: int = 1


@dataclass(frozen=True)
class BatchLineRow:
    proposal_id: str
    line_key: str
    occurrence_index: int
    detail: dict[str, Any] | None = field(default=None, repr=False)
    purged_at: datetime | None = None


@dataclass(frozen=True)
class RuleRow:
    rule_id: str
    tag: str
    match: str
    merchant_key: str | None = field(repr=False)
    merchant_key_hmac: str = field(repr=False)
    category: str | None = field(default=None, repr=False)
    path: str | None = None
    platform: str | None = None
    is_subscription: bool | None = None
    label: str | None = field(default=None, repr=False)
    aliases: tuple[str, ...] = field(default=(), repr=False)
    active: bool = True
    revision: int = 1
    source_proposal_id: str = ""


@dataclass(frozen=True)
class DraftRow:
    """The owner's saved review of one batch, plus the batch registry entry (module docstring)."""

    proposal_id: str
    draft_revision: int
    overrides: dict[str, Any] = field(default_factory=dict, repr=False)
    choices: dict[str, Any] = field(default_factory=dict, repr=False)
    statement_id: str = ""
    credential_id: str | None = None
    source: str = ""
    open: bool = True


@dataclass(frozen=True)
class CredentialRow:
    """The columns of ``os_assistant_credentials`` this service reads (V2-37 owns the table)."""

    credential_id: str
    owner_id: str
    epoch: int
    label: str
    kind: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


class StatementTables(Protocol):
    def statement(self, statement_id: str) -> StatementRow | None: ...
    def statements(self) -> list[StatementRow]: ...
    def insert_statement(self, row: StatementRow) -> None: ...
    def update_statement(self, row: StatementRow) -> None: ...
    def lines(self, statement_id: str) -> list[LineRow]: ...
    def lines_of(self, statement_ids: Sequence[str]) -> dict[str, list[LineRow]]: ...
    def insert_line(self, row: LineRow) -> None: ...
    def update_line(self, row: LineRow) -> None: ...
    def matched_elsewhere(self, statement_id: str) -> set[str]: ...
    def batch_lines(self, proposal_id: str) -> list[BatchLineRow]: ...
    def live_batch_line_count(self, proposal_id: str) -> int: ...
    def batch_line_count(self, proposal_id: str) -> int: ...
    def insert_batch_lines(self, rows: Sequence[BatchLineRow]) -> None: ...
    def purge_batch_lines(self, proposal_id: str) -> int: ...
    def rules(self) -> list[RuleRow]: ...
    def insert_rule(self, row: RuleRow) -> None: ...
    def update_rule(self, row: RuleRow) -> None: ...
    def draft(self, proposal_id: str) -> DraftRow | None: ...
    def put_draft(self, row: DraftRow) -> None: ...
    def open_drafts(
        self, *, statement_id: str | None = None, credential_id: str | None = None
    ) -> list[DraftRow]: ...
    def credential(self, credential_id: str) -> CredentialRow | None: ...
    def dump(self) -> dict[str, dict[Any, Any]]: ...


def tables_for(tx: Any) -> StatementTables:
    """PostgreSQL through ``tx.connection`` when there is one, else the in-memory scratch."""
    return PgTables(tx) if tx.connection is not None else MemoryTables(tx)


# ---------------------------------------------------------------------------------------------
# In memory


class MemoryTables:
    """Rows in ``tx.scratch["statement_tables"]``, rolled back with the ledger transaction."""

    NAMES = ("statements", "lines", "batch_lines", "rules", "drafts", "credentials")

    def __init__(self, tx: Any) -> None:
        self._tx = tx

    def _t(self, name: str) -> dict[Any, Any]:
        return self._tx.scratch.setdefault("statement_tables", {}).setdefault(name, {})

    # statements
    def statement(self, statement_id: str) -> StatementRow | None:
        return self._t("statements").get(statement_id)

    def statements(self) -> list[StatementRow]:
        return sorted(self._t("statements").values(),
                      key=lambda r: (r.charge_date, r.statement_id))

    def insert_statement(self, row: StatementRow) -> None:
        table = self._t("statements")
        _invariant(row.statement_id not in table, "statement_pk")
        _invariant(not any((r.account_label, r.charge_date) == (row.account_label, row.charge_date)
                           for r in table.values()), "statement_label_charge_date")
        table[row.statement_id] = row

    def update_statement(self, row: StatementRow) -> None:
        table = self._t("statements")
        _invariant(row.statement_id in table, "statement_missing")
        table[row.statement_id] = row

    # committed lines
    def lines(self, statement_id: str) -> list[LineRow]:
        return sorted((r for (sid, _), r in self._t("lines").items() if sid == statement_id),
                      key=lambda r: r.line_key)

    def lines_of(self, statement_ids: Sequence[str]) -> dict[str, list[LineRow]]:
        wanted = set(statement_ids)
        found: dict[str, list[LineRow]] = {sid: [] for sid in wanted}
        for (sid, _), row in sorted(self._t("lines").items()):
            if sid in wanted:
                found[sid].append(row)
        return found

    def insert_line(self, row: LineRow) -> None:
        table = self._t("lines")
        key = (row.statement_id, row.line_key)
        _invariant(key not in table, "line_pk")
        _invariant(row.statement_id in self._t("statements"), "line_statement")
        self._check_line(row)
        table[key] = row

    def update_line(self, row: LineRow) -> None:
        table = self._t("lines")
        key = (row.statement_id, row.line_key)
        _invariant(key in table, "line_missing")
        self._check_line(row)
        table[key] = row

    def _check_line(self, row: LineRow) -> None:
        if row.tag == "personal":
            _invariant(all(value is None for name, value in vars(row).items()
                           if name not in {"statement_id", "line_key", "tag", "state",
                                           "revision"}), "line_personal")
        else:
            _invariant(None not in (row.txn_date, row.merchant_display, row.merchant_key,
                                    row.amount_minor, row.kind, row.currency), "line_business")
        if row.matched_expense_id is not None and row.state == "active":
            _invariant(not any(
                r.matched_expense_id == row.matched_expense_id and r.state == "active"
                and (r.statement_id, r.line_key) != (row.statement_id, row.line_key)
                for r in self._t("lines").values()), "line_matched_expense_unique")

    def matched_elsewhere(self, statement_id: str) -> set[str]:
        return {r.matched_expense_id for r in self._t("lines").values()
                if r.state == "active" and r.matched_expense_id is not None
                and r.statement_id != statement_id}

    # pending batch detail
    def batch_lines(self, proposal_id: str) -> list[BatchLineRow]:
        return sorted((r for (pid, _), r in self._t("batch_lines").items() if pid == proposal_id),
                      key=lambda r: (r.occurrence_index, r.line_key))

    def live_batch_line_count(self, proposal_id: str) -> int:
        return sum(1 for (pid, _), r in self._t("batch_lines").items()
                   if pid == proposal_id and r.detail is not None)

    def batch_line_count(self, proposal_id: str) -> int:
        return sum(1 for (pid, _) in self._t("batch_lines") if pid == proposal_id)

    def insert_batch_lines(self, rows: Sequence[BatchLineRow]) -> None:
        table = self._t("batch_lines")
        for row in rows:
            key = (row.proposal_id, row.line_key)
            _invariant(key not in table, "batch_line_pk")
            _invariant((row.detail is None) == (row.purged_at is not None), "batch_line_purge")
            table[key] = row

    def purge_batch_lines(self, proposal_id: str) -> int:
        table = self._t("batch_lines")
        purged = 0
        for key, row in list(table.items()):
            if key[0] == proposal_id and row.detail is not None:
                table[key] = BatchLineRow(row.proposal_id, row.line_key, row.occurrence_index,
                                          None, self._tx.now)
                purged += 1
        return purged

    # rules
    def rules(self) -> list[RuleRow]:
        return sorted(self._t("rules").values(), key=lambda r: r.rule_id)

    def insert_rule(self, row: RuleRow) -> None:
        table = self._t("rules")
        _invariant(row.rule_id not in table, "rule_pk")
        self._check_rule(row)
        table[row.rule_id] = row

    def update_rule(self, row: RuleRow) -> None:
        table = self._t("rules")
        _invariant(row.rule_id in table, "rule_missing")
        self._check_rule(row)
        table[row.rule_id] = row

    def _check_rule(self, row: RuleRow) -> None:
        _invariant(not any((r.match, r.merchant_key_hmac) == (row.match, row.merchant_key_hmac)
                           and r.rule_id != row.rule_id for r in self._t("rules").values()),
                   "rule_match_hmac_unique")
        if row.tag == "personal":
            _invariant(row.merchant_key is None and row.match == "exact" and not row.aliases
                       and row.label is None and row.category is None and row.path is None
                       and row.platform is None and row.is_subscription is None, "rule_personal")
        else:
            _invariant(row.merchant_key is not None, "rule_business")

    # drafts and the registry
    def draft(self, proposal_id: str) -> DraftRow | None:
        return self._t("drafts").get(proposal_id)

    def put_draft(self, row: DraftRow) -> None:
        self._t("drafts")[row.proposal_id] = row

    def open_drafts(self, *, statement_id: str | None = None,
                    credential_id: str | None = None) -> list[DraftRow]:
        return sorted((r for r in self._t("drafts").values() if r.open
                       and (statement_id is None or r.statement_id == statement_id)
                       and (credential_id is None or r.credential_id == credential_id)),
                      key=lambda r: r.proposal_id)

    def credential(self, credential_id: str) -> CredentialRow | None:
        return self._t("credentials").get(credential_id)

    def dump(self) -> dict[str, dict[Any, Any]]:
        return {name: copy.deepcopy(self._t(name)) for name in self.NAMES}


def _invariant(condition: bool, name: str) -> None:
    if not condition:
        raise AssertionError("fake_invariant:statement_" + name)


# ---------------------------------------------------------------------------------------------
# PostgreSQL

_STATEMENT_COLUMNS = (
    "statement_id, account_label, charge_date, cycle, cycle_from, cycle_to, status, line_count, "
    "lines_digest, current_proposal_id, approved_proposal_id, imported_version")
_LINE_FIELDS = (
    "statement_id", "line_key", "tag", "state", "txn_date", "merchant_display", "merchant_key",
    "amount_minor", "kind", "currency", "original_amount_minor", "original_currency",
    "installment_n", "installment_of", "category", "path", "platform", "is_subscription",
    "rule_id", "assistant_tag", "owner_override", "expense_id", "matched_expense_id",
    "expense_revision_at_import", "revision")
_LINE_COLUMNS = ", ".join(_LINE_FIELDS)
_RULE_COLUMNS = (
    "rule_id, tag, match, merchant_key, merchant_key_hmac, category, path, platform, "
    "is_subscription, label, aliases, active, revision, source_proposal_id")


def _text(value: Any) -> Any:
    """CHAR(n) columns come back padded; every value stored here is exactly its length."""
    return value.strip() if isinstance(value, str) else value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class PgTables:
    """The §4 tables on this transaction's connection (statement and rule SQL only)."""

    def __init__(self, tx: Any) -> None:
        self._c = tx.connection
        self._ws = tx.ctx.workspace
        self._now: datetime = tx.now

    def _run(self, sql: str, **params: Any):
        return self._c.execute(text(sql), {"ws": self._ws, **params})

    # statements
    def _statement(self, row: Any) -> StatementRow:
        return StatementRow(
            row.statement_id, row.account_label, row.charge_date, _text(row.cycle),
            row.cycle_from, row.cycle_to, row.status, int(row.line_count),
            _text(row.lines_digest), row.current_proposal_id, row.approved_proposal_id,
            None if row.imported_version is None else int(row.imported_version))

    def statement(self, statement_id: str) -> StatementRow | None:
        row = self._run(
            f"SELECT {_STATEMENT_COLUMNS} FROM assaf_os.os_statements "
            "WHERE workspace_id = :ws AND statement_id = :id", id=statement_id).first()
        return None if row is None else self._statement(row)

    def statements(self) -> list[StatementRow]:
        rows = self._run(
            f"SELECT {_STATEMENT_COLUMNS} FROM assaf_os.os_statements "
            "WHERE workspace_id = :ws ORDER BY charge_date, statement_id").all()
        return [self._statement(r) for r in rows]

    def insert_statement(self, row: StatementRow) -> None:
        self._run(
            "INSERT INTO assaf_os.os_statements (workspace_id, statement_id, account_label, "
            "charge_date, cycle, cycle_from, cycle_to, status, line_count, lines_digest, "
            "current_proposal_id, approved_proposal_id, imported_version, created_at, updated_at) "
            "VALUES (:ws, :statement_id, :account_label, :charge_date, :cycle, :cycle_from, "
            ":cycle_to, :status, :line_count, :lines_digest, :current_proposal_id, "
            ":approved_proposal_id, :imported_version, :now, :now)",
            now=self._now, **vars(row))

    def update_statement(self, row: StatementRow) -> None:
        self._run(
            "UPDATE assaf_os.os_statements SET status = :status, line_count = :line_count, "
            "lines_digest = :lines_digest, current_proposal_id = :current_proposal_id, "
            "approved_proposal_id = :approved_proposal_id, imported_version = :imported_version, "
            "updated_at = :now WHERE workspace_id = :ws AND statement_id = :statement_id",
            now=self._now, **vars(row))

    # committed lines
    @staticmethod
    def _line(row: Any) -> LineRow:
        values = {name: _text(getattr(row, name)) for name in _LINE_FIELDS}
        values["revision"] = int(values["revision"])
        return LineRow(**values)

    def lines(self, statement_id: str) -> list[LineRow]:
        return self.lines_of([statement_id])[statement_id]

    def lines_of(self, statement_ids: Sequence[str]) -> dict[str, list[LineRow]]:
        found: dict[str, list[LineRow]] = {sid: [] for sid in statement_ids}
        if not found:
            return found
        rows = self._run(
            f"SELECT {_LINE_COLUMNS} FROM assaf_os.os_statement_lines "
            "WHERE workspace_id = :ws AND statement_id = ANY(:ids) "
            "ORDER BY statement_id, line_key", ids=list(found)).all()
        for row in rows:
            found[row.statement_id].append(self._line(row))
        return found

    def _line_params(self, row: LineRow) -> dict[str, Any]:
        params = dict(vars(row))
        params["owner_override"] = (None if row.owner_override is None
                                    else _json(row.owner_override))
        return params

    def insert_line(self, row: LineRow) -> None:
        values = ", ".join(
            "CAST(:owner_override AS jsonb)" if name == "owner_override" else f":{name}"
            for name in _LINE_FIELDS)
        self._run(
            f"INSERT INTO assaf_os.os_statement_lines (workspace_id, {_LINE_COLUMNS}, "
            f"created_at, updated_at) VALUES (:ws, {values}, :now, :now)",
            now=self._now, **self._line_params(row))

    def update_line(self, row: LineRow) -> None:
        sets = ", ".join(
            "owner_override = CAST(:owner_override AS jsonb)" if name == "owner_override"
            else f"{name} = :{name}"
            for name in _LINE_FIELDS if name not in {"statement_id", "line_key"})
        self._run(
            f"UPDATE assaf_os.os_statement_lines SET {sets}, updated_at = :now "
            "WHERE workspace_id = :ws AND statement_id = :statement_id AND line_key = :line_key",
            now=self._now, **self._line_params(row))

    def matched_elsewhere(self, statement_id: str) -> set[str]:
        rows = self._run(
            "SELECT matched_expense_id FROM assaf_os.os_statement_lines "
            "WHERE workspace_id = :ws AND state = 'active' AND matched_expense_id IS NOT NULL "
            "AND statement_id <> :id", id=statement_id).all()
        return {r.matched_expense_id for r in rows}

    # pending batch detail
    def batch_lines(self, proposal_id: str) -> list[BatchLineRow]:
        rows = self._run(
            "SELECT proposal_id, line_key, occurrence_index, detail, purged_at "
            "FROM assaf_os.os_statement_batch_lines WHERE workspace_id = :ws "
            "AND proposal_id = :id ORDER BY occurrence_index, line_key", id=proposal_id).all()
        return [BatchLineRow(r.proposal_id, _text(r.line_key), int(r.occurrence_index),
                             r.detail, r.purged_at) for r in rows]

    def live_batch_line_count(self, proposal_id: str) -> int:
        return int(self._run(
            "SELECT count(*) FROM assaf_os.os_statement_batch_lines WHERE workspace_id = :ws "
            "AND proposal_id = :id AND detail IS NOT NULL", id=proposal_id).scalar_one())

    def batch_line_count(self, proposal_id: str) -> int:
        return int(self._run(
            "SELECT count(*) FROM assaf_os.os_statement_batch_lines WHERE workspace_id = :ws "
            "AND proposal_id = :id", id=proposal_id).scalar_one())

    def insert_batch_lines(self, rows: Sequence[BatchLineRow]) -> None:
        if not rows:
            return
        self._c.execute(
            text("INSERT INTO assaf_os.os_statement_batch_lines (workspace_id, proposal_id, "
                 "line_key, occurrence_index, detail, purged_at, created_at, updated_at) VALUES "
                 "(:ws, :proposal_id, :line_key, :occurrence_index, CAST(:detail AS jsonb), "
                 ":purged_at, :now, :now)"),
            [{"ws": self._ws, "proposal_id": r.proposal_id, "line_key": r.line_key,
              "occurrence_index": r.occurrence_index,
              "detail": None if r.detail is None else _json(r.detail),
              "purged_at": r.purged_at, "now": self._now} for r in rows])

    def purge_batch_lines(self, proposal_id: str) -> int:
        return int(self._run(
            "UPDATE assaf_os.os_statement_batch_lines SET detail = NULL, purged_at = :now "
            "WHERE workspace_id = :ws AND proposal_id = :id AND detail IS NOT NULL",
            id=proposal_id, now=self._now).rowcount)

    # rules
    @staticmethod
    def _rule(row: Any) -> RuleRow:
        return RuleRow(
            row.rule_id, row.tag, row.match, row.merchant_key, _text(row.merchant_key_hmac),
            row.category, row.path, row.platform, row.is_subscription, row.label,
            tuple(row.aliases or ()), bool(row.active), int(row.revision), row.source_proposal_id)

    def rules(self) -> list[RuleRow]:
        rows = self._run(
            f"SELECT {_RULE_COLUMNS} FROM assaf_os.os_merchant_rules WHERE workspace_id = :ws "
            "ORDER BY rule_id").all()
        return [self._rule(r) for r in rows]

    @staticmethod
    def _rule_params(row: RuleRow) -> dict[str, Any]:
        params = dict(vars(row))
        params["aliases"] = _json(list(row.aliases))
        return params

    def insert_rule(self, row: RuleRow) -> None:
        self._run(
            "INSERT INTO assaf_os.os_merchant_rules (workspace_id, rule_id, tag, match, "
            "merchant_key, merchant_key_hmac, category, path, platform, is_subscription, label, "
            "aliases, active, revision, source_proposal_id, created_at, updated_at) VALUES "
            "(:ws, :rule_id, :tag, :match, :merchant_key, :merchant_key_hmac, :category, :path, "
            ":platform, :is_subscription, :label, CAST(:aliases AS jsonb), :active, :revision, "
            ":source_proposal_id, :now, :now)", now=self._now, **self._rule_params(row))

    def update_rule(self, row: RuleRow) -> None:
        self._run(
            "UPDATE assaf_os.os_merchant_rules SET tag = :tag, match = :match, "
            "merchant_key = :merchant_key, merchant_key_hmac = :merchant_key_hmac, "
            "category = :category, path = :path, platform = :platform, "
            "is_subscription = :is_subscription, label = :label, "
            "aliases = CAST(:aliases AS jsonb), active = :active, revision = :revision, "
            "source_proposal_id = :source_proposal_id, updated_at = :now "
            "WHERE workspace_id = :ws AND rule_id = :rule_id",
            now=self._now, **self._rule_params(row))

    # drafts and the registry
    @staticmethod
    def _draft(row: Any) -> DraftRow:
        choices = dict(row.choices or {})
        index = choices.pop(INDEX_KEY, {})
        return DraftRow(row.proposal_id, int(row.draft_revision), dict(row.overrides or {}),
                        choices, index.get("statement_id", ""), index.get("credential_id"),
                        index.get("source", ""), bool(index.get("open", False)))

    def draft(self, proposal_id: str) -> DraftRow | None:
        row = self._run(
            "SELECT proposal_id, draft_revision, overrides, choices FROM "
            "assaf_os.os_statement_review_drafts WHERE workspace_id = :ws AND proposal_id = :id",
            id=proposal_id).first()
        return None if row is None else self._draft(row)

    def put_draft(self, row: DraftRow) -> None:
        choices = dict(row.choices)
        choices[INDEX_KEY] = {"statement_id": row.statement_id, "credential_id": row.credential_id,
                              "source": row.source, "open": row.open}
        self._run(
            "INSERT INTO assaf_os.os_statement_review_drafts (workspace_id, proposal_id, "
            "draft_revision, overrides, choices, created_at, updated_at) VALUES (:ws, :id, :rev, "
            "CAST(:overrides AS jsonb), CAST(:choices AS jsonb), :now, :now) "
            "ON CONFLICT (workspace_id, proposal_id) DO UPDATE SET "
            "draft_revision = EXCLUDED.draft_revision, overrides = EXCLUDED.overrides, "
            "choices = EXCLUDED.choices, updated_at = EXCLUDED.updated_at",
            id=row.proposal_id, rev=row.draft_revision, overrides=_json(row.overrides),
            choices=_json(choices), now=self._now)

    def open_drafts(self, *, statement_id: str | None = None,
                    credential_id: str | None = None) -> list[DraftRow]:
        clauses = ["workspace_id = :ws", "choices #>> '{index,open}' = 'true'"]
        params: dict[str, Any] = {}
        if statement_id is not None:
            clauses.append("choices #>> '{index,statement_id}' = :statement_id")
            params["statement_id"] = statement_id
        if credential_id is not None:
            clauses.append("choices #>> '{index,credential_id}' = :credential_id")
            params["credential_id"] = credential_id
        rows = self._run(
            "SELECT proposal_id, draft_revision, overrides, choices FROM "
            "assaf_os.os_statement_review_drafts WHERE " + " AND ".join(clauses)
            + " ORDER BY proposal_id", **params).all()
        return [self._draft(r) for r in rows]

    def credential(self, credential_id: str) -> CredentialRow | None:
        row = self._run(
            "SELECT credential_id, owner_id, epoch, label, kind, created_at, expires_at, "
            "revoked_at FROM assaf_os.os_assistant_credentials WHERE workspace_id = :ws "
            "AND credential_id = :id", id=credential_id).first()
        if row is None:
            return None
        return CredentialRow(row.credential_id, str(row.owner_id), int(row.epoch), row.label,
                             row.kind, row.created_at, row.expires_at, row.revoked_at)

    def dump(self) -> dict[str, dict[Any, Any]]:
        """Every row of every table, keyed like the in-memory layout (tests, privacy greps)."""
        statements = {r.statement_id: r for r in self.statements()}
        lines = {(r.statement_id, r.line_key): r
                 for rows in self.lines_of(list(statements)).values() for r in rows}
        batch_rows = self._run(
            "SELECT proposal_id, line_key, occurrence_index, detail, purged_at "
            "FROM assaf_os.os_statement_batch_lines WHERE workspace_id = :ws").all()
        batch_lines = {(r.proposal_id, _text(r.line_key)): BatchLineRow(
            r.proposal_id, _text(r.line_key), int(r.occurrence_index), r.detail, r.purged_at)
            for r in batch_rows}
        draft_rows = self._run(
            "SELECT proposal_id, draft_revision, overrides, choices FROM "
            "assaf_os.os_statement_review_drafts WHERE workspace_id = :ws").all()
        credential_rows = self._run(
            "SELECT credential_id FROM assaf_os.os_assistant_credentials "
            "WHERE workspace_id = :ws").all()
        return {
            "statements": statements, "lines": lines, "batch_lines": batch_lines,
            "rules": {r.rule_id: r for r in self.rules()},
            "drafts": {r.proposal_id: self._draft(r) for r in draft_rows},
            "credentials": {r.credential_id: self.credential(r.credential_id)
                            for r in credential_rows},
        }
