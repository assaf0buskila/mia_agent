"""The 'statement-batch.v1' input: strict validation, canonical order, keyed line identity.

Shape (all keys required unless marked optional; unknown keys are rejected everywhere):

    {
      "schema": "statement-batch.v1",
      "client_batch_id": "<lowercase canonical uuid>",
      "account_label": "<= 40 chars, no digit runs>",
      "charge_date": "YYYY-MM-DD",
      "lines": [ <= 400 of
        {
          "txn_date": "YYYY-MM-DD", "merchant_display": "<= 80 chars",
          "amount_minor": <int > 0>, "kind": "charge" | "credit", "currency": "ILS",
          "confidence": <0..1>, "suggested_tag": "business" | "personal" | null,
          optional: "original_amount_minor" + "original_currency" (together),
                    "installment": {"n": <int>, "of": <int>},
                    "suggested_category" (<= 40 chars), "suggested_path", "is_subscription"
        } ]
    }

There is no free-text note or instruction field, so a line has nowhere to carry a sentence the
system could mistake for a command. Every string is data: cleaned, bounded, keyed.

This is the whole v1 contract, and it is narrower than some earlier drafts on purpose: no
merchant_raw and no note field (both are free text), merchant_display is capped at 80 characters
(the only merchant text that is kept), and the card label is called account_label. The batch is
at most 400 lines and 256 KiB of UTF-8 JSON, whatever the whitespace or escaping.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .errors import IssueCollector, StatementValidationError
from .identity import (
    LineFacts,
    cycle_of,
    line_key,
    parse_iso_date,
    require_pepper,
    require_workspace,
    statement_id,
)
from .text import check_text, normalize_merchant
from .vocabulary import (
    CURRENCY,
    KINDS,
    MAX_ACCOUNT_LABEL_CHARS,
    MAX_AMOUNT_MINOR,
    MAX_BATCH_BYTES,
    MAX_CATEGORY_CHARS,
    MAX_DISPLAY_CHARS,
    MAX_KEY_CHARS,
    MAX_LINES,
    PATHS,
    SCHEMA_VERSION,
    TAGS,
)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_FOREIGN_CURRENCY = re.compile(r"[A-Z]{3}")
_TOP_FIELDS = frozenset({"schema", "client_batch_id", "account_label", "charge_date", "lines"})
_LINE_REQUIRED = (
    "txn_date",
    "merchant_display",
    "amount_minor",
    "kind",
    "currency",
    "confidence",
    "suggested_tag",
)
_LINE_OPTIONAL = (
    "original_amount_minor",
    "original_currency",
    "installment",
    "suggested_category",
    "suggested_path",
    "is_subscription",
)
_LINE_FIELDS = frozenset((*_LINE_REQUIRED, *_LINE_OPTIONAL))
_MAX_INSTALLMENTS = 120


@dataclass(frozen=True)
class BatchLine:
    """One validated line with its keyed identity and the assistant's suggestions."""

    line_key: str
    statement_id: str  # which statement this key belongs to: a diff refuses a foreign line
    occurrence_index: int
    facts: LineFacts
    suggested_tag: str | None
    suggested_category: str | None = field(repr=False)
    suggested_path: str | None
    is_subscription: bool | None
    confidence: float


@dataclass(frozen=True)
class StatementBatch:
    client_batch_id: str
    account_label: str = field(repr=False)
    charge_date: date
    cycle: str
    statement_id: str
    cycle_from: date
    cycle_to: date
    lines: tuple[BatchLine, ...]
    digest: str


class _DuplicateKey(Exception):
    pass


class _NonFinite(Exception):
    pass


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey
        out[key] = value
    return out


def _reject_constant(_name: str) -> Any:
    raise _NonFinite


def _load(raw: object, issues: IssueCollector) -> dict[str, Any] | None:
    """Turn a JSON text, bytes or an already-parsed mapping into a mapping, size-checked first."""
    text: str
    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > MAX_BATCH_BYTES:
            issues.add("", "batch_too_large")
            return None
        try:
            text = bytes(raw).decode("utf-8-sig")
        except UnicodeDecodeError:
            issues.add("", "invalid_encoding")
            return None
    elif isinstance(raw, str):
        if len(raw) > MAX_BATCH_BYTES:
            issues.add("", "batch_too_large")
            return None
        try:
            size = len(raw.encode("utf-8"))
        except UnicodeEncodeError:
            issues.add("", "invalid_encoding")
            return None
        if size > MAX_BATCH_BYTES:
            issues.add("", "batch_too_large")
            return None
        text = raw
    elif isinstance(raw, dict):
        try:
            size = len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError, RecursionError):
            issues.add("", "not_json_value")
            return None
        if size > MAX_BATCH_BYTES:
            issues.add("", "batch_too_large")
            return None
        return raw
    else:
        issues.add("", "invalid_type")
        return None
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except _DuplicateKey:
        issues.add("", "duplicate_key")
        return None
    except _NonFinite:
        issues.add("", "non_finite_number")
        return None
    except (ValueError, RecursionError):
        issues.add("", "invalid_json")
        return None
    if not isinstance(data, dict):
        issues.add("", "not_an_object")
        return None
    return data


