"""The default full ``StateValidator`` of the PG ledger and the domain-error mapping (V2-16).

``FullStateValidator`` re-validates the transaction's complete record state after every ``apply``
with the copied prototype ``validate_state`` (the same check the FakeLedger runs by default).
V2-18's ``ScopedStateValidator`` replaces it in ``build_ledger()`` only after its property test
proves scoped accept/reject equal to this full one (V2_PG_CONTRACTS.md section 8).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.services.os_ledger import api
from app.services.os_ledger.domain import DomainError, validate_state


def domain_error(exc: Any, code: str = "target_changed") -> api.LedgerError:
    """Map a domain or summary refusal to its business code (the FakeLedger's mapping).

    ``lead_already_exists`` is its own definite no-write code; every other domain code becomes
    ``code`` (``target_changed`` inside a write, ``validation_error`` at propose) with
    ``details.reason`` set to the domain code.
    """
    reason = getattr(exc, "code", None)
    if reason == "lead_already_exists":
        return api.LedgerError("lead_already_exists")
    return api.LedgerError(code, details={"reason": reason})


class FullStateValidator:
    """``api.StateValidator``: the full seven-entity check over the transaction's records."""

    def validate(self, tx: Any, applied: Sequence[api.Mutation]) -> None:
        try:
            validate_state(tx.snapshot_records())
        except DomainError as exc:
            raise domain_error(exc) from None
