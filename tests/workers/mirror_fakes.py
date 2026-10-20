"""In-memory fakes for the V2-29 mirror: a ledger reader, an outbox store and a Sheets grid.

None of them touches a network or a database. Each fake counts its own "open transactions" so a
transport call can assert that nothing is open while it runs (§20 V2-29, I13).
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.integrations.sheets_mirror import MirrorTransportError
from app.services.os_ledger import api
from app.services.os_ledger.outbox import (
    FINISH_STATUSES,
    LEASE_TTL,
    MirrorState,
    OutboxError,
    OutboxEvent,
)

T0 = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


class TxCounter:
    """Shared across fakes: the number of fake transactions open right now."""

    def __init__(self) -> None:
        self.open = 0
        self.max_seen = 0

    @contextmanager
    def tx(self) -> Iterator[None]:
        self.open += 1
        self.max_seen = max(self.max_seen, self.open)
        try:
            yield
        finally:
            self.open -= 1


# ---------------------------------------------------------------------------------------------
# Reader


class FakeReader:
    """``api.LedgerReader`` subset the mirror uses: ``head``, ``page`` and ``monthly_summary``."""

    def __init__(self, counter: TxCounter) -> None:
        self.counter = counter
        self.version = 1
        self.epoch = 1
        self.states: dict[str, str] = {"core": "active", "statements": "shadow"}
        self.records: dict[str, dict[str, dict[str, Any]]] = {e: {} for e in api.ENTITIES}
        self.monthly: list[dict[str, Any]] = []
        # Personal statement detail lives in the ledger but is never reachable through a page.
        self.personal_lines: list[dict[str, Any]] = []
        self.on_page: Callable[[str], None] | None = None
        self.calls: list[str] = []

    def commit(self, entity: str, record: Mapping[str, Any]) -> int:
        key = api.ID_KEYS[entity]
        current = self.records[entity].get(record[key])
        revision = (current or {}).get("revision", 0) + 1
        self.records[entity][record[key]] = {**dict(record), "revision": revision}
        self.version += 1
        return self.version

    def head(self, ctx: Any) -> api.Head:
        with self.counter.tx():
            self.calls.append("head")
            return api.Head(
                observed_version=self.version,
                authority_epoch=self.epoch,
                states=dict(self.states),
                pending_seq=0,
                mirror_verified_version=0,
            )

    def page(self, ctx: Any, entity: str, *, after: str | None, limit: int) -> api.Page:
        if self.on_page is not None:
            self.on_page(entity)
        with self.counter.tx():
            self.calls.append(f"page:{entity}")
            offset = 0
            if after is not None:
                version, offset_text = after.split(":")
                if int(version) != self.version:
                    raise api.LedgerError(
                        "cursor_invalidated", details={"observed_version": self.version}
                    )
                offset = int(offset_text)
            rows = list(self.records[entity].values())
            chunk = rows[offset : offset + limit]
            more = offset + limit < len(rows)
            return api.Page(
                entity=entity,  # type: ignore[arg-type]
                records=tuple(copy.deepcopy(chunk)),
                next_cursor=f"{self.version}:{offset + limit}" if more else None,
                total=len(rows),
                observed_version=self.version,
            )

    def monthly_summary(self, ctx: Any) -> tuple[dict[str, Any], ...]:
        with self.counter.tx():
            self.calls.append("monthly")
            return tuple(copy.deepcopy(self.monthly))


# ---------------------------------------------------------------------------------------------
# Outbox


class FakeOutbox:
    """The §14 outbox and ``os_mirror_state`` semantics, in memory (V2-14 CHECKs asserted)."""

    def __init__(
        self, counter: TxCounter, clock: Callable[[], datetime], workspace_id: str = "ws-1"
    ) -> None:
        self.counter = counter
        self.clock = clock
        self.workspace_id = workspace_id
        self.rows: dict[str, dict[str, Any]] = {}
        self.state: dict[str, Any] | None = None
        self.calls: list[str] = []

    # test helpers ------------------------------------------------------------------------
    def add(
        self, commit_version: int, body: Mapping[str, Any], *, at: datetime = T0
    ) -> str:
        event_id = str(uuid4())
        self.rows[event_id] = {
            "event_id": event_id,
            "commit_version": commit_version,
            "body": dict(body),
            "status": "pending",
            "attempts": 0,
            "next_attempt_at": at,
            "lease_owner": None,
            "lease_generation": 0,
            "leased_at": None,
            "last_error_code": None,
            "created_at": at,
            "updated_at": at,
        }
        return event_id

    def row(self, event_id: str) -> dict[str, Any]:
        return self.rows[event_id]

    def _check(self, row: Mapping[str, Any]) -> None:
        assert (row["status"] == "leased") == (row["lease_owner"] is not None)
        assert (row["lease_owner"] is None) == (row["leased_at"] is None)
        assert row["updated_at"] >= row["created_at"]

    # OutboxStore ----------------------------------------------------------------------------
    def claim(
        self,
        limit: int,
        lease_owner: str,
        now: datetime,
        lease_ttl: timedelta = LEASE_TTL,
    ) -> list[OutboxEvent]:
        with self.counter.tx():
            self.calls.append("claim")
            due = [
                row
                for row in self.rows.values()
                if (row["status"] in ("pending", "unknown") and row["next_attempt_at"] <= now)
                or (row["status"] == "leased" and row["leased_at"] < now - lease_ttl)
            ]
            due.sort(key=lambda row: row["commit_version"])
            claimed = []
            for row in due[:limit]:
                row.update(
                    status="leased",
                    lease_owner=lease_owner,
                    leased_at=now,
                    lease_generation=row["lease_generation"] + 1,
                    attempts=row["attempts"] + 1,
                    updated_at=now,
                )
                self._check(row)
                claimed.append(
                    OutboxEvent(
                        workspace_id=self.workspace_id,
                        event_id=row["event_id"],
                        commit_version=row["commit_version"],
                        body=copy.deepcopy(row["body"]),
                        lease_generation=row["lease_generation"],
                        attempts=row["attempts"],
                    )
                )
            return claimed

    def finish(
        self,
        event: OutboxEvent,
        generation: int,
        status: str,
        error_code: str | None,
        next_attempt_at: datetime,
    ) -> bool:
        if status not in FINISH_STATUSES:
            raise OutboxError("outbox_status_invalid")
        with self.counter.tx():
            self.calls.append("finish")
            row = self.rows.get(event.event_id)
            if row is None or row["lease_generation"] != generation or row["status"] != "leased":
                return False
            row.update(
                status=status,
                lease_owner=None,
                leased_at=None,
                last_error_code=error_code,
                next_attempt_at=next_attempt_at,
                updated_at=max(row["updated_at"], self.clock()),
            )
            self._check(row)
            return True

    def load_state(self, destination_binding: str, now: datetime) -> MirrorState:
        with self.counter.tx():
            self.calls.append("load_state")
            if self.state is None:
                self.state = {
                    "destination_binding": destination_binding,
                    "status": "pending",
                    "projected_version": 0,
                    "verified_version": 0,
                    "last_attempt_at": None,
                    "last_verified_at": None,
                    "last_error_code": None,
                    "scan_coverage": {},
                }
            if self.state["destination_binding"] != destination_binding:
                raise OutboxError("mirror_destination_changed")
            return self.snapshot()

    def snapshot(self) -> MirrorState:
        assert self.state is not None
        return MirrorState(
            workspace_id=self.workspace_id,
            **{key: copy.deepcopy(value) for key, value in self.state.items()},
        )

    def record_attempt(self, version: int, now: datetime) -> None:
        with self.counter.tx():
            self.calls.append("record_attempt")
            state = self.state
            assert state is not None
            state["projected_version"] = max(state["projected_version"], version)
            state["last_attempt_at"] = now
            if state["status"] in ("synced", "pending", "delayed"):
                state["status"] = "pending"

    def record_result(
        self,
        *,
        status: str,
        version: int,
        verified: bool,
        error_code: str | None,
        now: datetime,
        coverage: Mapping[str, Any] | None,
    ) -> None:
        with self.counter.tx():
            self.calls.append("record_result")
            state = self.state
            assert state is not None
            current = state["verified_version"]
            if version >= current:
                state["status"] = status
                state["last_error_code"] = error_code
                if coverage is not None:
                    state["scan_coverage"] = copy.deepcopy(dict(coverage))
                if verified:
                    state["last_verified_at"] = now
            if verified:
                state["verified_version"] = max(current, version)
                state["projected_version"] = max(state["projected_version"], version)
            assert state["verified_version"] <= state["projected_version"]


# ---------------------------------------------------------------------------------------------
# Sheets


def tab_of(a1: str) -> str:
    name = a1.split("!", 1)[0]
    assert name.startswith("'") and name.endswith("'"), a1
    return name[1:-1].replace("''", "'")


class FakeSheets:
    """A RAW-only grid per tab. Formulas are never evaluated; an empty string clears a cell."""

    def __init__(self, tabs: Sequence[str], guard: Callable[[], None] | None = None) -> None:
        self.grid: dict[str, dict[tuple[int, int], Any]] = {tab: {} for tab in tabs}
        self.guard = guard
        self.writes: list[list[tuple[str, list[list[Any]]]]] = []
        self.reads = 0
        self.fail_next_write: str | None = None  # "lost_ack" | "not_sent"
        self.fail_next_read = False
        self.before_apply: Callable[[], None] | None = None
        self.mangle: Callable[[Any], Any] | None = None

    def _guard(self) -> None:
        if self.guard is not None:
            self.guard()

    def batch_update(self, ranges: Sequence[tuple[str, Sequence[Sequence[Any]]]]) -> None:
        self._guard()
        payload = [(a1, [list(row) for row in rows]) for a1, rows in ranges]
        self.writes.append(payload)
        mode, self.fail_next_write = self.fail_next_write, None
        if mode == "not_sent":
            raise MirrorTransportError("mirror_http_429", sent=False)
        if self.before_apply is not None:
            hook, self.before_apply = self.before_apply, None
            hook()
        for a1, rows in payload:
            assert a1.endswith("!A1"), a1
            cells = self.grid[tab_of(a1)]
            for r, row in enumerate(rows):
                for c, value in enumerate(row):
                    if self.mangle is not None:
                        value = self.mangle(value)
                    if value == "" or value is None:
                        cells.pop((r, c), None)
                    else:
                        cells[(r, c)] = value
        if mode == "lost_ack":
            raise MirrorTransportError("mirror_timeout", sent=True)

    def batch_get(self, ranges: Sequence[str]) -> dict[str, list[list[Any]]]:
        self._guard()
        self.reads += 1
        if self.fail_next_read:
            self.fail_next_read = False
            raise MirrorTransportError("mirror_read_failed", sent=False)
        return {a1: self.values(tab_of(a1)) for a1 in ranges}

    def values(self, tab: str) -> list[list[Any]]:
        cells = self.grid[tab]
        if not cells:
            return []
        rows = max(r for r, _ in cells) + 1
        out: list[list[Any]] = []
        for r in range(rows):
            cols = [c for (rr, c) in cells if rr == r]
            width = max(cols) + 1 if cols else 0
            out.append([cells.get((r, c), "") for c in range(width)])
        return out

    def edit(self, tab: str, row: int, col: int, value: Any) -> None:
        """An out-of-band edit (a person, or a stale writer) on the read-only mirror."""
        if value == "":
            self.grid[tab].pop((row, col), None)
        else:
            self.grid[tab][(row, col)] = value

    def all_cells(self) -> list[Any]:
        return [value for cells in self.grid.values() for value in cells.values()]


def outbox_body(epoch: int = 1, refs: Sequence[tuple[str, str, int]] = ()) -> dict[str, Any]:
    return {
        "authority_epoch": epoch,
        "refs": [{"entity": e, "record_id": i, "revision": r} for e, i, r in refs],
    }