def _is_positive_amount(value: object) -> bool:
    return type(value) is int and 0 < value <= MAX_AMOUNT_MINOR


@dataclass(frozen=True)
class _Draft:
    facts: LineFacts
    tag: str | None
    category: str | None
    path: str | None
    subscription: bool | None
    confidence: float


def _parse_installment(value: object) -> tuple[int, int] | None:
    if not isinstance(value, dict) or set(value) != {"n", "of"}:
        return None
    n, total = value["n"], value["of"]
    if type(n) is not int or type(total) is not int:
        return None
    if not 1 <= n <= total <= _MAX_INSTALLMENTS:
        return None
    return n, total


def _parse_line(
    index: int, item: object, charge_day: date | None, issues: IssueCollector
) -> _Draft | None:
    """Validate one line field by field. None means this line has at least one problem."""
    where = f"lines[{index}]"
    before = len(issues)
    if not isinstance(item, dict):
        issues.add(where, "invalid_line")
        return None
    if any(key not in _LINE_FIELDS for key in item):
        issues.add(where, "unknown_field")
    for name in _LINE_REQUIRED:
        if name not in item:
            issues.add(f"{where}.{name}", "missing_field")

    txn_date = None
    if "txn_date" in item:
        txn_date = parse_iso_date(item["txn_date"], allow_date=False)
        if txn_date is None:
            issues.add(f"{where}.txn_date", "invalid_date")
        elif charge_day is not None and txn_date > charge_day:
            issues.add(f"{where}.txn_date", "after_charge_date")

    display = None
    if "merchant_display" in item:
        display = check_text(
            item["merchant_display"],
            path=f"{where}.merchant_display",
            max_chars=MAX_DISPLAY_CHARS,
            issues=issues,
        )
    merchant_key = ""
    if display is not None:
        merchant_key = normalize_merchant(display)
        if not merchant_key or len(merchant_key) > MAX_KEY_CHARS:
            issues.add(f"{where}.merchant_display", "unusable_merchant")

    amount = item.get("amount_minor")
    if "amount_minor" in item and not _is_positive_amount(amount):
        issues.add(f"{where}.amount_minor", "invalid_amount")
    kind = item.get("kind")
    if "kind" in item and kind not in KINDS:
        issues.add(f"{where}.kind", "invalid_kind")
    if "currency" in item and item["currency"] != CURRENCY:
        issues.add(f"{where}.currency", "invalid_currency")

    original_amount = item.get("original_amount_minor")
    original_currency = item.get("original_currency")
    if (original_amount is None) != (original_currency is None):
        issues.add(where, "incomplete_original_amount")
    elif original_amount is not None:
        if not _is_positive_amount(original_amount):
            issues.add(f"{where}.original_amount_minor", "invalid_amount")
        if (
            not isinstance(original_currency, str)
            or not _FOREIGN_CURRENCY.fullmatch(original_currency)
            or original_currency == CURRENCY
        ):
            issues.add(f"{where}.original_currency", "invalid_currency")

    installment = None
    if item.get("installment") is not None:
        installment = _parse_installment(item["installment"])
        if installment is None:
            issues.add(f"{where}.installment", "invalid_installment")

    tag = item.get("suggested_tag")
    if tag is not None and tag not in TAGS:
        issues.add(f"{where}.suggested_tag", "invalid_tag")
    path = item.get("suggested_path")
    if path is not None and path not in PATHS:
        issues.add(f"{where}.suggested_path", "invalid_path")
    subscription = item.get("is_subscription")
    if subscription is not None and not isinstance(subscription, bool):
        issues.add(f"{where}.is_subscription", "invalid_flag")
    category = None
    if item.get("suggested_category") is not None:
        category = check_text(
            item["suggested_category"],
            path=f"{where}.suggested_category",
            max_chars=MAX_CATEGORY_CHARS,
            issues=issues,
            allow_empty=True,
        )
        category = category or None

    confidence = item.get("confidence")
    if "confidence" in item and not (type(confidence) in (int, float) and 0.0 <= confidence <= 1.0):
        issues.add(f"{where}.confidence", "invalid_confidence")

    if len(issues) != before or txn_date is None or display is None:
        return None
    facts = LineFacts(
        txn_date=txn_date,
        merchant_display=display,
        merchant_key=merchant_key,
        amount_minor=amount,
        kind=kind,
        currency=CURRENCY,
        original_amount_minor=original_amount,
        original_currency=original_currency,
        installment_n=installment[0] if installment else None,
        installment_of=installment[1] if installment else None,
    )
    return _Draft(facts, tag, category, path, subscription, float(confidence))


