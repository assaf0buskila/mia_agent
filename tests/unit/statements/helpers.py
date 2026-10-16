"""Shared builders for the statement-domain tests. Synthetic data only."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from app.domain.statements import (
    ManualExpense,
    MerchantRule,
    OwnerOverride,
    ResolvedLine,
    ReviewChoices,
    StatementBatch,
    StatementDiff,
    StoredLine,
    apply_rules,
    diff_statement,
    parse_statement_batch,
)

PEPPER = b"synthetic-test-pepper-0123456789abcdef"
OTHER_PEPPER = b"another-synthetic-pepper-fedcba9876543210"
WORKSPACE = "ws_synthetic"
CHARGE_DATE = "2026-10-15"
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "statements" / "synthetic_cycle.json"


def fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


DROP = object()  # pass DROP as a value to remove that key from the built dict


def raw_line(**overrides: Any) -> dict[str, Any]:
    line: dict[str, Any] = {
        "txn_date": "2026-09-20",
        "merchant_display": "DEMO SHOP",
        "amount_minor": 10000,
        "kind": "charge",
        "currency": "ILS",
        "suggested_tag": "business",
        "confidence": 0.9,
    }
    line.update(overrides)
    return {key: value for key, value in line.items() if value is not DROP}


def raw_batch(lines: Sequence[Mapping[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    batch: dict[str, Any] = {
        "schema": "statement-batch.v1",
        "client_batch_id": "0b6f7a52-3c1d-4e8a-9f20-5d1c7e4b9a01",
        "account_label": "Synthetic Business Card",
        "charge_date": CHARGE_DATE,
        "lines": [copy.deepcopy(dict(line)) for line in (lines or [raw_line()])],
    }
    batch.update(overrides)
    return {key: value for key, value in batch.items() if value is not DROP}


def parse(raw: Any, *, pepper: bytes = PEPPER, workspace: str = WORKSPACE) -> StatementBatch:
    return parse_statement_batch(raw, pepper=pepper, workspace=workspace)


def rule(**fields: Any) -> MerchantRule:
    data = dict(fields)
    if "aliases" in data:
        data["aliases"] = tuple(data["aliases"])
    return MerchantRule(**data)


def fixture_rules() -> list[MerchantRule]:
    return [rule(**item) for item in fixture()["rules"]]


def manual_expense(**fields: Any) -> ManualExpense:
    data = dict(fields)
    data["date"] = date.fromisoformat(data["date"])
    return ManualExpense(**data)


def fixture_manual_expenses() -> list[ManualExpense]:
    return [manual_expense(**item) for item in fixture()["manual_expenses"]]


@dataclass(frozen=True)
class Imported:
    batch: StatementBatch
    resolved: tuple[ResolvedLine, ...]
    diff: StatementDiff

    def key(self, merchant_display: str, nth: int = 0) -> str:
        """line_key of the nth line whose normalized display equals merchant_display."""
        wanted = " ".join(merchant_display.split())
        keys = [x.line_key for x in self.batch.lines if x.facts.merchant_display == wanted]
        return keys[nth]


def run_import(
    raw: Any,
    *,
    previous: Iterable[StoredLine] = (),
    rules: Sequence[MerchantRule] = (),
    overrides: Mapping[str, OwnerOverride] | None = None,
    candidates: Sequence[ManualExpense] = (),
    choices: ReviewChoices | None = None,
    minimize_personal: bool = True,
    date_basis: str = "charge_date",
) -> Imported:
    batch = parse(raw)
    resolved = apply_rules(batch.lines, rules, overrides)
    diff = diff_statement(
        tuple(previous),
        resolved,
        statement_id=batch.statement_id,
        charge_date=batch.charge_date,
        manual_candidates=tuple(candidates),
        rules=tuple(rules),
        choices=choices,
        date_basis=date_basis,  # type: ignore[arg-type]
        minimize_personal=minimize_personal,
    )
    return Imported(batch, resolved, diff)


def commit_rows(diff: StatementDiff) -> list[StoredLine]:
    """Emulate the committer: expense ids appear only after the write happened."""
    rows: list[StoredLine] = []
    created = {op.line_key: op for op in diff.ops if op.op == "create"}
    touched = {op.line_key: op for op in diff.ops if op.op in {"update", "revive"}}
    for row in diff.next_lines:
        if row.line_key in created:
            row = replace(
                row,
                expense_id=f"exp-{row.line_key[:10]}",
                expense_source_key=created[row.line_key].source_key,
                expense_revision=1,
                imported_expense_revision=1,
            )
        elif row.line_key in touched:
            nxt = (row.expense_revision or 1) + 1
            row = replace(row, expense_revision=nxt, imported_expense_revision=nxt)
        rows.append(row)
    return rows


def ops_of(diff: StatementDiff, op: str) -> list[Any]:
    return [x for x in diff.ops if x.op == op]


def entry_of(diff: StatementDiff, line_key: str) -> Any:
    matches = [entry for entry in diff.entries if entry.line_key == line_key]
    assert len(matches) == 1, f"expected exactly one entry for {line_key[:8]}, got {len(matches)}"
    return matches[0]
