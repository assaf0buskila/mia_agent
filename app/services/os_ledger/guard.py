"""The production guard of the PG ledger (V2_PG_CONTRACTS.md §5.5) and its runtime engines.

The runtime never holds the privileged migration DSN, so the guard reuses V2-15's runtime hook:

* **Per engine** (on the first connection, again every 10 minutes, and after any new pooled
  connection; cached in between): ``role_policy.validate_ledger_role(db, main_engine,
  migration_role=None)`` and ``SHOW fsync`` = ``on``.
* **Per transaction**, in the same round trip as the workspace lock (``admission_sql``):
  ``set_config('synchronous_commit', 'on', true)`` and ``current_user = session_user =
  'assaf_os_ledger'``.

Every refusal is ``ledger_unavailable``, logged ``os_ledger op=guard outcome=refused
reason=<code>`` (reason codes only, never DB error text).

Every ledger engine connects with ``CONNECT_TIMEOUT_S``, so a dead database refuses quickly
(``db_unreachable``, or the guard's own sanitized ``ledger_role_denied``) instead of holding a
caller for the operating system's own connect timeout. The Sheets fence's read,
``authority.core_state``, adds a statement timeout on top.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.os_ledger_migrate import RUNTIME_ROLE
from app.db.session import sqlalchemy_database_url
from app.services.os_ledger import api

_LOG = logging.getLogger("mia.os_ledger")
REVALIDATE_SECONDS = 600
FSYNC_SQL = "SHOW fsync"
# Keeps a dead database from eating a caller's budget (a cold decide has 30 s and the Sheets fence
# reads here first). libpq reads a connect_timeout below 2 s as 2 s.
CONNECT_TIMEOUT_S = 3

# The per-transaction guard and the workspace lock in one round trip. The lock CTE is
# materialized so the row lock is always taken; the authority rows are read by a SEPARATE
# statement after it (a READ COMMITTED statement keeps its start snapshot, so reading them here
# could miss a transition committed while this statement waited for the lock).
ADMISSION_SQL = text("""
    WITH w AS MATERIALIZED (
        SELECT owner_id, epoch, version, pending_seq FROM assaf_os.os_workspaces
        WHERE workspace_id = :ws FOR UPDATE
    )
    SELECT set_config('synchronous_commit', 'on', true) AS synchronous_commit,
           current_user::text AS current_role, session_user::text AS session_role,
           now() AS db_now, w.owner_id, w.epoch, w.version, w.pending_seq
    FROM (VALUES (1)) AS one(x) LEFT JOIN w ON true
""")
AUTHORITY_SQL = text(
    "SELECT domain, state, epoch FROM assaf_os.os_authority WHERE workspace_id = :ws"
)
# Reads: REPEATABLE READ READ ONLY, one snapshot for the identity, workspace and authority.
READ_ADMISSION_SQL = text("""
    SELECT current_user::text AS current_role, session_user::text AS session_role,
           now() AS db_now, w.owner_id, w.epoch, w.version, w.pending_seq,
           (SELECT jsonb_object_agg(a.domain, jsonb_build_array(a.state, a.epoch))
              FROM assaf_os.os_authority a WHERE a.workspace_id = :ws) AS authority
    FROM (VALUES (1)) AS one(x)
    LEFT JOIN assaf_os.os_workspaces w ON w.workspace_id = :ws
""")


def refuse(reason: str) -> api.LedgerError:
    _LOG.warning("os_ledger op=guard outcome=refused reason=%s", reason)
    return api.LedgerError("ledger_unavailable", reason=reason)


def check_identity(current_role: str | None, session_role: str | None) -> None:
    """The per-transaction half of the guard: the session is the unimpersonated runtime role."""
    if (current_role, session_role) != (RUNTIME_ROLE, RUNTIME_ROLE):
        raise refuse("guard_identity")


class EngineGuard:
    """The cached per-engine half of the guard for one (runtime engine, main DSN) pair."""

    def __init__(self, engine: Engine, main_engine: Engine, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._engine = engine
        self._main_engine = main_engine
        self._clock = clock
        self._lock = threading.Lock()
        self._passed_at: float | None = None
        self._stale = True
        event.listen(engine, "connect", self._on_connect)

    def _on_connect(self, *_args) -> None:
        # Any new DBAPI connection (first use, pool growth, reconnect) re-runs the check.
        self._stale = True

    def invalidate(self) -> None:
        self._stale = True

    def ensure(self) -> None:
        with self._lock:
            fresh = (not self._stale and self._passed_at is not None
                     and self._clock() - self._passed_at < REVALIDATE_SECONDS)
            if fresh:
                return
            self._passed_at = None
            self._validate()
            self._stale = False
            self._passed_at = self._clock()

    def _validate(self) -> None:
        from app.services.os_ledger import role_policy

        try:
            with Session(self._engine) as db:
                role_policy.validate_ledger_role(db, self._main_engine, migration_role=None)
                fsync = db.execute(text(FSYNC_SQL)).scalar()
        except ValueError:
            raise refuse(role_policy.DENIED) from None
        except (SQLAlchemyError, OSError):
            raise refuse("guard_unreachable") from None
        if fsync != "on":
            raise refuse("fsync_off")


# ---- engines -----------------------------------------------------------------------------

_REGISTRY_LOCK = threading.Lock()
_ENGINES: dict[str, Engine] = {}
_GUARDS: dict[tuple[str, str], EngineGuard] = {}


def _postgres_url(dsn: str, reason: str) -> str:
    """The DSN pinned to psycopg3, as the app pins every Postgres DSN (``runtime.env`` holds the
    bare ``postgresql://`` scheme that V2-15 provisioning writes)."""
    try:
        pinned = sqlalchemy_database_url(dsn)
        url = make_url(pinned)
    except Exception:  # noqa: BLE001 - any parse failure is the same refusal
        raise refuse(reason) from None
    if url.get_backend_name() != "postgresql":
        raise refuse(reason)
    return pinned


def engine_for(dsn: str, *, reason: str = "ledger_dsn_invalid") -> Engine:
    """One pooled engine per DSN, shared by every ledger built on it (never logged)."""
    pinned = _postgres_url(dsn, reason)
    with _REGISTRY_LOCK:
        engine = _ENGINES.get(dsn)
        if engine is None:
            try:
                engine = create_engine(pinned, pool_pre_ping=True, pool_size=5, max_overflow=5,
                                       connect_args={"connect_timeout": CONNECT_TIMEOUT_S})
            except Exception:  # noqa: BLE001 - e.g. a driver that is not installed
                raise refuse(reason) from None
            _ENGINES[dsn] = engine
        return engine


def guard_for(runtime_dsn: str, main_dsn: str) -> EngineGuard:
    runtime = engine_for(runtime_dsn)
    main = engine_for(main_dsn, reason="main_dsn_invalid")
    with _REGISTRY_LOCK:
        guard = _GUARDS.get((runtime_dsn, main_dsn))
        if guard is None:
            guard = EngineGuard(runtime, main)
            _GUARDS[(runtime_dsn, main_dsn)] = guard
        return guard


def dispose_engines() -> None:
    """Close every pooled ledger connection and forget the cached guard results."""
    with _REGISTRY_LOCK:
        engines = list(_ENGINES.values())
        _ENGINES.clear()
        _GUARDS.clear()
    for engine in engines:
        engine.dispose()
