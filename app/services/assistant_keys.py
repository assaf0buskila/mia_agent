"""Assistant keys: issue, list, revoke, bearer authentication, limits, audit (V2_PG_CONTRACTS §17).

An assistant (Claude or GPT) holds a static bearer key. It can only SUBMIT a statement batch, which
lands as a pending V2-26 proposal that the owner alone can approve; nothing here can commit, decide
or reach the approve path. Two halves:

* ``AssistantCredentialService``: the owner's side. Issue, list, revoke, revoke-all. Each is ONE
  ledger transaction (``kind="statement_batch"``: the kind both a credential-only write and V2-26's
  ``revoke_pending_for_credential`` are admitted under, §5.3, D5), so revoking a key and rejecting
  its pending batches happen together or not at all.
* ``AssistantGateway``: the bearer side. ``authenticate`` runs §17.2's order: the feature switch,
  a cookie (401), the exact header, the per-IP count (429, before any lookup), the hash lookup,
  validity (revoked, expiry, allow-list, epoch, kill switch, the owner's revoke-all watermark;
  one uniform 401) with the invalid-lookup budget, and ``run`` then applies the per-key limits and
  writes the call's single audit row.

Storage for the limits is ``os_assistant_audit`` itself (D1; no new table). Rows hold codes and
counts only: never a payload, a label, a secret or an address (only ``client_ip_hmac``). A call
refused by the per-IP count or by the invalid-lookup budget writes no row (the auth-attempt
pattern: a blocked caller cannot grow the table, and its window slides); each such refusal logs a
reason-code line. Nothing here logs a secret, a hash, an address or a statement.

The advisory-lock pattern follows ``OwnerSessionStore.record_auth_attempt``: take the bucket lock,
COUNT the window, INSERT the row, all in one short transaction. A valid key's call holds its key
lock from the quota COUNT to the audit INSERT, so 30 requests an hour and 5 batches a day are exact
under concurrency; its lock and statement timeouts are 3 s.
"""

from __future__ import annotations

import base64
import hmac
import ipaddress
import logging
import re
import secrets
import unicodedata
import uuid
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.services.os_ledger import api, authorize
from app.services.os_ledger.guard import check_identity, engine_for, guard_for, refuse
from app.services.os_ledger.statement_tables import CredentialRow
from app.services.os_ledger.statement_wire import binding, iso
from app.services.os_ledger.statements import LedgerStatementService, db_revoke_watermark
from app.services.os_ledger.tx import database_error
from app.services.owner_sessions import credential_hash

API_VERSION = "assaf-os-assistant.v1"
SECRET_PREFIX = "aosp_"
BEARER = re.compile(r"Bearer aosp_[A-Za-z0-9_-]{43}")
SCOPES_TEXT = "merchant_rules:read own_proposals:read statement:propose"
KINDS = ("claude", "gpt")
DEFAULT_DAYS = 30
MAX_DAYS = 90
LABEL_MAX = 40
MIN_KEY_BYTES = 32

# D2 + D8. Windows are seconds.
IP_LIMIT, IP_WINDOW_SECONDS = 60, 600  # calls per address bucket, counted BEFORE the lookup
FAIL_LIMIT, FAIL_WINDOW_SECONDS = 20, 600  # invalid lookups per address bucket
GLOBAL_FAIL_LIMIT = 100  # invalid lookups across every address, same window
KEY_LIMIT, KEY_WINDOW_SECONDS = 30, 3600  # calls per key
BATCH_LIMIT, BATCH_WINDOW_SECONDS = 5, 86_400  # accepted submits per key
# Rows of a call refused by a key limit stay in the table (one row per call) but are not counted,
# so a key that keeps knocking does not extend its own block.
LIMIT_REASONS = frozenset({"key_hour_limit", "key_day_limit"})
_INT_MAX = 2_147_483_647
_LOCK_TIMEOUT = "3s"
_CREDENTIAL_ID = re.compile(r"AC_[0-9a-f]{32}")
_FORBIDDEN_LABEL_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_LOG = logging.getLogger("mia.os_assistant")