def _group_key(facts: LineFacts) -> tuple[Any, ...]:
    return (
        facts.txn_date,
        facts.merchant_key,
        facts.kind,
        facts.amount_minor,
        facts.original_amount_minor or 0,
        facts.original_currency or "",
        facts.installment_n or 0,
        facts.installment_of or 0,
    )


def _tiebreak(draft: _Draft) -> tuple[Any, ...]:
    return (
        draft.facts.merchant_display,
        draft.tag or "",
        draft.category or "",
        draft.path or "",
        -1 if draft.subscription is None else int(draft.subscription),
        draft.confidence,
    )


def parse_statement_batch(raw: object, *, pepper: bytes, workspace: str) -> StatementBatch:
    """Validate and canonicalize a batch. Raises StatementValidationError (codes, no values)."""
    secret = require_pepper(pepper)
    scope = require_workspace(workspace)
    issues = IssueCollector()
    data = _load(raw, issues)
    if data is None:
        issues.raise_if_any()
        raise StatementValidationError.of("invalid_json")  # unreachable: _load recorded why

    if any(key not in _TOP_FIELDS for key in data):
        issues.add("", "unknown_field")
    if "schema" not in data:
        issues.add("schema", "missing_field")
    elif data["schema"] != SCHEMA_VERSION:
        issues.add("schema", "unsupported_schema")
    batch_id = data.get("client_batch_id")
    if "client_batch_id" not in data:
        issues.add("client_batch_id", "missing_field")
    elif not isinstance(batch_id, str) or not _UUID.fullmatch(batch_id):
        issues.add("client_batch_id", "invalid_uuid")
    label = None
    if "account_label" not in data:
        issues.add("account_label", "missing_field")
    else:
        label = check_text(
            data["account_label"],
            path="account_label",
            max_chars=MAX_ACCOUNT_LABEL_CHARS,
            issues=issues,
        )
    charge_day = None
    if "charge_date" not in data:
        issues.add("charge_date", "missing_field")
    else:
        charge_day = parse_iso_date(data["charge_date"], allow_date=False)
        if charge_day is None:
            issues.add("charge_date", "invalid_date")

    items: Sequence[object] = ()
    if "lines" not in data:
        issues.add("lines", "missing_field")
    elif not isinstance(data["lines"], (list, tuple)) or not data["lines"]:
        issues.add("lines", "invalid_lines")
    elif len(data["lines"]) > MAX_LINES:
        issues.add("lines", "too_many_lines")
    else:
        items = data["lines"]

    drafts = [_parse_line(index, item, charge_day, issues) for index, item in enumerate(items)]
    issues.raise_if_any()
    built = [draft for draft in drafts if draft is not None]
    if charge_day is None or label is None or len(built) != len(drafts):
        raise StatementValidationError.of("invalid_batch")  # unreachable: issues were recorded

    statement = statement_id(secret, scope, label, charge_day)
    grouped: dict[tuple[Any, ...], list[_Draft]] = {}
    for draft in built:
        grouped.setdefault(_group_key(draft.facts), []).append(draft)
    lines: list[BatchLine] = []
    for group in grouped.values():
        for occurrence, draft in enumerate(sorted(group, key=_tiebreak)):
            lines.append(
                BatchLine(
                    line_key=line_key(secret, statement, draft.facts, occurrence),
                    statement_id=statement,
                    occurrence_index=occurrence,
                    facts=draft.facts,
                    suggested_tag=draft.tag,
                    suggested_category=draft.category,
                    suggested_path=draft.path,
                    is_subscription=draft.subscription,
                    confidence=draft.confidence,
                )
            )
    lines.sort(key=lambda line: (*_group_key(line.facts), line.occurrence_index))
    cycle_dates = [line.facts.txn_date for line in lines]
    digest_body = json.dumps(
        {
            "statement_id": statement,
            "lines": [
                [
                    line.line_key,
                    line.facts.merchant_display,
                    line.suggested_tag,
                    line.suggested_category,
                    line.suggested_path,
                    line.is_subscription,
                ]
                for line in lines
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hmac.new(
        secret, b"batch-digest\x1f" + digest_body.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return StatementBatch(
        client_batch_id=str(batch_id),
        account_label=label,
        charge_date=charge_day,
        cycle=cycle_of(charge_day),
        statement_id=statement,
        cycle_from=min(cycle_dates),
        cycle_to=max(cycle_dates),
        lines=tuple(lines),
        digest=digest,
    )
