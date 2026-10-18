"""Statement domain: pure logic for importing a card statement into Assaf OS.

No database, routes, migrations, clock, randomness, files or network live here. Callers inject
everything (the HMAC pepper, the workspace, the stored rows, the rules, the hand-entered
expenses) and receive plain immutable data back: a parsed batch, a diff, expense drafts, totals.
Persisting any of it is a separate, approved step.
"""

from __future__ import annotations

from .batch import BatchLine, StatementBatch, parse_statement_batch
from .diff import (
    ACTIONS,
    BLOCK_MERGE_CHOICE,
    BLOCK_UNASSIGNED,
    LINKED_EXPENSE_CHANGED,
    DiffEntry,
    ExpenseOp,
    ReviewChoices,
    StatementDiff,
    StoredLine,
    diff_statement,
)
from .errors import MAX_ISSUES, Issue, StatementValidationError
from .expense import (
    Classification,
    ExpenseDraft,
    is_statement_source_key,
    map_to_expense,
    owns_source_key,
    statement_source_key,
)
from .identity import LineFacts, cycle_of, line_key, parse_iso_date, statement_id
from .merge import (
    ManualExpense,
    MatchResult,
    MergeItem,
    match_batch,
    match_manual,
    vendor_matches,
)
from .rules import (
    MerchantRule,
    OwnerOverride,
    ResolvedLine,
    RuleConfirmation,
    RuleMatch,
    RuleProposal,
    apply_rules,
    best_rule,
    confirmations_from_review,
    learn_rules,
    rule_matches,
)
from .text import contains_card_like_digits, luhn_valid, normalize_merchant
from .totals import CycleTotals, SubscriptionRow, cycle_status, subscription_view, summarize_cycle
from .vocabulary import (
    CURRENCIES,
    DEFAULT_CATEGORY,
    DEFAULT_PATH,
    DEFAULT_PLATFORM,
    LOW_CONFIDENCE,
    MAX_BATCH_BYTES,
    MAX_LINES,
    PATHS,
    PAYMENT_STATUS_CANCELLED,
    PAYMENT_STATUS_PAID,
    PAYMENT_STATUSES,
    PLATFORMS,
    SCHEMA_VERSION,
    SOURCE_TYPE_ACTUAL_PAYMENT,
    TAGS,
)

__all__ = (
    "ACTIONS",
    "BLOCK_MERGE_CHOICE",
    "BLOCK_UNASSIGNED",
    "CURRENCIES",
    "DEFAULT_CATEGORY",
    "DEFAULT_PATH",
    "DEFAULT_PLATFORM",
    "LINKED_EXPENSE_CHANGED",
    "LOW_CONFIDENCE",
    "MAX_BATCH_BYTES",
    "MAX_ISSUES",
    "MAX_LINES",
    "PATHS",
    "PAYMENT_STATUS_CANCELLED",
    "PAYMENT_STATUS_PAID",
    "PAYMENT_STATUSES",
    "PLATFORMS",
    "SCHEMA_VERSION",
    "SOURCE_TYPE_ACTUAL_PAYMENT",
    "TAGS",
    "BatchLine",
    "Classification",
    "CycleTotals",
    "DiffEntry",
    "ExpenseDraft",
    "ExpenseOp",
    "Issue",
    "LineFacts",
    "ManualExpense",
    "MatchResult",
    "MerchantRule",
    "MergeItem",
    "OwnerOverride",
    "ResolvedLine",
    "ReviewChoices",
    "RuleConfirmation",
    "RuleMatch",
    "RuleProposal",
    "StatementBatch",
    "StatementDiff",
    "StatementValidationError",
    "StoredLine",
    "SubscriptionRow",
    "apply_rules",
    "best_rule",
    "confirmations_from_review",
    "contains_card_like_digits",
    "cycle_of",
    "cycle_status",
    "diff_statement",
    "is_statement_source_key",
    "learn_rules",
    "line_key",
    "luhn_valid",
    "map_to_expense",
    "match_batch",
    "match_manual",
    "normalize_merchant",
    "owns_source_key",
    "parse_iso_date",
    "parse_statement_batch",
    "rule_matches",
    "statement_id",
    "statement_source_key",
    "subscription_view",
    "summarize_cycle",
    "vendor_matches",
)