# ---------------------------------------------------------------------------------------------
# Values and small helpers


@dataclass(frozen=True)
class StoredCredential(CredentialRow):
    """``os_assistant_credentials`` as the bearer path reads it (V2-26's ``CredentialRow`` plus)."""

    workspace_id: str = ""
    scopes: str = SCOPES_TEXT
    secret_hash: str = field(default="", repr=False)
    last_used_at: datetime | None = None
    use_count: int = 0


@dataclass
class Facts:
    """What a call learned, for its audit row: counts only, filled in as the work goes."""

    bytes: int = 0
    line_count: int = 0
    proposal_id: str | None = None


@dataclass(frozen=True)
class Caller:
    """An authenticated bearer call: the credential, its trusted context and its address bucket."""

    credential: StoredCredential = field(repr=False)
    ctx: api.TrustedOwnerContext = field(repr=False)
    ip_hmac: str = field(repr=False)


def generate_secret() -> str:
    """``aosp_`` + 43 base64url characters (256 bits), shown once."""
    return SECRET_PREFIX + base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def secret_hash(secret: str) -> str:
    return credential_hash(secret, purpose="assistant")


def new_credential_id() -> str:
    return "AC_" + secrets.token_hex(16)


def reason_of(exc: BaseException) -> str:
    """The audit ``reason`` of a failed call: a ledger code or reason, else ``internal_error``."""
    if isinstance(exc, api.LedgerError):
        return (exc.reason or exc.code)[:64]
    return "internal_error"


