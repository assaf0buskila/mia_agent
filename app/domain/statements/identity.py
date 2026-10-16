"""Billing-cycle identity and deterministic, keyed line identity.

A statement is identified by (workspace, account label, charge date): the date printed on the
statement, not a calendar month and not an assumed 15th. A line is identified by a keyed HMAC of
its facts plus an occurrence index, so two identical same-day charges stay distinct and a
re-import of an unchanged statement yields identical keys.

The pepper is injected. Without it a stored key-only personal row cannot be brute-forced back to
a merchant and an amount.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import date, datetime

from .errors import IssueCollector, StatementValidationError
from .text import check_text, clean_text, identity_text, normalize_merchant
from .vocabulary import (
    CURRENCY,
    KINDS,
    MAX_ACCOUNT_LABEL_CHARS,
    MAX_AMOUNT_MINOR,
    MAX_DISPLAY_CHARS,
    MAX_KEY_CHARS,
    MIN_PEPPER_BYTES,
)

_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_STATEMENT_ID = re.compile(r"STM_[0-9a-f]{32}")
_LINE_KEY = re.compile(r"[0-9a-f]{64}")
_CYCLE = re.compile(r"[0-9]{4}-[0-9]{2}")
_MIN_YEAR = 2000
_MAX_YEAR = 2100
_MAX_WORKSPACE_CHARS = 64
_MAX_OCCURRENCE = 10_000


def parse_iso_date(value: object, *, allow_date: bool = True) -> date | None:
    """Strict YYYY-MM-DD (ASCII digits, no shortcuts). None when it is anything else."""
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        parsed: date | None = value if allow_date else None
    elif isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed is None or not _MIN_YEAR <= parsed.year <= _MAX_YEAR:
        return None
    return parsed


def cycle_of(charge_date: date | str) -> str:
    """The billing cycle label: the YYYY-MM of the charge date, not of the transactions."""
    parsed = parse_iso_date(charge_date)
    if parsed is None:
        raise StatementValidationError.of("invalid_date", "charge_date")
    return f"{parsed.year:04d}-{parsed.month:02d}"


def is_cycle_label(value: object) -> bool:
    if not isinstance(value, str) or not _CYCLE.fullmatch(value):
        return False
    return parse_iso_date(f"{value}-01") is not None


def is_statement_id(value: object) -> bool:
    return isinstance(value, str) and _STATEMENT_ID.fullmatch(value) is not None


def is_line_key(value: object) -> bool:
    return isinstance(value, str) and _LINE_KEY.fullmatch(value) is not None


def require_pepper(pepper: object) -> bytes:
    if not isinstance(pepper, (bytes, bytearray)) or len(pepper) < MIN_PEPPER_BYTES:
        raise StatementValidationError.of("pepper_invalid")
    return bytes(pepper)


def require_workspace(workspace: object) -> str:
    if (
        not isinstance(workspace, str)
        or not 0 < len(workspace) <= _MAX_WORKSPACE_CHARS
        or workspace != clean_text(workspace)
    ):
        raise StatementValidationError.of("workspace_invalid")
    return workspace


def _is_amount(value: object) -> bool:
    return type(value) is int and 0 < value <= MAX_AMOUNT_MINOR


@dataclass(frozen=True)
class LineFacts:
    """What the bank printed for one line. Immutable, and the only source of a line's key."""

    txn_date: date
    merchant_display: str
    merchant_key: str
    amount_minor: int
    kind: str
    currency: str
    original_amount_minor: int | None = None
    original_currency: str | None = None
    installment_n: int | None = None
    installment_of: int | None = None

    def __post_init__(self) -> None:
        problems = IssueCollector()
        if not isinstance(self.txn_date, date) or parse_iso_date(self.txn_date) is None:
            problems.add("txn_date", "invalid_date")
        if (
            not isinstance(self.merchant_display, str)
            or not 0 < len(self.merchant_display) <= MAX_DISPLAY_CHARS
            or self.merchant_display != clean_text(self.merchant_display)
        ):
            problems.add("merchant_display", "invalid_text")
        if (
            not isinstance(self.merchant_key, str)
            or not 0 < len(self.merchant_key) <= MAX_KEY_CHARS
            or self.merchant_key != normalize_merchant(self.merchant_key)
        ):
            problems.add("merchant_key", "invalid_text")
        if not _is_amount(self.amount_minor):
            problems.add("amount_minor", "invalid_amount")
        if self.kind not in KINDS:
            problems.add("kind", "invalid_kind")
        if self.currency != CURRENCY:
            problems.add("currency", "invalid_currency")
        if (self.original_amount_minor is None) != (self.original_currency is None):
            problems.add("original_amount_minor", "incomplete_original_amount")
        elif self.original_amount_minor is not None and (
            not _is_amount(self.original_amount_minor)
            or not isinstance(self.original_currency, str)
            or not re.fullmatch(r"[A-Z]{3}", self.original_currency)
            or self.original_currency == CURRENCY
        ):
            problems.add("original_amount_minor", "invalid_original_amount")
        if (self.installment_n is None) != (self.installment_of is None):
            problems.add("installment", "invalid_installment")
        elif self.installment_n is not None and not (
            type(self.installment_n) is int
            and type(self.installment_of) is int
            and 1 <= self.installment_n <= self.installment_of <= 120
        ):
            problems.add("installment", "invalid_installment")
        problems.raise_if_any()


def _optional(value: object) -> str:
    return "" if value is None else str(value)


def statement_id(pepper: bytes, workspace: str, account_label: str, charge_date: date | str) -> str:
    """'STM_' plus 128 bits of a keyed digest of (workspace, account label, charge date)."""
    secret = require_pepper(pepper)
    scope = require_workspace(workspace)
    issues = IssueCollector()
    label = check_text(
        account_label, path="account_label", max_chars=MAX_ACCOUNT_LABEL_CHARS, issues=issues
    )
    issues.raise_if_any()
    day = parse_iso_date(charge_date)
    if day is None or label is None:
        raise StatementValidationError.of("invalid_date", "charge_date")
    message = "\x1f".join(("statement", scope, identity_text(label), day.isoformat()))
    digest = hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()
    return "STM_" + digest[:32]


def line_key(pepper: bytes, statement: str, facts: LineFacts, occurrence_index: int) -> str:
    """A 64-hex keyed digest of the line's facts; the display text is deliberately not in it."""
    secret = require_pepper(pepper)
    if not is_statement_id(statement):
        raise StatementValidationError.of("statement_id_invalid")
    if type(occurrence_index) is not int or not 0 <= occurrence_index < _MAX_OCCURRENCE:
        raise StatementValidationError.of("occurrence_invalid")
    message = "\x1f".join(
        (
            "line",
            statement,
            facts.txn_date.isoformat(),
            facts.merchant_key,
            str(facts.amount_minor),
            facts.currency,
            facts.kind,
            _optional(facts.original_amount_minor),
            _optional(facts.original_currency),
            _optional(facts.installment_n),
            _optional(facts.installment_of),
            str(occurrence_index),
        )
    )
    return hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()
