"""Mirror outbox leases and ``os_mirror_state`` (§14), each step one short transaction.

The ledger writes one ``os_outbox`` row per committed receipt (§5.3). The mirror publisher claims
rows under a lease, publishes outside any transaction, and finishes each row with a
compare-and-set on ``lease_generation``. A worker that crashed between claim and finish leaves a
lease that is reclaimed after ``lease_ttl``.

``os_mirror_state.verified_version`` only ever moves forward, and only to the observed version of
a projection that was read back equal. It is never set from the head or from an event's own
``commit_version``. A status reported from an observation older than the verified version is
ignored, so a stale worker can never overwrite a newer outcome.

No method here opens a transaction that outlives it, and none is ever called with one open.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.services.os_ledger import api

LEASE_TTL = timedelta(seconds=120)
FINISH_STATUSES = frozenset({"pending", "sent", "unknown", "conflict"})
MIRROR_STATUSES = frozenset({"pending", "synced", "delayed", "unknown", "conflict", "fenced"})
_BACKOFF_BASE_S = 5
_BACKOFF_CAP_S = 300
_SCHEMA_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class OutboxError(Exception):
    """A refused outbox or mirror-state operation. ``code`` is a reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OutboxEvent:
    workspace_id: str
    event_id: str
    commit_version: int
    body: Mapping[str, Any]
    lease_generation: int
    attempts: int


@dataclass(frozen=True)
class MirrorState:
    workspace_id: str
    destination_binding: str
    status: str
    projected_version: int
    verified_version: int
    last_attempt_at: datetime | None
    last_verified_at: datetime | None
    last_error_code: str | None
    scan_coverage: Mapping[str, Any] = field(default_factory=dict)


class OutboxStore(Protocol):
    def claim(
        self, limit: int, lease_owner: str, now: datetime, lease_ttl: timedelta = LEASE_TTL
    ) -> list[OutboxEvent]: ...
    def finish(
        self,
        event: OutboxEvent,
        generation: int,
        status: str,
        error_code: str | None,
        next_attempt_at: datetime,
    ) -> bool: ...
    def load_state(self, destination_binding: str, now: datetime) -> MirrorState: ...
    def record_attempt(self, version: int, now: datetime) -> None: ...
    def record_result(
        self,
        *,
        status: str,
        version: int,
        verified: bool,
        error_code: str | None,
        now: datetime,
        coverage: Mapping[str, Any] | None,
    ) -> None: ...


def destination_binding(spreadsheet_id: str) -> str:
    """Binds ``os_mirror_state`` to one workbook without storing its id (≤ 256 chars)."""
    return "sheets_v4:" + api.digest({"transport": "sheets_v4", "id": spreadsheet_id.strip()})


def backoff(attempts: int) -> timedelta:
    """5 s, 10 s, 20 s ... capped at 300 s."""
    exponent = max(0, min(attempts, 16) - 1)
    return timedelta(seconds=min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2**exponent))


def _code(error_code: str | None) -> str | None:
    if error_code is None:
        return None
    return error_code if _CODE_RE.fullmatch(error_code) else "error"


def _check_result(status: str, verified: bool) -> None:
    if status not in MIRROR_STATUSES or (verified and status != "synced"):
        raise OutboxError("mirror_status_invalid")


_STATE_COLUMNS = (
    "workspace_id, destination_binding, status, projected_version, verified_version, "
    "last_attempt_at, last_verified_at, last_error_code, scan_coverage"
)


