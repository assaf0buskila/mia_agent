"""Validation errors carry reason codes and structural paths, never input values.

Statement text is private and may be hostile. Nothing in this package echoes it back, so a
logged error cannot leak a merchant name, an amount or a card number.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

MAX_ISSUES = 25


@dataclass(frozen=True)
class Issue:
    path: str
    code: str


class StatementValidationError(ValueError):
    """One or more reason-coded problems. `issues` holds (path, code) pairs only."""

    def __init__(self, issues: Iterable[Issue]) -> None:
        self.issues: tuple[Issue, ...] = tuple(issues)[:MAX_ISSUES]
        summary = ", ".join(f"{issue.path or '-'}:{issue.code}" for issue in self.issues)
        super().__init__(f"statement validation failed: {summary}")

    @classmethod
    def of(cls, code: str, path: str = "") -> StatementValidationError:
        return cls([Issue(path, code)])


class IssueCollector:
    """Collects issues up to MAX_ISSUES so a hostile batch cannot make the error unbounded."""

    __slots__ = ("_issues", "_attempts")

    def __init__(self) -> None:
        self._issues: list[Issue] = []
        self._attempts = 0

    def add(self, path: str, code: str) -> None:
        self._attempts += 1
        if len(self._issues) < MAX_ISSUES:
            self._issues.append(Issue(path, code))

    def __len__(self) -> int:
        """Problems seen so far, including those dropped past the cap."""
        return self._attempts

    def raise_if_any(self) -> None:
        if self._issues:
            raise StatementValidationError(self._issues)
