"""A deterministic statement world for the V2-26 tests: a FakeLedger, a service, three contexts.

Everything here is synthetic. ``World`` wires the real ``LedgerStatementService`` to the
``FakeLedger`` with in-memory statement tables (``tx.scratch``), a pinned pepper and a frozen
clock. The knobs the ledger reads (kill switch, freeze, backend, flag) are the ``Settings`` object
the service reads, so one assignment moves both.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from app.core.config import MiaEnv, Settings
from app.services.os_ledger import api
from app.services.os_ledger.statement_tables import CredentialRow
from app.services.os_ledger.statements import LedgerStatementService, pepper_fingerprint

from tests.fakes.contexts import for_worker, make_ctx
from tests.fakes.os_ledger import FakeLedger
from tests.fakes.os_ledger_builders import commit_direct, expense
from tests.fakes.os_ledger_env import NOW, OWNER_ID, WORKSPACE, Knobs, LedgerEnv
from tests.unit.statements.helpers import PEPPER, raw_batch, raw_line

OWNER = int(OWNER_ID)
CRED = "AC_" + "a" * 32
CRED_B = "AC_" + "b" * 32
OWNER_SCOPES = frozenset({"assaf-os:read", "assaf-os:propose", "assaf-os:approve"})
ASSISTANT_SCOPES = frozenset({"merchant_rules:read", "own_proposals:read", "statement:propose"})
PASTE = "ASSISTANT_PASTE"
CLAUDE = "ASSISTANT_CLAUDE"
GPT = "ASSISTANT_GPT"
HEX64 = re.compile(r"[0-9a-f]{64}")

# ---- the §9 matrix (V2-19) --------------------------------------------------------------------
# V2-19's ``authorize.require`` is not on this base. The service reaches it through ONE seam
# (``LedgerStatementService(require=...)``, default: the real module, resolved at call time). Until
# V2-19 merges, these tests inject this table, which copies the §9 cells the statement service
# uses; once ``authorize`` is importable they use the real function, unchanged.
_DENY = "channel_operation_denied"
_COLUMN = {"OS": 0, "MIA": 1, "TELEGRAM_APPROVAL": 2, "ASSISTANT_CLAUDE": 3, "ASSISTANT_GPT": 3,
           "WORKER": 4}
_MATRIX: dict[str, tuple[tuple[Any, ...], ...]] = {
    "read": (("scope", "assaf-os:read", False), ("allow",), ("deny",), ("deny",),
             ("job", {"verifier", "mirror"})),
    "read_rules": (("scope", "assaf-os:read", False), ("deny",), ("deny",),
                   ("scope", "merchant_rules:read", False), ("deny",)),
    "read_own_batches": (("scope", "assaf-os:read", False), ("deny",), ("deny",),
                         ("scope", "own_proposals:read", False), ("deny",)),
    "propose_statement_batch": (("scope", "assaf-os:propose", True), ("deny",), ("deny",),
                                ("scope", "statement:propose", False), ("deny",)),
    "decide_statement_batch": (("scope", "assaf-os:approve", True), ("deny",), ("deny",),
                               ("deny",), ("deny",)),
    "draft_statement_batch": (("scope", "assaf-os:approve", True), ("deny",), ("deny",),
                              ("deny",), ("deny",)),
    "expire_batches": (("deny",), ("deny",), ("deny",), ("deny",), ("job", {"retention"})),
}


def matrix_require(ctx: api.TrustedOwnerContext, op: str) -> None:
    cell = _MATRIX[op][_COLUMN[ctx.channel]]
    if cell[0] == "deny":
        raise api.LedgerError(_DENY)
    if cell[0] == "scope":
        if cell[1] not in ctx.scopes:
            raise api.LedgerError("scope_denied")
        if cell[2] and ctx.browser_mutation_verified is not True:
            raise api.LedgerError("browser_mutation_verification_required")
    elif cell[0] == "job" and not ctx.scopes & cell[1]:
        raise api.LedgerError("scope_denied")


def default_require():
    try:
        from app.services.os_ledger.authorize import require
    except ImportError:
        return matrix_require
    return require


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


class World:
    def __init__(self, *, pin: bool = True, statements: str = "active", **settings: Any) -> None:
        self.config = make_settings(**settings)
        config = self.config
        self.new_id = Counter()
        self.ledger = FakeLedger(
            clock=lambda: NOW, uuid4=self.new_id,
            settings_epoch=lambda: config.assaf_os_authority_epoch,
            backend=lambda: config.assaf_os_backend, kill_switch=lambda: config.kill_switch,
            freeze=lambda: config.assaf_os_freeze,
            undo_window_s=lambda: config.owner_undo_window_seconds,
            enabled=lambda: config.assaf_os_ledger_enabled)
        self.ledger.ensure_workspace(WORKSPACE, OWNER_ID, 1)
        for domain, state in (("core", "active"), ("statements", statements)):
            if state != "shadow":
                self.ledger.transition(WORKSPACE, domain, state, expected_epoch=1,
                                       actor_owner_id=OWNER_ID)
        if pin:
            self.ledger.pin_statement_pepper(WORKSPACE, pepper_fingerprint(PEPPER))
        self.watermark: dict[str, Any] = {"value": None}
        self.service = LedgerStatementService(
            self.ledger, config, pin_reader=lambda tx: tx.data["statement_pepper_fp"],
            revoke_watermark=lambda owner_id: self.watermark["value"], require=default_require())
        self.env = LedgerEnv("fake", self.ledger, Knobs(), rows=self.ledger.rows,
                             version=self.ledger.version, now=self.ledger._now,
                             advance=self.ledger.advance)
        self.owner = make_ctx("OS", owner=OWNER, workspace=WORKSPACE, scopes=OWNER_SCOPES,
                              browser_mutation_verified=True)
        self.reader = make_ctx("OS", owner=OWNER, workspace=WORKSPACE, scopes=OWNER_SCOPES,
                               browser_mutation_verified=False)
        self.worker = for_worker("retention", owner=OWNER, workspace=WORKSPACE)

    # ---- contexts -------------------------------------------------------------------------

    def assistant(self, kind: str = "claude", credential: str = CRED):
        channel = {"claude": "ASSISTANT_CLAUDE", "gpt": "ASSISTANT_GPT"}[kind]
        return make_ctx(channel, owner=OWNER, workspace=WORKSPACE, scopes=ASSISTANT_SCOPES,
                        browser_mutation_verified=False, credential_id=credential)

    def ctx(self, channel: str, **over: Any):
        fields: dict[str, Any] = dict(owner=OWNER, workspace=WORKSPACE, scopes=OWNER_SCOPES,
                                      browser_mutation_verified=True)
        fields.update(over)
        return make_ctx(channel, **fields)

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
        with self.scratch_tx() as tx:
            tx.scratch.setdefault("statement_tables", {}).setdefault(
                "credentials", {})[credential_id] = row

    def revoke(self, credential_id: str = CRED) -> None:
        with self.scratch_tx() as tx:
            table = tx.scratch["statement_tables"]["credentials"]
            table[credential_id] = dataclasses.replace(table[credential_id], revoked_at=tx.now)

    def drop_batch_lines(self) -> None:
        """A restore: FORK-BACKUP excludes the batch-line data."""
        with self.scratch_tx() as tx:
            tx.scratch["statement_tables"]["batch_lines"].clear()

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
        return self.ledger.version(WORKSPACE)

    def tables(self) -> dict[str, Any]:
        """The statement tables, copied out of a transaction (full rows, detail included)."""
        import copy
        with self.scratch_tx() as tx:
            return copy.deepcopy(tx.scratch.get("statement_tables", {}))

    def rows(self, table: str) -> list[dict[str, Any]]:
        return self.ledger.rows(table)

    def everything(self) -> str:
        """A text dump of every ledger table and every statement table (for privacy greps)."""
        parts = [repr(self.ledger.rows(name)) for name in (
            "os_proposals", "os_receipts", "os_events", "os_outbox", "os_expenses",
            "os_mirror_state", "os_summary_monthly")]
        parts.append(json.dumps(_plain(self.tables()), ensure_ascii=False, default=str,
                                sort_keys=True))
        return "\n".join(parts)

    def saved_drafts(self) -> dict[str, Any]:
        """The drafts the owner actually saved (every batch also has a revision-0 registry row)."""
        return {pid: row for pid, row in self.tables().get("drafts", {}).items()
                if row.draft_revision > 0}


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