class PgOutbox:
    """The §14 outbox on the ledger runtime DSN. Every method is one short transaction."""

    def __init__(self, engine: Any, workspace_id: str, *, schema: str = "assaf_os") -> None:
        if not _SCHEMA_RE.fullmatch(schema):
            raise OutboxError("outbox_schema_invalid")
        self._engine = engine
        self._workspace_id = workspace_id
        self._s = schema

    def _text(self, sql: str) -> Any:
        from sqlalchemy import text

        return text(sql.replace("{s}", self._s))

    def claim(
        self, limit: int, lease_owner: str, now: datetime, lease_ttl: timedelta = LEASE_TTL
    ) -> list[OutboxEvent]:
        # UPDATE ... FOR UPDATE SKIP LOCKED is not valid PostgreSQL: the lock is taken in the
        # subquery (§14). RETURNING adds ``attempts`` so a retried row is read back first.
        statement = self._text(
            "UPDATE {s}.os_outbox SET status='leased', lease_owner=:owner, "
            "leased_at=CAST(:now AS timestamptz), lease_generation=lease_generation+1, "
            "attempts=attempts+1, updated_at=CAST(:now AS timestamptz) "
            "WHERE (workspace_id, event_id) IN ("
            " SELECT workspace_id, event_id FROM {s}.os_outbox"
            " WHERE workspace_id=:ws AND ("
            "   (status IN ('pending','unknown') AND next_attempt_at <= CAST(:now AS timestamptz))"
            "   OR (status = 'leased'"
            "       AND leased_at < CAST(:now AS timestamptz) - CAST(:ttl AS interval)))"
            " ORDER BY commit_version"
            " FOR UPDATE SKIP LOCKED LIMIT :n) "
            "RETURNING workspace_id, event_id, commit_version, body, lease_generation, attempts"
        )
        with self._engine.begin() as conn:
            rows = conn.execute(
                statement,
                {"owner": lease_owner, "now": now, "ws": self._workspace_id,
                 "ttl": lease_ttl, "n": int(limit)},
            ).mappings().all()
        events = [
            OutboxEvent(
                workspace_id=row["workspace_id"],
                event_id=str(row["event_id"]),
                commit_version=int(row["commit_version"]),
                body=row["body"] if isinstance(row["body"], Mapping) else {},
                lease_generation=int(row["lease_generation"]),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]
        return sorted(events, key=lambda event: event.commit_version)

    def finish(
        self,
        event: OutboxEvent,
        generation: int,
        status: str,
        error_code: str | None,
        next_attempt_at: datetime,
    ) -> bool:
        """Compare-and-set on ``lease_generation``; clears the lease as V2-14's CHECKs require."""
        if status not in FINISH_STATUSES:
            raise OutboxError("outbox_status_invalid")
        statement = self._text(
            "UPDATE {s}.os_outbox SET status=:status, lease_owner=NULL, leased_at=NULL, "
            "last_error_code=CAST(:code AS varchar), "
            "next_attempt_at=CAST(:next AS timestamptz), updated_at=GREATEST(now(), updated_at) "
            "WHERE workspace_id=:ws AND event_id=CAST(:event AS uuid) "
            "AND lease_generation=:generation AND status='leased'"
        )
        with self._engine.begin() as conn:
            result = conn.execute(
                statement,
                {"status": status, "code": _code(error_code), "next": next_attempt_at,
                 "ws": event.workspace_id, "event": event.event_id,
                 "generation": int(generation)},
            )
            return result.rowcount == 1

    def _select_state(self, conn: Any) -> Any:
        return conn.execute(
            self._text(
                f"SELECT {_STATE_COLUMNS} FROM {{s}}.os_mirror_state WHERE workspace_id=:ws"
            ),
            {"ws": self._workspace_id},
        ).mappings().first()

    def load_state(self, destination_binding: str, now: datetime) -> MirrorState:
        with self._engine.begin() as conn:
            row = self._select_state(conn)
            if row is None:
                conn.execute(
                    self._text(
                        "INSERT INTO {s}.os_mirror_state (workspace_id, destination_binding) "
                        "VALUES (:ws, :binding) ON CONFLICT (workspace_id) DO NOTHING"
                    ),
                    {"ws": self._workspace_id, "binding": destination_binding},
                )
                row = self._select_state(conn)
        if row is None:
            raise OutboxError("mirror_state_unavailable")
        if row["destination_binding"] != destination_binding:
            raise OutboxError("mirror_destination_changed")
        coverage = row["scan_coverage"]
        return MirrorState(
            workspace_id=row["workspace_id"],
            destination_binding=row["destination_binding"],
            status=row["status"],
            projected_version=int(row["projected_version"]),
            verified_version=int(row["verified_version"]),
            last_attempt_at=row["last_attempt_at"],
            last_verified_at=row["last_verified_at"],
            last_error_code=row["last_error_code"],
            scan_coverage=coverage if isinstance(coverage, Mapping) else {},
        )

    def record_attempt(self, version: int, now: datetime) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                self._text(
                    "UPDATE {s}.os_mirror_state SET "
                    "projected_version=GREATEST(projected_version, CAST(:v AS bigint)), "
                    "last_attempt_at=CAST(:now AS timestamptz), "
                    "status=CASE WHEN status IN ('synced','pending','delayed') THEN 'pending' "
                    "ELSE status END, updated_at=CAST(:now AS timestamptz) "
                    "WHERE workspace_id=:ws"
                ),
                {"v": int(version), "now": now, "ws": self._workspace_id},
            )

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
        """Monotonic: ``verified_version = GREATEST(verified_version, version)`` when verified,
        and an observation older than the verified version changes nothing else."""
        _check_result(status, verified)
        fresh = "CAST(:v AS bigint) >= verified_version"
        statement = self._text(
            "UPDATE {s}.os_mirror_state SET "
            f"status=CASE WHEN {fresh} THEN CAST(:status AS varchar) ELSE status END, "
            f"last_error_code=CASE WHEN {fresh} THEN CAST(:code AS varchar) "
            "ELSE last_error_code END, "
            f"scan_coverage=CASE WHEN {fresh} AND CAST(:has_cov AS boolean) "
            "THEN CAST(:cov AS jsonb) ELSE scan_coverage END, "
            f"last_verified_at=CASE WHEN CAST(:verified AS boolean) AND {fresh} "
            "THEN CAST(:now AS timestamptz) ELSE last_verified_at END, "
            "verified_version=CASE WHEN CAST(:verified AS boolean) "
            "THEN GREATEST(verified_version, CAST(:v AS bigint)) ELSE verified_version END, "
            "projected_version=CASE WHEN CAST(:verified AS boolean) "
            "THEN GREATEST(projected_version, CAST(:v AS bigint)) ELSE projected_version END, "
            "updated_at=CAST(:now AS timestamptz) "
            "WHERE workspace_id=:ws"
        )
        with self._engine.begin() as conn:
            conn.execute(
                statement,
                {
                    "status": status,
                    "code": _code(error_code),
                    "has_cov": coverage is not None,
                    "cov": json.dumps(dict(coverage or {}), ensure_ascii=False),
                    "verified": bool(verified),
                    "v": int(version),
                    "now": now,
                    "ws": self._workspace_id,
                },
            )
