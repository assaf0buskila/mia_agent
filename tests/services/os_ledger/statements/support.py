"""A deterministic statement world for the V2-26 tests: a ledger, a service, three contexts.

Everything here is synthetic. ``World`` wires the real ``LedgerStatementService`` to either ledger:

* ``"fake"``: the ``FakeLedger`` with in-memory statement tables (``tx.scratch``);
* ``"pg"``: the V2-16 ``PgLedger`` on a disposable PostgreSQL, with the §4 tables on
  ``tx.connection`` (``MIA_OS_LEDGER_TEST_POSTGRES_URL``; never skipped when it is set).

Both run on a pinned pepper and the conformance suite's frozen clock. The knobs the ledger reads
(kill switch, freeze, backend, flag) are the ``Settings`` object the service reads, so one
assignment moves both. A test written against ``World`` runs unchanged on the two.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from app.api import owner_sessions
from app.api.owner_sessions import OwnerSessionPrincipal
from app.core.config import MiaEnv, Settings
from app.services.os_ledger import api, authorize
from app.services.os_ledger.statement_tables import CredentialRow, tables_for
from app.services.os_ledger.statements import LedgerStatementService, pepper_fingerprint

from tests.fakes.contexts import for_worker, make_ctx
from tests.fakes.os_ledger import FakeLedger
from tests.fakes.os_ledger_builders import commit_direct, expense
from tests.fakes.os_ledger_env import NOW, OWNER_ID, WORKSPACE, Knobs, LedgerEnv
from tests.unit.statements.helpers import PEPPER, raw_batch, raw_line

PG_URL_ENV = "MIA_OS_LEDGER_TEST_POSTGRES_URL"
OWNER = int(OWNER_ID)
CRED = "AC_" + "a" * 32
CRED_B = "AC_" + "b" * 32
OWNER_SCOPES = frozenset({"assaf-os:read", "assaf-os:propose", "assaf-os:approve"})
ASSISTANT_SCOPES = frozenset({"merchant_rules:read", "own_proposals:read", "statement:propose"})
PASTE = "ASSISTANT_PASTE"
CLAUDE = "ASSISTANT_CLAUDE"
GPT = "ASSISTANT_GPT"
HEX64 = re.compile(r"[0-9a-f]{64}")

# ---- trusted contexts (V2-19) -----------------------------------------------------------------
# ``authorize.require`` refuses any context ``authorize`` did not mint (review r1), so the service
# runs on contexts from V2-19's own constructors: ``World.owner``, ``reader``, ``worker`` and
# ``assistant()``. ``World.ctx`` covers the shapes a constructor cannot produce (a wrong scope set,
# another epoch, a channel with no constructor to reach) by registering a ``make_ctx`` context, as
# ``tests/services/os_ledger/test_channel_matrix.py`` does. Its defaults are the well-formed shape
# of each channel, so a refusal comes from the §9 cell and never from a malformed context.
_CHANNEL_SHAPE: dict[str, tuple[frozenset[str], str | None]] = {
    "OS": (OWNER_SCOPES, None),
    "MIA": (frozenset(), None),
    "TELEGRAM_APPROVAL": (frozenset(), None),
    "ASSISTANT_CLAUDE": (ASSISTANT_SCOPES, CRED),
    "ASSISTANT_GPT": (ASSISTANT_SCOPES, CRED),
    "WORKER": (frozenset({"retention"}), None),
}


def session_principal(scopes: Iterable[str] = OWNER_SCOPES, *, proven: bool,
                      ) -> OwnerSessionPrincipal:
    """An owner session as a route gets it (``proven``: the Origin and CSRF checks passed)."""
    principal = OwnerSessionPrincipal(
        owner_id=OWNER_ID, scopes=frozenset(scopes), session_hash="h" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1))
    return owner_sessions._attach_mutation_proof(principal) if proven else principal


def uuid_v4(number: int) -> str:
    return str(UUID(int=number, version=4))


class Counter:
    def __init__(self, base: int = 0x2000) -> None:
        self.n = base

    def __call__(self) -> str:
        self.n += 1
        return uuid_v4(self.n)


def encode(batch: Mapping[str, Any]) -> bytes:
    """Compact UTF-8 JSON, exactly what the assistant is told to produce (E4.4)."""
    return json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def make_settings(**over: Any) -> Settings:
    values: dict[str, Any] = dict(
        _env_file=None, env=MiaEnv.TEST, telegram_owner_user_ids=OWNER_ID, kill_switch=False,
        assaf_os_backend="postgres", assaf_os_ledger_enabled=True, assaf_os_authority_epoch=1,
        assaf_os_freeze=False, assaf_os_statements_enabled=True, assaf_os_workspace_id=WORKSPACE,
        os_statement_pepper=PEPPER.decode("ascii"), owner_undo_window_seconds=30)
    values.update(over)
    return Settings(**values)


def pg_url() -> str | None:
    return os.environ.get(PG_URL_ENV) or None


class World:
    def __init__(self, backend: str = "fake", *, request: Any = None, pin: bool = True,
                 statements: str = "active", **settings: Any) -> None:
        # ``request`` must stay a local name: the PG fixture registers its teardown from the
        # caller's frame (tests/services/os_ledger/conftest.py).
        self.kind = backend
        self.config = make_settings(**settings)
        self.new_id = Counter()
        self.watermark: dict[str, Any] = {"value": None}
        if backend == "fake":
            self._start_fake(pin, statements)
        else:
            self._start_pg(request, pin, statements)
        self.service = LedgerStatementService(
            self.ledger, self.config, pin_reader=self._pin_reader,
            revoke_watermark=lambda owner_id: self.watermark["value"])
        self.owner = authorize.from_owner_mutation(session_principal(proven=True), self.config)
        self.reader = authorize.from_owner_read(session_principal(proven=False), self.config)

    # ---- the two ledgers ------------------------------------------------------------------

    def _start_fake(self, pin: bool, statements: str) -> None:
        config = self.config
        self.ledger = FakeLedger(
            clock=lambda: NOW, uuid4=self.new_id,
            settings_epoch=lambda: config.assaf_os_authority_epoch,
            backend=lambda: config.assaf_os_backend, kill_switch=lambda: config.kill_switch,
            freeze=lambda: config.assaf_os_freeze,
            undo_window_s=lambda: config.owner_undo_window_seconds,
            enabled=lambda: config.assaf_os_ledger_enabled)
        self.admin = self.ledger
        self.env = LedgerEnv("fake", self.ledger, Knobs(), rows=self.ledger.rows,
                             version=self.ledger.version, now=self.ledger._now,
                             advance=self.ledger.advance)
        self._pin_reader = lambda tx: tx.data["statement_pepper_fp"]
        self._start(pin, statements)

    def _start_pg(self, request: Any, pin: bool, statements: str) -> None:
        from app.db.os_ledger_migrate import RUNTIME_ROLE
        from app.services.os_ledger import core
        from sqlalchemy.engine import make_url

        from tests.services.os_ledger.conftest import build_pg_env

        url = pg_url()
        assert url, f"{PG_URL_ENV} is not set"
        base = build_pg_env(url)  # one-time roles and grants, then a clean ledger
        self._admin_engine = build_pg_env.state[url]["admin"]
        parsed = make_url(url)
        self.config.database_url = parsed.set(
            username="v216_main", password=None).render_as_string(hide_password=False)
        self.config.owner_os_ledger_database_url = parsed.set(
            username=RUNTIME_ROLE, password=None).render_as_string(hide_password=False)
        self.ledger = core.build_ledger(self.config, clock=base.now)
        self.admin = base
        ledger = self.ledger

        class Impl:
            def __getattr__(self, name: str) -> Any:
                if name in {"ensure_workspace", "state", "transition", "bump_epoch",
                            "pin_statement_pepper"}:
                    return getattr(base, name)
                return getattr(ledger, name)

        self.env = LedgerEnv("pg", Impl(), Knobs(), rows=base.rows, version=base.version,
                             now=base.now, advance=base.advance)
        self._pin_reader = None  # the production reader: the workspace row on tx.connection
        self._start(pin, statements)

    def _start(self, pin: bool, statements: str) -> None:
        self.admin.ensure_workspace(WORKSPACE, OWNER_ID, 1)
        for domain, state in (("core", "active"), ("statements", statements)):
            if state != "shadow":
                self.admin.transition(WORKSPACE, domain, state, expected_epoch=1,
                                      actor_owner_id=OWNER_ID)
        if pin:
            self.admin.pin_statement_pepper(WORKSPACE, pepper_fingerprint(PEPPER))

    # ---- contexts -------------------------------------------------------------------------

    @property
    def worker(self) -> api.TrustedOwnerContext:
        """The retention job's context, minted on each use (a worker context lives 120 s)."""
        return authorize.for_worker("retention", self.config)

    def assistant(self, kind: str = "claude", credential: str = CRED):
        held = SimpleNamespace(
            credential_id=credential, kind=kind, owner_id=OWNER_ID, workspace_id=WORKSPACE,
            epoch=self.config.assaf_os_authority_epoch, scopes=ASSISTANT_SCOPES, revoked_at=None,
            expires_at=datetime.now(UTC) + timedelta(days=1))
        return authorize.from_assistant(held, self.config)

    def ctx(self, channel: str, **over: Any):
        scopes, credential = _CHANNEL_SHAPE[channel]
        fields: dict[str, Any] = dict(
            owner=OWNER, workspace=WORKSPACE, scopes=scopes, credential_id=credential,
            browser_mutation_verified=channel == "OS")
        fields.update(over)
        return authorize._register(make_ctx(channel, **fields))

    # ---- the clock and the authority -------------------------------------------------------

    def now(self):
        return self.env.now()

    def advance(self, seconds: float) -> None:
        self.env.advance(seconds)

    def bump_epoch(self) -> None:
        """Move the world to epoch 2 (settings and workspace together)."""
        self.config.assaf_os_authority_epoch = 2
        self.admin.bump_epoch(WORKSPACE, expected_epoch=1)

    def fence_statements(self) -> None:
        self.admin.transition(WORKSPACE, "statements", "fenced", expected_epoch=1,
                              actor_owner_id=OWNER_ID)

    def set_pin(self, fingerprint: str | None) -> None:
        """Tamper with the pinned fingerprint (the privileged side may; the runtime may not)."""
        if self.kind == "fake":
            with self.scratch_tx() as tx:
                tx.data["statement_pepper_fp"] = fingerprint
            return
        from sqlalchemy import text
        with self._admin_engine.begin() as conn:
            conn.execute(text("UPDATE assaf_os.os_workspaces SET statement_pepper_fp = :fp "
                              "WHERE workspace_id = :ws"), {"fp": fingerprint, "ws": WORKSPACE})

    # ---- seeding --------------------------------------------------------------------------

    def observer(self):
        """A WORKER context at the current epoch, for transactions that only look or seed.

        The expiry admission skips the kill switch, the backend, the authority states and the
        enabled flag, so a test can look at the tables whatever knob it has turned.
        """
        return for_worker("retention", owner=OWNER, workspace=WORKSPACE,
                          epoch=self.config.assaf_os_authority_epoch)

    @contextmanager
    def scratch_tx(self):
        with self.ledger.transaction(self.observer(), domain="statements",
                                     kind="statement_batch", purpose="expire") as tx:
            yield tx

    def credential(self, credential_id: str = CRED, *, label: str = "Claude desktop",
                   revoked: bool = False) -> None:
        row = CredentialRow(
            credential_id=credential_id, owner_id=OWNER_ID, epoch=1, label=label, kind="claude",
            created_at=NOW - timedelta(days=1), expires_at=NOW + timedelta(days=30),
            revoked_at=NOW if revoked else None)
        if self.kind == "fake":
            with self.scratch_tx() as tx:
                tx.scratch.setdefault("statement_tables", {}).setdefault(
                    "credentials", {})[credential_id] = row
            return
        from sqlalchemy import text
        with self._admin_engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO assaf_os.os_assistant_credentials (workspace_id, credential_id, "
                "owner_id, epoch, label, kind, scopes, secret_hash, created_at, expires_at, "
                "revoked_at) VALUES (:ws, :id, :owner, 1, :label, 'claude', "
                "'merchant_rules:read own_proposals:read statement:propose', :hash, :created, "
                ":expires, :revoked)"),
                {"ws": WORKSPACE, "id": credential_id, "owner": OWNER_ID, "label": label,
                 "hash": hashlib.sha256(credential_id.encode()).hexdigest(),
                 "created": row.created_at, "expires": row.expires_at, "revoked": row.revoked_at})

    def revoke(self, credential_id: str = CRED) -> None:
        if self.kind == "fake":
            with self.scratch_tx() as tx:
                table = tx.scratch["statement_tables"]["credentials"]
                table[credential_id] = dataclasses.replace(table[credential_id],
                                                           revoked_at=tx.now)
            return
        from sqlalchemy import text
        with self._admin_engine.begin() as conn:
            conn.execute(text("UPDATE assaf_os.os_assistant_credentials SET revoked_at = :now "
                              "WHERE workspace_id = :ws AND credential_id = :id"),
                         {"now": self.now(), "ws": WORKSPACE, "id": credential_id})

    def drop_batch_lines(self) -> None:
        """A restore: FORK-BACKUP excludes the batch-line data."""
        if self.kind == "fake":
            with self.scratch_tx() as tx:
                tx.scratch["statement_tables"]["batch_lines"].clear()
            return
        from sqlalchemy import text
        with self._admin_engine.begin() as conn:
            conn.execute(text("DELETE FROM assaf_os.os_statement_batch_lines"))

    def hand_expense(self, record_id: str, *, vendor: str, amount: int, day: str,
                     currency: str = "ILS", source_key: str | None = None, **over: Any) -> None:
        mutation = expense(record_id, source_key or f"manual:{record_id}", vendor=vendor,
                           amount_ils=amount, date=day, currency=currency, **over)
        commit_direct(self.env, self.ctx("OS", scopes=OWNER_SCOPES | {"assaf-os:direct"}),
                      [mutation], request_id=self.new_id())

    @staticmethod
    def _wire_expense(row: Mapping[str, Any]) -> dict[str, Any]:
        return {**row["inputs"], "expense_id": row["record_id"], "revision": row["revision"]}

    def expense_record(self, record_id: str) -> dict[str, Any] | None:
        return next((self._wire_expense(r) for r in self.rows("os_expenses")
                     if r["record_id"] == record_id), None)

    def edit_expense(self, record_id: str, **changes: Any) -> None:
        record = self.expense_record(record_id)
        assert record is not None
        after = {**record, **changes, "revision": record["revision"] + 1}
        mutation = api.Mutation("expenses", record_id, "upsert", record, after)
        commit_direct(self.env, self.ctx("OS", scopes=OWNER_SCOPES | {"assaf-os:direct"}),
                      [mutation], request_id=self.new_id())

    def expenses(self, prefix: str = "stmt:") -> list[dict[str, Any]]:
        return [self._wire_expense(r) for r in self.rows("os_expenses")
                if r["deleted_at"] is None and r["source_key"].startswith(prefix)]

    # ---- statement flow -------------------------------------------------------------------

    def paste(self, batch: Mapping[str, Any] | bytes, *, ctx=None, source: str = PASTE):
        raw = batch if isinstance(batch, bytes) else encode(batch)
        return self.service.submit(ctx or self.owner, raw, source=source)

    def review(self, proposal_id: str, ctx=None) -> dict[str, Any]:
        return self.service.review(ctx or self.reader, proposal_id)

    def lines(self, proposal_id: str, ctx=None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        after = None
        while True:
            page = self.service.lines(ctx or self.reader, proposal_id, after=after, limit=100)
            out.extend(page["lines"])
            after = page["next_cursor"]
            if after is None:
                return out

    def approve(self, proposal_id: str, *, ctx=None):
        review = self.review(proposal_id)
        return self.service.approve(ctx or self.owner, proposal_id,
                                    review["approval_request_id"], review["review_digest"])

    def reject(self, proposal_id: str, *, ctx=None):
        review = self.review(proposal_id)
        return self.service.reject(ctx or self.owner, proposal_id, review["rejection_request_id"])

    def draft(self, proposal_id: str, overrides: Sequence[Mapping[str, Any]] = (),
              choices: Mapping[str, Any] | None = None, *, revision: int | None = None):
        review = self.review(proposal_id)
        current = review["draft_revision"] if revision is None else revision
        return self.service.save_draft(self.owner, proposal_id, current, list(overrides),
                                       dict(choices or {}))

    def tag_all(self, proposal_id: str, tag: str = "business") -> dict[str, Any]:
        keys = [line["line_key"] for line in self.lines(proposal_id) if line["tag"] is None]
        return self.draft(proposal_id, [{"line_key": key, "tag": tag} for key in keys])

    # ---- observers ------------------------------------------------------------------------

    def version(self) -> int:
        return self.env.version(WORKSPACE)

    def tables(self) -> dict[str, Any]:
        """Every statement table, copied out of a transaction (full rows, detail included)."""
        with self.scratch_tx() as tx:
            return tables_for(tx).dump()

    def saved_drafts(self) -> dict[str, Any]:
        """The drafts the owner actually saved (every batch also has a revision-0 registry row)."""
        return {pid: row for pid, row in self.tables()["drafts"].items()
                if row.draft_revision > 0}

    def rows(self, table: str) -> list[dict[str, Any]]:
        found = self.env.rows(table)
        if table == "os_receipts":  # PG returns lists where the fake keeps tuples
            for row in found:
                row["reason_codes"] = tuple(row["reason_codes"])
        return found

    def everything(self) -> str:
        """A text dump of every ledger table and every statement table (for privacy greps)."""
        parts = [repr(self.rows(name)) for name in (
            "os_proposals", "os_receipts", "os_events", "os_outbox", "os_expenses",
            "os_mirror_state", "os_summary_monthly")]
        parts.append(json.dumps(_plain(self.tables()), ensure_ascii=False, default=str,
                                sort_keys=True))
        return "\n".join(parts)


def _plain(value: Any) -> Any:
    """Dataclass rows as dicts with EVERY field (their repr hides the private ones)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return value


def line(merchant: str, amount: int = 10000, *, tag: str | None = "business",
         day: str = "2026-09-20", **over: Any) -> dict[str, Any]:
    return raw_line(merchant_display=merchant, amount_minor=amount, suggested_tag=tag,
                    txn_date=day, **over)


def batch(lines: Iterable[Mapping[str, Any]], *, number: int = 1,
          label: str = "Synthetic Business Card", charge_date: str = "2026-10-15",
          ) -> dict[str, Any]:
    """A batch whose lines are never dated after its charge date (the parser refuses that)."""
    limit = date.fromisoformat(charge_date)
    clamped = []
    for item in lines:
        item = dict(item)
        if date.fromisoformat(item["txn_date"]) > limit:
            item["txn_date"] = (limit - timedelta(days=5)).isoformat()
        clamped.append(item)
    return raw_batch(clamped, client_batch_id=uuid_v4(0x9000 + number), account_label=label,
                     charge_date=charge_date)


def three_lines() -> list[dict[str, Any]]:
    """A business charge, a personal charge and a business credit."""
    return [
        line("DEMO SHOP", 10000, suggested_category="כלי AI", is_subscription=False),
        line("PERSONAL CAFE", 421731, tag="personal", day="2026-09-21"),
        line("BIZ REFUND", 1500, kind="credit", day="2026-09-22"),
    ]


def date_of(text: str) -> date:
    return date.fromisoformat(text)
