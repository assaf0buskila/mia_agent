"""The seam between the ledger conformance suite and one ledger implementation (V2-16f).

The conformance suite (``tests/services/os_ledger/conformance``) is written against ``LedgerEnv``
only. ``build_fake_env`` wraps ``FakeLedger``. V2-16 supplies the same surface over PostgreSQL in
``tests/services/os_ledger/conftest.py``, so one suite proves both implementations.

A ``LedgerEnv`` exposes exactly:
  * the ``Ledger`` and ``AuthorityAdmin`` protocol methods (delegated, never anything else);
  * ``knobs``: the mutable settings the ledger reads at call time;
  * ``rows(table)``, ``version(ws)`` and ``now()``/``advance(seconds)``: observers and a clock.
Fault injection (``inject``/``pause``) is deliberately absent: it is fake-only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tests.fakes.os_ledger import FakeLedger

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
WORKSPACE = "test"
OWNER_ID = "123"

# Every name a conformance test may call on the implementation.
LEDGER_SURFACE = frozenset({
    "transaction", "propose", "decide", "receipt", "creation_request",
    "head", "page", "deleted", "pending", "labels", "monthly_summary",
    "ensure_workspace", "state", "transition", "bump_epoch", "pin_statement_pepper",
})


@dataclass
class Knobs:
    """Settings the ledger reads on every call (plain attributes; tests assign to them)."""

    settings_epoch: int = 1
    backend: str = "postgres"
    kill_switch: bool = False
    freeze: bool = False
    enabled: bool = True
    undo_window_s: int = 30


class LedgerEnv:
    """One implementation of the ledger plus the test observers around it."""

    def __init__(
        self,
        kind: str,
        impl: Any,
        knobs: Knobs,
        *,
        rows: Callable[[str], list[dict]],
        version: Callable[[str], int],
        now: Callable[[], datetime],
        advance: Callable[[float], None],
    ) -> None:
        self.kind = kind
        self.knobs = knobs
        self._impl = impl
        self._rows, self._version, self._now, self._advance = rows, version, now, advance

    def __getattr__(self, name: str) -> Any:
        if name in LEDGER_SURFACE:
            return getattr(self._impl, name)
        raise AttributeError(name)

    def rows(self, table: str) -> list[dict]:
        return self._rows(table)

    def version(self, workspace: str = WORKSPACE) -> int:
        return self._version(workspace)

    def now(self) -> datetime:
        return self._now()

    def advance(self, seconds: float) -> None:
        self._advance(seconds)


def build_fake_fixture(**overrides: Any) -> tuple[FakeLedger, Knobs]:
    knobs = Knobs(**overrides)
    fake = FakeLedger(
        clock=lambda: NOW,
        settings_epoch=lambda: knobs.settings_epoch,
        backend=lambda: knobs.backend,
        kill_switch=lambda: knobs.kill_switch,
        freeze=lambda: knobs.freeze,
        undo_window_s=lambda: knobs.undo_window_s,
        enabled=lambda: knobs.enabled,
    )
    return fake, knobs


def build_fake_env(**overrides: Any) -> LedgerEnv:
    """A ``LedgerEnv`` over a fresh ``FakeLedger`` with no workspace."""
    fake, knobs = build_fake_fixture(**overrides)
    return LedgerEnv(
        "fake", fake, knobs, rows=fake.rows, version=fake.version,
        now=fake._now, advance=fake.advance,
    )
