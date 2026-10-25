"""The authority state machine and epoch (V2_PG_CONTRACTS.md §5.4) on PostgreSQL.

``PgAuthorityAdmin`` implements ``api.AuthorityAdmin`` and runs ONLY on the privileged DSN, inside
the ``mia os-authority`` / ``mia os-ledger`` jobs (V2-31 owns the CLI). The runtime role cannot
UPDATE ``os_authority``, ``os_workspaces.epoch`` or ``statement_pepper_fp``. Every call takes the
``os_workspaces`` row lock first, so a transition happens strictly between ledger transactions.

Legal edges, per domain: shadow->active, shadow->fenced, active->fenced, fenced->restoring, plus
the two bumping edges fenced->shadow (only while no non-import receipt exists at the current
epoch) and restoring->active. There is no fenced->active. One epoch per workspace:
``os_workspaces.epoch`` and both ``os_authority`` rows always move together.

``core_state(settings)`` is the context-free read behind ``api.core_authority_state`` (V2-17's
Sheets fence only): it runs on the runtime DSN behind the same §5.5 guard.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from app.services.os_ledger import api
from app.services.os_ledger.guard import check_identity, engine_for, guard_for
from app.services.os_ledger.tx import database_error, held, integrity

_LOG = logging.getLogger("mia.os_ledger")
DOMAINS = ("core", "statements")
STATES = ("shadow", "active", "fenced", "restoring")
PLAIN_EDGES = frozenset({("shadow", "active"), ("shadow", "fenced"), ("active", "fenced"),
                         ("fenced", "restoring")})
BUMP_EDGES = frozenset({("fenced", "shadow"), ("restoring", "active")})
_BUMP_TARGETS = frozenset(target for _, target in BUMP_EDGES)
_OWNER_RE = re.compile(r"[0-9]{1,20}")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
# The Sheets fence reads core_state before every Sheets write, and a cold decide has a 30 s budget.
# The read is one indexed lookup, so 3 s is generous; the statement timeout also covers a wait on
# a lock (a migration holding ACCESS EXCLUSIVE on os_authority). The connect timeout is
# guard.CONNECT_TIMEOUT_S, which every ledger engine carries.
FENCE_STATEMENT_TIMEOUT_MS = 3000


def _changed(reason: str) -> api.LedgerError:
    _LOG.warning("os_ledger op=authority outcome=refused reason=%s", reason)
    return api.LedgerError("authority_changed", reason=reason)


def _missing() -> api.LedgerError:
    _LOG.warning("os_ledger op=authority outcome=refused reason=workspace_missing")
    return api.LedgerError("ledger_unavailable", reason="workspace_missing")


class PgAuthorityAdmin:
    """``api.AuthorityAdmin`` on the privileged DSN. ``clock`` is a test seam (default DB now())."""

    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] | None = None) -> None:
        self._engine = engine
        self._clock = clock

    @contextmanager
    def _locked(self, workspace_id: str):
        try:
            with held(workspace_id), self._engine.begin() as conn:
                row = conn.execute(text(
                    "SELECT owner_id, epoch, version, statement_pepper_fp, now() AS db_now "
                    "FROM assaf_os.os_workspaces WHERE workspace_id = :ws FOR UPDATE"
                ), {"ws": workspace_id}).first()
                yield conn, row
        except api.LedgerError:
            raise
        except SQLAlchemyError as exc:
            raise database_error(exc) from None

    def _now(self, conn: Connection) -> datetime:
        if self._clock is not None:
            return self._clock()
        return conn.execute(text("SELECT now()")).scalar_one()

    @staticmethod
    def _snapshot(conn: Connection, workspace_id: str) -> api.AuthoritySnapshot:
        ws = conn.execute(text(
            "SELECT epoch, version FROM assaf_os.os_workspaces WHERE workspace_id = :ws"
        ), {"ws": workspace_id}).first()
        if ws is None:
            raise _missing()
        rows = conn.execute(text(
            "SELECT domain, state, activated_at FROM assaf_os.os_authority "
            "WHERE workspace_id = :ws"), {"ws": workspace_id}).all()
        states = {row.domain: row.state for row in rows}
        if set(states) != set(DOMAINS):
            raise integrity("authority_rows")
        return api.AuthoritySnapshot(
            workspace_id, int(ws.epoch), int(ws.version),
            {domain: states[domain] for domain in DOMAINS},
            {domain: next(r.activated_at for r in rows if r.domain == domain)
             for domain in DOMAINS})

    def ensure_workspace(self, workspace_id: str, owner_id: str,
                         epoch: int) -> api.AuthoritySnapshot:
        """First call: epoch 1 and both domains shadow; an equal repeat is a no-op."""
        if not (isinstance(owner_id, str) and _OWNER_RE.fullmatch(owner_id)):
            raise integrity("workspace_owner")
        with self._locked(workspace_id) as (conn, row):
            if row is None:
                if epoch != 1:
                    raise integrity("workspace_initial_epoch")
                now = self._now(conn)
                inserted = conn.execute(text(
                    "INSERT INTO assaf_os.os_workspaces (workspace_id, owner_id, epoch, version, "
                    "summaries, created_at, updated_at, pending_seq) VALUES (:ws, :owner, 1, 0, "
                    "'{}'::jsonb, :now, :now, 0) ON CONFLICT (workspace_id) DO NOTHING"
                ), {"ws": workspace_id, "owner": owner_id, "now": now}).rowcount
                if inserted == 1:
                    conn.execute(text(
                        "INSERT INTO assaf_os.os_authority (workspace_id, domain, state, epoch, "
                        "created_at, updated_at) VALUES (:ws, 'core', 'shadow', 1, :now, :now), "
                        "(:ws, 'statements', 'shadow', 1, :now, :now)"
                    ), {"ws": workspace_id, "now": now})
                    return self._snapshot(conn, workspace_id)
                row = conn.execute(text(  # a concurrent creator won; compare with its row
                    "SELECT owner_id, epoch FROM assaf_os.os_workspaces "
                    "WHERE workspace_id = :ws FOR UPDATE"), {"ws": workspace_id}).first()
            if (row.owner_id, int(row.epoch)) != (owner_id, epoch):
                raise _changed("workspace_conflict")  # D1: another (owner, epoch) is refused
            return self._snapshot(conn, workspace_id)

    def state(self, workspace_id: str) -> api.AuthoritySnapshot:
        with self._locked(workspace_id) as (conn, row):
            if row is None:
                raise _missing()
            return self._snapshot(conn, workspace_id)

    def reset_mirror(self, workspace_id: str, *, expected_epoch: int, actor_owner_id: str,
                     destination_binding: str, audit: Callable[[], None]) -> bool:
        """Privileged CLI only, under a freeze. Never deletes or changes a destination pin.

        The workspace lock serializes the epoch/core checks with activation and rearm. The
        row lock serializes each publisher progress statement, not a whole tick: an in-flight
        tick can record_attempt or record_result after this reset commits. That affects only
        liveness; re-run the guarded reset. Audit failure aborts the tx.
        """
        with self._locked(workspace_id) as (conn, row):
            if row is None:
                raise _missing()
            if row.owner_id != actor_owner_id or int(row.epoch) != expected_epoch:
                raise _changed("epoch_mismatch")
            if self._snapshot(conn, workspace_id).states["core"] == "active":
                raise _changed("core_active")
            mirror = conn.execute(text(
                "SELECT destination_binding FROM assaf_os.os_mirror_state "
                "WHERE workspace_id=:ws FOR UPDATE"
            ), {"ws": workspace_id}).first()
            if mirror is not None and mirror.destination_binding != destination_binding:
                raise _changed("mirror_destination_changed")
            audit()
            if mirror is None:
                return False
            result = conn.execute(text(
                "UPDATE assaf_os.os_mirror_state SET status='pending', projected_version=0, "
                "verified_version=0, last_attempt_at=NULL, last_verified_at=NULL, "
                "last_error_code=NULL, watermark='{}'::jsonb, scan_coverage='{}'::jsonb, "
                "updated_at=GREATEST(updated_at, now()) WHERE workspace_id=:ws AND "
                "(status, projected_version, verified_version, last_attempt_at, last_verified_at, "
                "last_error_code, watermark, scan_coverage) IS DISTINCT FROM "
                "('pending', 0::bigint, 0::bigint, NULL::timestamptz, NULL::timestamptz, "
                "NULL::varchar, '{}'::jsonb, '{}'::jsonb)"
            ), {"ws": workspace_id})
            return result.rowcount == 1

    @staticmethod
    def _bump(conn: Connection, workspace_id: str, now: datetime) -> int:
        epoch = conn.execute(text(
            "UPDATE assaf_os.os_workspaces SET epoch = epoch + 1, "
            "updated_at = GREATEST(updated_at, :now) WHERE workspace_id = :ws RETURNING epoch"
        ), {"ws": workspace_id, "now": now}).scalar_one()
        conn.execute(text(
            "UPDATE assaf_os.os_authority SET epoch = :epoch, "
            "updated_at = GREATEST(updated_at, :now) WHERE workspace_id = :ws"
        ), {"ws": workspace_id, "epoch": epoch, "now": now})
        return int(epoch)

    def transition(self, workspace_id: str, domain: api.Domain, to: api.AuthorityState, *,
                   expected_epoch: int, actor_owner_id: str) -> api.AuthoritySnapshot:
        if domain not in DOMAINS:
            raise integrity("authority_domain")
        if to not in STATES:
            raise integrity("authority_state")
        with self._locked(workspace_id) as (conn, row):
            if row is None:
                raise _missing()
            if row.owner_id != actor_owner_id:
                raise _changed("epoch_mismatch")
            epoch = int(row.epoch)
            current = conn.execute(text(
                "SELECT state FROM assaf_os.os_authority WHERE workspace_id = :ws "
                "AND domain = :domain"), {"ws": workspace_id, "domain": domain}).scalar_one()
            # D2: a bumping edge repeated by a caller that still holds the old epoch is a no-op
            # once its target state holds at the next epoch (no "last bump" is stored on PG; see
            # the V2-16 report).
            if epoch == expected_epoch + 1 and current == to and to in _BUMP_TARGETS:
                return self._snapshot(conn, workspace_id)
            if epoch != expected_epoch:
                raise _changed("epoch_mismatch")
            if current == to:
                return self._snapshot(conn, workspace_id)
            edge = (current, to)
            if edge not in PLAIN_EDGES | BUMP_EDGES:
                raise _changed("transition_refused")
            if edge == ("fenced", "shadow") and conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM assaf_os.os_receipts WHERE workspace_id = :ws "
                "AND kind <> 'import' AND epoch = :epoch)"
            ), {"ws": workspace_id, "epoch": epoch}).scalar():
                raise _changed("transition_refused")
            now = self._now(conn)
            if edge in BUMP_EDGES:
                self._bump(conn, workspace_id, now)
            activated = to == "active"
            conn.execute(text(
                "UPDATE assaf_os.os_authority SET state = :to, "
                "activated_at = CASE WHEN :activated THEN :now ELSE activated_at END, "
                "activated_by = CASE WHEN :activated THEN :actor ELSE activated_by END, "
                "updated_at = GREATEST(updated_at, :now) WHERE workspace_id = :ws "
                "AND domain = :domain"
            ), {"ws": workspace_id, "domain": domain, "to": to, "activated": activated,
                "now": now, "actor": actor_owner_id})
            conn.execute(text(  # a new workspace row version for every authority change
                "UPDATE assaf_os.os_workspaces SET updated_at = GREATEST(updated_at, :now) "
                "WHERE workspace_id = :ws"), {"ws": workspace_id, "now": now})
            _LOG.info("os_ledger op=authority outcome=transitioned reason=%s_%s_to_%s",
                      domain, current, to)
            return self._snapshot(conn, workspace_id)

    def bump_epoch(self, workspace_id: str, *, expected_epoch: int) -> api.AuthoritySnapshot:
        with self._locked(workspace_id) as (conn, row):
            if row is None:
                raise _missing()
            if int(row.epoch) == expected_epoch + 1:  # D3: idempotent at the new epoch
                return self._snapshot(conn, workspace_id)
            if int(row.epoch) != expected_epoch:
                raise _changed("epoch_mismatch")
            self._bump(conn, workspace_id, self._now(conn))
            return self._snapshot(conn, workspace_id)

    def pin_statement_pepper(self, workspace_id: str,
                             fingerprint: str) -> api.AuthoritySnapshot:
        """Write-once; equal is a no-op; different is ledger_unavailable (pepper mismatch)."""
        if not (isinstance(fingerprint, str) and _FINGERPRINT_RE.fullmatch(fingerprint)):
            raise integrity("statement_pepper_fp")
        with self._locked(workspace_id) as (conn, row):
            if row is None:
                raise _missing()
            if row.statement_pepper_fp not in {None, fingerprint}:
                _LOG.warning("os_ledger op=authority outcome=refused "
                             "reason=statement_pepper_mismatch")
                raise api.LedgerError("ledger_unavailable", reason="statement_pepper_mismatch")
            if row.statement_pepper_fp is None:
                conn.execute(text(
                    "UPDATE assaf_os.os_workspaces SET statement_pepper_fp = :fp "
                    "WHERE workspace_id = :ws AND statement_pepper_fp IS NULL"
                ), {"ws": workspace_id, "fp": fingerprint})
            return self._snapshot(conn, workspace_id)


def build_authority_admin(privileged_dsn: str, *,
                          clock: Callable[[], datetime] | None = None) -> PgAuthorityAdmin:
    """The AuthorityAdmin for the CLI jobs; ``privileged_dsn`` is never the runtime DSN."""
    return PgAuthorityAdmin(engine_for(privileged_dsn, reason="privileged_dsn_invalid"),
                            clock=clock)


def core_state(settings: Any) -> api.AuthorityState | None:
    """``os_authority(core)`` of ``settings.assaf_os_workspace_id`` on the runtime DSN.

    None when that workspace has no ``os_workspaces`` row (the Sheets call proceeds). Behind the
    §5.5 guard; any failure is ``ledger_unavailable``. ``api.core_authority_state`` has already
    returned None when no ledger DSN is configured.

    The read is bounded so that it cannot eat a caller's budget: the connection times out after
    ``guard.CONNECT_TIMEOUT_S`` and the statement (lock waits included) after
    ``FENCE_STATEMENT_TIMEOUT_MS``; both end as ``ledger_unavailable`` (``read_failed``).
    """
    dsn = getattr(settings, "owner_os_ledger_database_url", "")
    workspace_id = getattr(settings, "assaf_os_workspace_id", "") or ""
    guard_for(dsn, getattr(settings, "database_url", "")).ensure()
    try:
        with engine_for(dsn).connect() as conn:
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {FENCE_STATEMENT_TIMEOUT_MS}")
            row = conn.execute(text(
                "SELECT current_user::text AS current_role, session_user::text AS session_role, "
                "w.workspace_id IS NOT NULL AS present, a.state FROM (VALUES (1)) AS one(x) "
                "LEFT JOIN assaf_os.os_workspaces w ON w.workspace_id = :ws "
                "LEFT JOIN assaf_os.os_authority a ON a.workspace_id = w.workspace_id "
                "AND a.domain = 'core'"), {"ws": workspace_id}).one()
            conn.rollback()
    except SQLAlchemyError:
        _LOG.warning("os_ledger op=authority_state outcome=refused reason=read_failed")
        raise api.LedgerError("ledger_unavailable", reason="read_failed") from None
    check_identity(row.current_role, row.session_role)
    if not row.present:
        return None
    if row.state not in STATES:
        raise integrity("authority_rows")
    return row.state
