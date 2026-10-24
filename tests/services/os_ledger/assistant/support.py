"""A deterministic assistant world for the V2-37 tests: a statements ``World`` plus the assistant
service, the bearer gateway and an audit/limiter store.

Everything is synthetic. ``AWorld`` wraps V2-26's ``World`` (FakeLedger or the V2-16 ``PgLedger`` on
a disposable PostgreSQL) and adds:

* ``service``: ``AssistantCredentialService`` (the owner side: issue, list, revoke, revoke-all);
* ``gateway``: ``AssistantGateway`` (the bearer side) over ``store``;
* ``store``: the real ``PgAssistantStore`` on PostgreSQL, ``MemoryAssistantStore`` (below) on the
  FakeLedger. The memory store is a test double of the same protocol; the parametrized suites run
  one set of tests on both so they cannot drift apart.
* ``clock``: the one clock the gateway and the store read, advanced by tests.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services import assistant_keys as ac
from app.services.os_ledger.statement_tables import tables_for

from tests.services.os_ledger.statements.support import World

ORIGIN = "https://owner.assafweb.test"
IP_KEY = "i" * 32
STEP_UP_SECRET = "s" * 32
IP_A = "203.0.113.10"
IP_B = "203.0.113.11"
AUDIT_COLUMNS = ("credential_id", "occurred_at", "operation", "reason", "ok", "line_count",
                 "bytes", "proposal_id", "client_ip_hmac")


class Clock:
    """The real time at construction (an expiry is judged against the real clock), then manual."""

    def __init__(self) -> None:
        self.value = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class MemoryAssistantStore:
    """The ``AssistantStore`` protocol over plain lists and one lock per key (FakeLedger worlds)."""

    def __init__(self, world: World) -> None:
        self._world = world
        self.rows: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._keys: dict[str, threading.Lock] = {}

    def _credentials(self, tx: Any) -> dict[str, Any]:
        return tx.scratch.setdefault("statement_tables", {}).setdefault("credentials", {})

    def _row(self, **fields: Any) -> dict[str, Any]:
        row = {"credential_id": None, "line_count": 0, "bytes": 0, "proposal_id": None}
        row.update(fields)
        return row

    def ip_calls(self, ip_hmac: str, since: datetime) -> int:
        with self._lock:
            return sum(1 for r in self.rows
                       if r["client_ip_hmac"] == ip_hmac and r["occurred_at"] >= since)

    def find(self, secret_hash: str):
        with self._world.scratch_tx() as tx:
            for row in self._credentials(tx).values():
                if getattr(row, "secret_hash", "") == secret_hash:
                    return row
        return None

    def invalid_lookup(self, *, ip_hmac: str, credential_id: str | None, reason: str,
                       now: datetime) -> str | None:
        since = now - timedelta(seconds=ac.FAIL_WINDOW_SECONDS)
        with self._lock:
            failures = [r for r in self.rows if r["operation"] == "bearer"
                        and r["occurred_at"] >= since]
            if sum(1 for r in failures if r["client_ip_hmac"] == ip_hmac) >= ac.FAIL_LIMIT:
                return "ip"
            if len(failures) >= ac.GLOBAL_FAIL_LIMIT:
                return "global"
            self.rows.append(self._row(
                credential_id=credential_id, occurred_at=now, operation="bearer", reason=reason,
                ok=False, client_ip_hmac=ip_hmac))
        return None

    def call(self, credential, *, ip_hmac: str, operation: str, now: Callable[[], datetime],
             work: Callable[[ac.Facts], Any]) -> Any:
        with self._lock:
            gate = self._keys.setdefault(credential.credential_id, threading.Lock())
        with gate:
            moment = now()
            mine = [r for r in self.rows if r["credential_id"] == credential.credential_id]
            counted = [r for r in mine if r["reason"] not in ac.LIMIT_REASONS]
            hour = sum(1 for r in counted
                       if r["occurred_at"] >= moment - timedelta(seconds=ac.KEY_WINDOW_SECONDS))
            day = sum(1 for r in counted if r["operation"] == "submit" and r["ok"]
                      and r["occurred_at"] >= moment - timedelta(seconds=ac.BATCH_WINDOW_SECONDS))
            refusal = None
            if hour >= ac.KEY_LIMIT:
                refusal = "key_hour_limit"
            elif operation == "submit" and day >= ac.BATCH_LIMIT:
                refusal = "key_day_limit"
            facts, failure = ac.Facts(), None
            result = None
            if refusal is None:
                try:
                    result = work(facts)
                except Exception as exc:  # recorded, then re-raised after the row is written
                    failure = exc
            else:
                failure = ac.api.LedgerError("rate_limited", reason=refusal)
            reason = "ok" if failure is None else ac.reason_of(failure)
            self.rows.append(self._row(
                credential_id=credential.credential_id, occurred_at=moment, operation=operation,
                reason=reason, ok=failure is None, line_count=facts.line_count,
                bytes=facts.bytes, proposal_id=facts.proposal_id, client_ip_hmac=ip_hmac))
            with self._world.scratch_tx() as tx:
                table = self._credentials(tx)
                held = table.get(credential.credential_id)
                if held is not None:
                    table[credential.credential_id] = replace(
                        held, last_used_at=moment, use_count=getattr(held, "use_count", 0) + 1)
            if failure is not None:
                raise failure
            return result


class SpyStore:
    """Counts the store methods a gateway reaches (``calls``); everything else passes through."""

    METHODS = frozenset({"ip_calls", "find", "invalid_lookup", "call"})

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: Counter[str] = Counter()

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)
        if name not in self.METHODS:
            return target

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.calls[name] += 1
            return target(*args, **kwargs)

        return counted


class AWorld:
    """``AWorld("fake")`` or ``AWorld("pg", request=request)``; one object per test."""

    def __init__(self, backend: str = "fake", *, request: Any = None, **settings: Any) -> None:
        values: dict[str, Any] = dict(
            assaf_os_assistant_enabled=True, os_assistant_ip_hmac_key=IP_KEY,
            os_step_up_secret=STEP_UP_SECRET, owner_os_origin=ORIGIN)
        values.update(settings)
        # ``request`` must stay a local name of this frame's caller chain: see World.
        self.w = World(backend, request=request, **values)
        self.kind = backend
        self.clock = Clock()
        self.counter = Counter()
        self.statements = self.w.service
        self.service = ac.AssistantCredentialService(self.w.ledger, self.w.config, self.statements)
        self.store = SpyStore(MemoryAssistantStore(self.w) if backend == "fake"
                              else ac.PgAssistantStore(self.w.config))
        self.gateway = self.make_gateway()

    def make_gateway(self, **over: Any) -> ac.AssistantGateway:
        values: dict[str, Any] = dict(
            store=self.store, statements=self.statements,
            revoke_watermark=lambda owner_id: self.w.watermark["value"], clock=self.clock)
        values.update(over)
        return ac.AssistantGateway(self.w.config, **values)

    @property
    def config(self):
        return self.w.config

    # ---- owner side -----------------------------------------------------------------------

    def issue(self, *, label: str = "Claude desktop", kind: str = "claude",
              days: int | None = None, ctx=None) -> dict[str, Any]:
        return self.service.issue(ctx or self.w.owner, label=label, kind=kind,
                                  expires_in_days=days)

    def listing(self, ctx=None) -> dict[str, Any]:
        return self.service.list(ctx or self.w.reader)

    def revoke(self, credential_id: str, ctx=None) -> dict[str, Any]:
        return self.service.revoke(ctx or self.w.owner, credential_id)

    def revoke_all(self, ctx=None) -> dict[str, Any]:
        return self.service.revoke_all(ctx or self.w.owner)

    # ---- bearer side ----------------------------------------------------------------------

    def ip_hmac(self, ip: str) -> str:
        return ac.client_ip_hmac(self.w.config, ip)

    def authenticate(self, secret: str | None, *, ip: str = IP_A, cookie: bool = False):
        header = () if secret is None else (f"Bearer {secret}",)
        return self.gateway.authenticate(cookie=cookie, authorization=header, ip=ip)

    def paste_as(self, issued: dict[str, Any], batch_body: Any, *, kind: str = "claude"):
        """A pending batch of this key, through the V2-26 service (not through a route)."""
        from tests.services.os_ledger.statements.support import CLAUDE, GPT
        ctx = self.w.assistant(kind, issued["credential_id"])
        return self.w.paste(batch_body, ctx=ctx, source=CLAUDE if kind == "claude" else GPT)

    # ---- observers ------------------------------------------------------------------------

    def credential_rows(self) -> list[dict[str, Any]]:
        """Every stored credential as plain values (with the secret hash)."""
        if self.kind == "fake":
            with self.w.scratch_tx() as tx:
                held = dict(tx.scratch.get("statement_tables", {}).get("credentials", {}))
            return [{"credential_id": r.credential_id, "owner_id": r.owner_id, "epoch": r.epoch,
                     "label": r.label, "kind": r.kind, "scopes": r.scopes,
                     "secret_hash": r.secret_hash, "created_at": r.created_at,
                     "expires_at": r.expires_at, "revoked_at": r.revoked_at,
                     "last_used_at": r.last_used_at, "use_count": r.use_count}
                    for r in sorted(held.values(), key=lambda r: r.credential_id)]
        from sqlalchemy import text
        with self.w._admin_engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT credential_id, owner_id, epoch, label, kind, scopes, secret_hash, "
                "created_at, expires_at, revoked_at, last_used_at, use_count "
                "FROM assaf_os.os_assistant_credentials ORDER BY credential_id")).mappings().all()
        return [{k: (v.strip() if isinstance(v, str) else v) for k, v in dict(r).items()}
                for r in rows]

    def credential(self, credential_id: str) -> dict[str, Any]:
        return next(r for r in self.credential_rows() if r["credential_id"] == credential_id)

    def audit(self) -> list[dict[str, Any]]:
        """Every audit row as plain values, oldest first."""
        if self.kind == "fake":
            return [dict(r) for r in self.store.rows]
        from sqlalchemy import text
        with self.w._admin_engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT credential_id, occurred_at, operation, reason, ok, line_count, bytes, "
                "proposal_id, client_ip_hmac FROM assaf_os.os_assistant_audit "
                "ORDER BY occurred_at, audit_id")).mappings().all()
        return [{k: (v.strip() if isinstance(v, str) else v) for k, v in dict(r).items()}
                for r in rows]

    def seed_audit(self, count: int, *, ip_hmac: str, operation: str = "bearer",
                   age: float = 0.0, ok: bool = False, reason: str = "key_unknown",
                   credential_id: str | None = None) -> None:
        """Rows as if ``count`` earlier calls had been made ``age`` seconds ago."""
        moment = self.clock() - timedelta(seconds=age)
        if self.kind == "fake":
            for _ in range(count):
                self.store.rows.append({
                    "credential_id": credential_id, "occurred_at": moment, "operation": operation,
                    "reason": reason, "ok": ok, "line_count": 0, "bytes": 0, "proposal_id": None,
                    "client_ip_hmac": ip_hmac})
            return
        import uuid

        from sqlalchemy import text
        with self.w._admin_engine.begin() as conn:
            for _ in range(count):
                conn.execute(text(
                    "INSERT INTO assaf_os.os_assistant_audit (workspace_id, audit_id, "
                    "credential_id, occurred_at, operation, reason, ok, line_count, bytes, "
                    "proposal_id, client_ip_hmac) VALUES (:ws, :aid, :cid, :at, :op, :reason, "
                    ":ok, 0, 0, NULL, :ip)"),
                    {"ws": self.w.config.assaf_os_workspace_id, "aid": str(uuid.uuid4()),
                     "cid": credential_id, "at": moment, "op": operation, "reason": reason,
                     "ok": ok, "ip": ip_hmac})

    def proposals(self) -> list[dict[str, Any]]:
        return self.w.rows("os_proposals")

    def receipts(self) -> list[dict[str, Any]]:
        return self.w.rows("os_receipts")

    def everything(self) -> str:
        return "\n".join([
            self.w.everything(), repr(self.credential_rows()), repr(self.audit())])

    def statement_tables(self):
        with self.w.scratch_tx() as tx:
            return tables_for(tx).dump()