def ip_bucket(ip: str) -> str:
    """The address a per-IP rule counts: an IPv6 /64 prefix, an IPv4 address as it is (D8).

    A client with an IPv6 allocation owns billions of addresses; counting each separately would
    let it rotate past every limit. An IPv4-mapped IPv6 address counts as the IPv4 address.
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return str(ip)  # "unknown": no peer
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network((address, 64), strict=False))
    return str(address)


def client_ip_hmac(settings: Any, ip: str) -> str:
    """HMAC-SHA256(``os_assistant_ip_hmac_key``, the address bucket), hex. Required at use."""
    raw = getattr(settings, "os_assistant_ip_hmac_key", "")
    key = raw.encode("utf-8") if isinstance(raw, str) else b""
    if len(key) < MIN_KEY_BYTES:
        _LOG.warning("os_assistant op=ip_hmac outcome=refused reason=ip_hmac_key_unset")
        raise api.LedgerError("ledger_unavailable", reason="ip_hmac_key_unset")
    return hmac.new(key, ip_bucket(ip).encode("utf-8"), "sha256").hexdigest()


# ---------------------------------------------------------------------------------------------
# The credential tables inside a ledger transaction (issue, list, revoke)

_COLUMNS = ("credential_id, owner_id, epoch, label, kind, scopes, secret_hash, created_at, "
            "expires_at, revoked_at, last_used_at, use_count")


class CredentialTables(Protocol):
    def list(self) -> list[StoredCredential]: ...
    def get(self, credential_id: str) -> StoredCredential | None: ...
    def insert(self, row: StoredCredential) -> None: ...
    def revoke(self, credential_id: str, at: datetime) -> bool: ...


def credential_tables(tx: Any) -> CredentialTables:
    """PostgreSQL through ``tx.connection`` when there is one, else the in-memory scratch (the
    same rows V2-26's ``MemoryTables.credential`` reads)."""
    return PgCredentialTables(tx) if tx.connection is not None else MemoryCredentialTables(tx)


def _stored(row: Any, workspace: str = "") -> StoredCredential:
    """Any credential row (V2-26's plain ``CredentialRow`` included) as a ``StoredCredential``."""
    if isinstance(row, StoredCredential):
        return row
    return StoredCredential(
        row.credential_id, row.owner_id, row.epoch, row.label, row.kind, row.created_at,
        row.expires_at, row.revoked_at, workspace_id=workspace)


class MemoryCredentialTables:
    """Rows in ``tx.scratch["statement_tables"]["credentials"]``, rolled back with the ledger tx."""

    def __init__(self, tx: Any) -> None:
        self._tx = tx

    def _table(self) -> dict[str, Any]:
        return self._tx.scratch.setdefault("statement_tables", {}).setdefault("credentials", {})

    def list(self) -> list[StoredCredential]:
        rows = [_stored(r, self._tx.ctx.workspace) for r in self._table().values()]
        return sorted(rows, key=lambda r: (r.created_at, r.credential_id), reverse=True)

    def get(self, credential_id: str) -> StoredCredential | None:
        row = self._table().get(credential_id)
        return None if row is None else _stored(row, self._tx.ctx.workspace)

    def insert(self, row: StoredCredential) -> None:
        table = self._table()
        if row.credential_id in table or any(
                getattr(r, "secret_hash", "") == row.secret_hash for r in table.values()):
            raise AssertionError("fake_invariant:credential_unique")
        table[row.credential_id] = row

    def revoke(self, credential_id: str, at: datetime) -> bool:
        table = self._table()
        row = table.get(credential_id)
        if row is None or row.revoked_at is not None:
            return False
        table[credential_id] = replace(row, revoked_at=at)
        return True


class PgCredentialTables:
    """``os_assistant_credentials`` on this transaction's connection (runtime role: SELECT, INSERT,
    UPDATE of ``revoked_at``/``last_used_at``/``use_count`` only)."""

    def __init__(self, tx: Any) -> None:
        self._c = tx.connection
        self._ws = tx.ctx.workspace

    def _run(self, sql: str, **params: Any):
        return self._c.execute(text(sql), {"ws": self._ws, **params})

    def _row(self, row: Any) -> StoredCredential:
        return stored_from_row(row, self._ws)

    def list(self) -> list[StoredCredential]:
        rows = self._run(
            f"SELECT {_COLUMNS} FROM assaf_os.os_assistant_credentials WHERE workspace_id = :ws "
            "ORDER BY created_at DESC, credential_id DESC").all()
        return [self._row(r) for r in rows]

    def get(self, credential_id: str) -> StoredCredential | None:
        row = self._run(
            f"SELECT {_COLUMNS} FROM assaf_os.os_assistant_credentials "
            "WHERE workspace_id = :ws AND credential_id = :id", id=credential_id).first()
        return None if row is None else self._row(row)

    def insert(self, row: StoredCredential) -> None:
        self._run(
            "INSERT INTO assaf_os.os_assistant_credentials (workspace_id, credential_id, owner_id, "
            "epoch, label, kind, scopes, secret_hash, created_at, expires_at) VALUES (:ws, :id, "
            ":owner, :epoch, :label, :kind, :scopes, :hash, :created, :expires)",
            id=row.credential_id, owner=row.owner_id, epoch=row.epoch, label=row.label,
            kind=row.kind, scopes=row.scopes, hash=row.secret_hash, created=row.created_at,
            expires=row.expires_at)

    def revoke(self, credential_id: str, at: datetime) -> bool:
        return self._run(
            "UPDATE assaf_os.os_assistant_credentials SET revoked_at = :at "
            "WHERE workspace_id = :ws AND credential_id = :id AND revoked_at IS NULL",
            id=credential_id, at=at).rowcount == 1


def stored_from_row(row: Any, workspace: str) -> StoredCredential:
    """A database row of ``_COLUMNS`` (CHAR columns come back padded)."""
    return StoredCredential(
        row.credential_id, str(row.owner_id), int(row.epoch), row.label, row.kind,
        row.created_at, row.expires_at, row.revoked_at, workspace_id=workspace,
        scopes=row.scopes, secret_hash=row.secret_hash.strip(), last_used_at=row.last_used_at,
        use_count=int(row.use_count))


# ---------------------------------------------------------------------------------------------
# The owner's side


def _item(row: StoredCredential) -> dict[str, Any]:
    return {
        "credential_id": row.credential_id, "label": row.label, "kind": row.kind,
        "created_at": iso(row.created_at), "expires_at": iso(row.expires_at),
        "last_used_at": iso(row.last_used_at), "use_count": int(row.use_count or 0),
        "revoked_at": iso(row.revoked_at),
    }


def _invalid(reason: str) -> api.LedgerError:
    return api.LedgerError("validation_error", details={"reason": reason}, reason=reason)


def _label(value: Any) -> str:
    if type(value) is not str:
        raise _invalid("invalid_label")
    label = value.strip()
    if not 1 <= len(label) <= LABEL_MAX or any(
            unicodedata.category(ch) in _FORBIDDEN_LABEL_CATEGORIES for ch in label):
        raise _invalid("invalid_label")
    return label


class AssistantCredentialService:
    """The owner's keys. The ledger context comes from V2-19's ``authorize`` only; this service
    enforces the §9 matrix itself (``authorize.require``) and never mints a context."""

    def __init__(self, ledger: Any, settings: Any, statements: Any = None, *,
                 new_id: Callable[[], str] = new_credential_id,
                 new_secret: Callable[[], str] = generate_secret) -> None:
        self._ledger = ledger
        self._settings = settings
        self._statements = (statements if statements is not None
                            else LedgerStatementService(ledger, settings))
        self._new_id = new_id
        self._new_secret = new_secret

    def _admit(self, ctx: api.TrustedOwnerContext, op: str) -> None:
        settings = self._settings
        if settings.assaf_os_assistant_enabled is not True or (
                settings.assaf_os_backend != "postgres"):
            raise api.LedgerError("operation_unavailable", reason="assistant_off")
        if str(getattr(ctx, "numeric_owner", None)) not in settings.telegram_owner_user_id_set():
            raise api.LedgerError("owner_auth_required", reason="owner_not_allowed")
        if getattr(ctx, "channel", None) != "OS":
            raise api.LedgerError("channel_operation_denied", reason="owner_session_only")
        authorize.require(ctx, op)

    def _tx(self, ctx: api.TrustedOwnerContext):
        return self._ledger.transaction(ctx, domain="statements", kind="statement_batch")

    def _listing(self, ctx: api.TrustedOwnerContext, tables: CredentialTables) -> dict[str, Any]:
        return {**binding(ctx), "credentials": [_item(r) for r in tables.list()]}

    def list(self, ctx: api.TrustedOwnerContext) -> dict[str, Any]:
        self._admit(ctx, "read")
        if "assaf-os:approve" not in ctx.scopes:
            raise api.LedgerError("scope_denied", reason="approve_scope_missing")
        with self._tx(ctx) as tx:
            return self._listing(ctx, credential_tables(tx))

    def issue(self, ctx: api.TrustedOwnerContext, *, label: Any, kind: Any,
              expires_in_days: Any = None) -> dict[str, Any]:
        """A new key. The caller has already checked the step-up token (the route does)."""
        self._admit(ctx, "manage_credentials")
        clean = _label(label)
        if kind not in KINDS or type(kind) is not str:
            raise _invalid("invalid_kind")
        days = DEFAULT_DAYS if expires_in_days is None else expires_in_days
        if type(days) is not int or not 1 <= days <= MAX_DAYS:
            raise _invalid("invalid_expiry")
        secret = self._new_secret()
        with self._tx(ctx) as tx:
            row = StoredCredential(
                self._new_id(), str(ctx.numeric_owner), ctx.authority_epoch, clean, kind,
                tx.now, tx.now + timedelta(days=days), None, workspace_id=ctx.workspace,
                scopes=SCOPES_TEXT, secret_hash=secret_hash(secret))
            credential_tables(tx).insert(row)
        _LOG.info("os_assistant op=issue outcome=ok reason=issued")
        return {**binding(ctx), **_item(row), "secret": secret}

    def _revoke_in(self, tx: Any, tables: CredentialTables, credential_id: str) -> None:
        """The key and its pending batches, on the caller's open transaction (D5)."""
        tables.revoke(credential_id, tx.now)
        self._statements.revoke_pending_for_credential(tx, credential_id)

    def revoke(self, ctx: api.TrustedOwnerContext, credential_id: Any) -> dict[str, Any]:
        self._admit(ctx, "manage_credentials")
        if type(credential_id) is not str or _CREDENTIAL_ID.fullmatch(credential_id) is None:
            raise _invalid("credential_unknown")
        with self._tx(ctx) as tx:
            tables = credential_tables(tx)
            if tables.get(credential_id) is None:
                raise _invalid("credential_unknown")
            self._revoke_in(tx, tables, credential_id)
            body = self._listing(ctx, tables)
        _LOG.info("os_assistant op=revoke outcome=ok reason=revoked")
        return body

    def revoke_all(self, ctx: api.TrustedOwnerContext) -> dict[str, Any]:
        """Every key of the workspace, expired ones included, in one transaction."""
        self._admit(ctx, "manage_credentials")
        with self._tx(ctx) as tx:
            tables = credential_tables(tx)
            for row in tables.list():
                self._revoke_in(tx, tables, row.credential_id)
            body = self._listing(ctx, tables)
        _LOG.info("os_assistant op=revoke_all outcome=ok reason=revoked_all")
        return body


# ---------------------------------------------------------------------------------------------
# The bearer side: the store (limits and audit on os_assistant_audit)


class AssistantStore(Protocol):
    """Context-free access to ``os_assistant_credentials`` and ``os_assistant_audit``.

    ``PgAssistantStore`` is the production implementation (the ledger runtime role); the test
    suite runs one set of tests on it and on an in-memory double.
    """

    def ip_calls(self, ip_hmac: str, since: datetime) -> int: ...
    def find(self, secret_hash: str) -> StoredCredential | None: ...
    def invalid_lookup(self, *, ip_hmac: str, credential_id: str | None, reason: str,
                       now: datetime) -> str | None: ...
    def call(self, credential: StoredCredential, *, ip_hmac: str, operation: str,
             now: Callable[[], datetime], work: Callable[[Facts], Any]) -> Any: ...


class PgAssistantStore:
    """The audit-table limiter on the ledger runtime DSN (D1)."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    @property
    def _workspace(self) -> str:
        return self._settings.assaf_os_workspace_id

    def _engine(self):
        dsn = getattr(self._settings, "owner_os_ledger_database_url", "") or ""
        main = getattr(self._settings, "database_url", "") or ""
        if not dsn:
            raise refuse("ledger_dsn_missing")
        if not main:
            raise refuse("main_dsn_missing")
        guard_for(dsn, main).ensure()
        return engine_for(dsn)

    @contextmanager
    def _begin(self):
        """One short transaction as the unimpersonated runtime role, with bounded waits."""
        engine = self._engine()
        try:
            with engine.begin() as conn:
                who = conn.execute(text(
                    "SELECT current_user::text AS current_role, session_user::text AS session_role"
                )).one()
                check_identity(who.current_role, who.session_role)
                conn.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
                conn.execute(text(f"SET LOCAL statement_timeout = '{_LOCK_TIMEOUT}'"))
                yield conn
        except api.LedgerError:
            raise
        except SQLAlchemyError as exc:
            raise database_error(exc) from None

    def _lock(self, conn: Any, name: str) -> None:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                     {"key": f"assaf-os-assistant:{self._workspace}:{name}"})

    def _count(self, conn: Any, where: str, **params: Any) -> int:
        return int(conn.execute(text(
            "SELECT count(*) FROM assaf_os.os_assistant_audit WHERE workspace_id = :ws AND "
            + where), {"ws": self._workspace, **params}).scalar_one())

    def _insert(self, conn: Any, *, credential_id: str | None, moment: datetime, operation: str,
                reason: str, ok: bool, facts: Facts, ip_hmac: str) -> None:
        conn.execute(text(
            "INSERT INTO assaf_os.os_assistant_audit (workspace_id, audit_id, credential_id, "
            "occurred_at, operation, reason, ok, line_count, bytes, proposal_id, client_ip_hmac) "
            "VALUES (:ws, CAST(:audit AS uuid), :cid, :at, :op, :reason, :ok, :lines, :bytes, "
            ":pid, :ip)"),
            {"ws": self._workspace, "audit": str(uuid.uuid4()), "cid": credential_id,
             "at": moment, "op": operation, "reason": reason, "ok": ok,
             "lines": max(0, min(int(facts.line_count), _INT_MAX)),
             "bytes": max(0, min(int(facts.bytes), _INT_MAX)),
             "pid": facts.proposal_id, "ip": ip_hmac})

    def ip_calls(self, ip_hmac: str, since: datetime) -> int:
        with self._begin() as conn:
            return self._count(conn, "client_ip_hmac = :ip AND occurred_at >= :since",
                               ip=ip_hmac, since=since)

    def find(self, secret_hash: str) -> StoredCredential | None:
        with self._begin() as conn:
            row = conn.execute(text(
                f"SELECT {_COLUMNS} FROM assaf_os.os_assistant_credentials "
                "WHERE workspace_id = :ws AND secret_hash = :hash"),
                {"ws": self._workspace, "hash": secret_hash}).first()
        return None if row is None else stored_from_row(row, self._workspace)

    def invalid_lookup(self, *, ip_hmac: str, credential_id: str | None, reason: str,
                       now: datetime) -> str | None:
        """Count, then record, under one lock. ``"ip"`` or ``"global"`` when over: no row."""
        since = now - timedelta(seconds=FAIL_WINDOW_SECONDS)
        with self._begin() as conn:
            self._lock(conn, "invalid-lookups")
            if self._count(conn, "operation = 'bearer' AND client_ip_hmac = :ip AND "
                           "occurred_at >= :since", ip=ip_hmac, since=since) >= FAIL_LIMIT:
                return "ip"
            if self._count(conn, "operation = 'bearer' AND occurred_at >= :since",
                           since=since) >= GLOBAL_FAIL_LIMIT:
                return "global"
            self._insert(conn, credential_id=credential_id, moment=now, operation="bearer",
                         reason=reason, ok=False, facts=Facts(), ip_hmac=ip_hmac)
        return None

    def call(self, credential: StoredCredential, *, ip_hmac: str, operation: str,
             now: Callable[[], datetime], work: Callable[[Facts], Any]) -> Any:
        failure: BaseException | None = None
        result: Any = None
        with self._begin() as conn:
            self._lock(conn, f"key:{credential.credential_id}")
            moment = now()
            counted = ("credential_id = :cid AND reason NOT IN ('key_hour_limit', "
                       "'key_day_limit') AND ")
            hour = self._count(conn, counted + "occurred_at >= :since",
                               cid=credential.credential_id,
                               since=moment - timedelta(seconds=KEY_WINDOW_SECONDS))
            day = (self._count(conn, counted + "operation = 'submit' AND ok AND "
                               "occurred_at >= :since", cid=credential.credential_id,
                               since=moment - timedelta(seconds=BATCH_WINDOW_SECONDS))
                   if operation == "submit" else 0)
            facts = Facts()
            if hour >= KEY_LIMIT:
                failure = api.LedgerError("rate_limited", reason="key_hour_limit")
            elif operation == "submit" and day >= BATCH_LIMIT:
                failure = api.LedgerError("rate_limited", reason="key_day_limit")
            else:
                try:
                    result = work(facts)
                except Exception as exc:  # recorded below, re-raised once the row is committed
                    failure = exc
            self._insert(conn, credential_id=credential.credential_id, moment=moment,
                         operation=operation, reason="ok" if failure is None
                         else reason_of(failure), ok=failure is None, facts=facts, ip_hmac=ip_hmac)
            conn.execute(text(
                "UPDATE assaf_os.os_assistant_credentials SET last_used_at = :at, "
                "use_count = use_count + 1 WHERE workspace_id = :ws AND credential_id = :cid"),
                {"ws": self._workspace, "at": moment, "cid": credential.credential_id})
        if failure is not None:
            raise failure
        return result


# ---------------------------------------------------------------------------------------------
# The bearer side: the gateway


def _uniform(reason: str) -> api.LedgerError:
    _LOG.info("os_assistant op=auth outcome=assistant_auth_required reason=%s", reason)
    return api.LedgerError("assistant_auth_required", reason=reason)


def _limited(reason: str) -> api.LedgerError:
    _LOG.info("os_assistant op=auth outcome=rate_limited reason=%s", reason)
    return api.LedgerError("rate_limited", reason=reason)


def check_enabled(settings: Any) -> None:
    """Step 0: a disabled feature is the uniform 401 with no database and no ledger touched."""
    if settings.assaf_os_assistant_enabled is not True:
        raise _uniform("assistant_off")


class AssistantGateway:
    """§17.2's bearer enforcement order, then the per-key limits and the audit row."""

    def __init__(self, settings: Any, *, store: AssistantStore, statements: Any,
                 revoke_watermark: Callable[[str], datetime | None] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._settings = settings
        self._store = store
        self.statements = statements
        self._watermark = revoke_watermark or db_revoke_watermark
        self._clock = clock or (lambda: datetime.now(UTC))

    def authenticate(self, *, cookie: bool, authorization: Sequence[str], ip: str) -> Caller:
        """The caller behind a bearer header, or ``assistant_auth_required`` / ``rate_limited``.

        Order: (0) the feature switch, no database touched; (1) a cookie is 401; (2) the header
        must be exactly ``Bearer aosp_`` + 43 base64url characters; (3) the per-IP count, before any
        lookup; (4) the hash lookup and validity. An invalid lookup is counted against the
        per-address and the global budget and answers the uniform 401, or 429 once a budget is
        spent; a valid key is never blocked by either budget.
        """
        settings = self._settings
        check_enabled(settings)
        if cookie:
            raise _uniform("cookie_present")
        if len(authorization) != 1 or BEARER.fullmatch(authorization[0]) is None:
            raise _uniform("header_invalid")
        ip_hmac = client_ip_hmac(settings, ip)
        now = self._clock()
        if self._store.ip_calls(ip_hmac, now - timedelta(seconds=IP_WINDOW_SECONDS)) >= IP_LIMIT:
            raise _limited("ip_limit")
        found = self._store.find(secret_hash(authorization[0][len("Bearer "):]))
        reason, ctx = self._validity(found, now)
        if reason is not None or ctx is None or found is None:
            over = self._store.invalid_lookup(
                ip_hmac=ip_hmac, credential_id=None if found is None else found.credential_id,
                reason=reason or "key_invalid", now=now)
            if over is not None:
                raise _limited("auth_budget_" + over)
            raise _uniform(reason or "key_invalid")
        return Caller(found, ctx, ip_hmac)

    def _validity(self, found: StoredCredential | None, now: datetime,
                  ) -> tuple[str | None, api.TrustedOwnerContext | None]:
        """Step 4. Every failure is one reason code; the response is the same uniform 401."""
        settings = self._settings
        if found is None:
            return "key_unknown", None
        if found.revoked_at is not None:
            return "key_revoked", None
        if found.expires_at <= now:
            return "key_expired", None
        if found.owner_id not in settings.telegram_owner_user_id_set():
            return "key_owner", None
        if settings.kill_switch:
            return "kill_switch", None
        mark = self._watermark(found.owner_id)  # the owner's revoke-all; unreadable fails closed
        if mark is not None and found.created_at <= mark:
            return "key_revoked_all", None
        try:
            # The workspace, the epoch (I12) and the fixed scopes are checked here, by V2-19.
            return None, authorize.from_assistant(found, settings)
        except api.LedgerError as exc:
            if exc.code != "assistant_auth_required":
                raise
            return exc.reason or "key_invalid", None

    def run(self, caller: Caller, operation: str, work: Callable[[Facts], Any]) -> Any:
        """The per-key limits, ``work`` and the call's one audit row, in the store's transaction."""
        return self._store.call(caller.credential, ip_hmac=caller.ip_hmac, operation=operation,
                                now=self._clock, work=work)


def build_gateway(settings: Any, statements: Any) -> AssistantGateway:
    """The production gateway: the ledger runtime DSN's store and the live revoke-all watermark."""
    return AssistantGateway(settings, store=PgAssistantStore(settings), statements=statements)
