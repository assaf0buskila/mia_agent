"""Mapping an active business charge to the frozen Assaf OS expense input.

The seven-entity schema does not change. A statement-created expense is recognised only by its
source key, 'stmt:<statement_id>:<line_key>', and that prefix is the only thing the replace
logic ever touches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .errors import StatementValidationError
from .identity import LineFacts, is_line_key, is_statement_id
from .text import clean_text
from .vocabulary import (
    DEFAULT_CATEGORY,
    MAX_CATEGORY_CHARS,
    PATHS,
    PAYMENT_STATUS_PAID,
    PLATFORMS,
    SOURCE_TYPE_ACTUAL_PAYMENT,
)

_DATE_BASES = ("charge_date", "txn_date")
_LINE_KEY_TAIL = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Classification:
    """How a business line is booked. This is all the detail a business row keeps beyond its
    facts."""

    category: str = field(repr=False)
    path: str
    platform: str
    is_subscription: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.category, str)
            or not 0 < len(self.category) <= MAX_CATEGORY_CHARS
            or self.category != clean_text(self.category)
        ):
            raise StatementValidationError.of("invalid_classification", "category")
        if self.path not in PATHS:
            raise StatementValidationError.of("invalid_classification", "path")
        if self.platform not in PLATFORMS:
            raise StatementValidationError.of("invalid_classification", "platform")
        if not isinstance(self.is_subscription, bool):
            raise StatementValidationError.of("invalid_classification", "is_subscription")


def default_classification() -> Classification:
    return Classification(DEFAULT_CATEGORY, PATHS[-1], PLATFORMS[-1], False)


def statement_source_key(statement: str, line_key: str) -> str:
    if not is_statement_id(statement) or not is_line_key(line_key):
        raise StatementValidationError.of("invalid_source_key")
    return f"stmt:{statement}:{line_key}"


def owns_source_key(statement: str, source_key: object) -> bool:
    """True only for exactly 'stmt:<this statement_id>:<64-hex line key>'."""
    if not is_statement_id(statement) or not isinstance(source_key, str):
        return False
    prefix = f"stmt:{statement}:"
    return source_key.startswith(prefix) and bool(
        _LINE_KEY_TAIL.fullmatch(source_key[len(prefix) :])
    )


def is_statement_source_key(source_key: object) -> bool:
    """Any statement-created expense, whichever statement made it."""
    return isinstance(source_key, str) and source_key.startswith("stmt:")


@dataclass(frozen=True)
class ExpenseDraft:
    """The expense input for one line, in minor units. Foreign amounts stay line metadata."""

    date: date
    category: str = field(repr=False)
    platform: str
    path: str
    vendor: str = field(repr=False)
    amount_ils: int = field(repr=False)
    currency: str
    payment_status: str
    source_type: str
    source_key: str

    def as_input(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "category": self.category,
            "platform": self.platform,
            "path": self.path,
            "vendor": self.vendor,
            "amount_ils": self.amount_ils,
            "currency": self.currency,
            "payment_status": self.payment_status,
            "source_type": self.source_type,
            "source_key": self.source_key,
        }


def map_to_expense(
    statement: str,
    line_key: str,
    facts: LineFacts,
    classification: Classification,
    *,
    charge_date: date,
    date_basis: str = "charge_date",
) -> ExpenseDraft:
    """The paid ('תשלום בפועל') expense for a business charge.

    Credits are never posted: an expense amount must be positive, so a credit stays on the
    statement and reduces its total only.
    """
    if facts.kind != "charge":
        raise StatementValidationError.of("credit_not_postable", "kind")
    if date_basis not in _DATE_BASES:
        raise StatementValidationError.of("date_basis_invalid", "date_basis")
    if not isinstance(charge_date, date) or isinstance(charge_date, datetime):
        raise StatementValidationError.of("invalid_date", "charge_date")
    return ExpenseDraft(
        date=charge_date if date_basis == "charge_date" else facts.txn_date,
        category=classification.category,
        platform=classification.platform,
        path=classification.path,
        vendor=facts.merchant_display,
        amount_ils=facts.amount_minor,
        currency=facts.currency,
        payment_status=PAYMENT_STATUS_PAID,
        source_type=SOURCE_TYPE_ACTUAL_PAYMENT,
        source_key=statement_source_key(statement, line_key),
    )
