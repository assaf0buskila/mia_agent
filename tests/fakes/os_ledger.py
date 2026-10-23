"""In-memory ledger for tests: ``api.Ledger`` + ``api.AuthorityAdmin`` (V2-16f, contract §5/§6).

No database, credentials or external effects. It mirrors the V2-14/14b SQL: every unique key and
CHECK is enforced, and a violation by a caller bug raises
``AssertionError("fake_invariant:<name>")``.
A constraint a client input can reach is pre-checked and mapped to its business code first, so a
conformance case never sees an invariant. This is not a PostgreSQL emulator: the decisions that fill
the contract's silences (D1-D6, see the V2-16f report) are noted where they are implemented.
"""

from __future__ import annotations

import base64
import copy
import re
import secrets
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass, replace
from datetime import date, timedelta
from uuid import uuid4 as default_uuid4
from uuid import uuid5

from app.prototypes.assaf_os_domain import (
    ENUMS,
    canonical_input,
    validate_state,
    validate_transition,
)
from app.prototypes.assaf_os_ledger import DATE_KEYS, PREFIXES, _dependencies, _envelope
from app.prototypes.assaf_os_primary import LocalPrimaryError
from app.prototypes.assaf_os_summaries import SummaryInputError, build_summaries
from app.services.os_ledger import api
from app.services.os_ledger.ids import refuse_live_dependents, same_record

_POINTS = {"after_apply", "after_receipt", "before_commit", "commit_unknown"}
_STATES = ("shadow", "active", "fenced", "restoring")
_DOMAINS = ("core", "statements")
_KINDS = ("proposal", "owner_direct", "owner_undo", "statement_batch", "import")
_TERMINAL = ("committed", "rejected", "expired", "target_changed", "superseded", "abandoned")
# D2: the non-bumping edges; the two bumping edges are handled in ``transition``.
_PLAIN_EDGES = {("shadow", "active"), ("shadow", "fenced"), ("active", "fenced"),
                ("fenced", "restoring")}
_BUMP_EDGES = {("fenced", "shadow"), ("restoring", "active")}
_TABLES = (
    *("os_" + e for e in api.ENTITIES), "os_proposals", "os_receipts", "os_events", "os_outbox",
    "os_mirror_state", "os_summary_monthly", "os_summary_cycle", "os_import_batches",
    "os_import_rows",
)
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CLAIM_FIELDS = ("person_id", "work_id", "original_payment_id", "currency", "source_type",
                 "source_key", "minutes")
_WORKSPACE_COLUMNS = ("owner_id", "epoch", "version", "pending_seq", "created_at", "updated_at",
                      "statement_pepper_fp", "summaries")


def _check(condition, name):
    if not condition:
        raise AssertionError("fake_invariant:" + name)


def _domain_error(exc, code="target_changed"):
    if exc.code == "lead_already_exists":
        return api.LedgerError("lead_already_exists")
    return api.LedgerError(code, details={"reason": exc.code})


def _wire(row, entity):
    result = copy.deepcopy(row["inputs"])
    result.update({api.ID_KEYS[entity]: row["record_id"], "revision": row["revision"]})
    if entity == "leads":
        result["person_id"] = row["person_id"]
    return result


def _as_row(row):
    return asdict(row) if is_dataclass(row) else copy.deepcopy(row)


def _valid_uuid(value):
    return isinstance(value, str) and _UUID_RE.fullmatch(value) is not None


class FakeLedger:
    def __init__(self, *, clock, uuid4=lambda: str(default_uuid4()), settings_epoch,
                 backend=lambda: "postgres", kill_switch=lambda: False, freeze=lambda: False,
                 undo_window_s=lambda: 30, enabled=lambda: True, validator=None, summaries=None):
        self.clock = clock
        self.uuid4 = uuid4
        self.settings_epoch = settings_epoch
        self.backend = backend
        self.kill_switch = kill_switch
        self.freeze = freeze
        self.undo_window_s = undo_window_s
        self.enabled = enabled
        self.validator = validator
        self.summaries = summaries
        self._offset = timedelta()
        self._workspaces = {}
        self._locks = {}
        self._registry_lock = threading.Lock()
        self._local = threading.local()
        self._faults = {}
        self._pauses = {}
        self._cursors = {}

    # ---- test hooks (not part of api.Ledger) ----------------------------------------------

    def _now(self):
        return self.clock() + self._offset

    def advance(self, seconds):
        self._offset += timedelta(seconds=seconds)

    def inject(self, point, exc):
        """One-shot failure at ``point``; ``commit_unknown`` commits and then raises ``unknown``."""
        if point not in _POINTS:
            raise ValueError("invalid_injection_point")
        self._faults[point] = exc

    def pause(self, point):
        """Block the next transaction that reaches ``point`` until the returned event is set.

        ``event.reached`` is set when a transaction is blocked there. The blocked transaction
        holds the workspace lock, so a second transaction waits behind it.
        """
        if point not in _POINTS:
            raise ValueError("invalid_injection_point")
        event = threading.Event()
        event.reached = threading.Event()
        self._pauses[point] = event
        return event

    def _hit(self, point):
        event = self._pauses.pop(point, None)
        if event is not None:
            event.reached.set()
            if not event.wait(30):
                raise TimeoutError("fake_pause_timeout")
        exc = self._faults.pop(point, None)
        if exc is not None:
            if point == "commit_unknown":
                raise api.LedgerError("unknown") from None
            raise exc

    def rows(self, table):
        """Copies of the rows of ``table`` over every workspace, as plain dicts."""
        result = []
        for workspace in list(self._workspaces):
            with self._locked(workspace):
                data = self._workspace(workspace)
                if table == "os_workspaces":
                    result.append({"workspace_id": workspace} | {
                        key: copy.deepcopy(data[key]) for key in _WORKSPACE_COLUMNS})
                elif table == "os_authority":
                    result.extend({
                        "workspace_id": workspace, "domain": domain, "state": state,
                        "epoch": data["authority_epochs"][domain],
                        "activated_at": data["activated_at"][domain],
                        "activated_by": data["activated_by"][domain],
                        "created_at": data["created_at"], "updated_at": data["updated_at"],
                    } for domain, state in data["states"].items())
                else:
                    result.extend(_as_row(r) for r in data["tables"][table].values())
        return result

    def version(self, ws):
        with self._locked(ws):
            return self._workspace(ws)["version"]

    # ---- locking and workspaces --------------------------------------------------------------

    def _lock(self, workspace):
        with self._registry_lock:
            return self._locks.setdefault(workspace, threading.Lock())

    @contextmanager
    def _locked(self, workspace):
        # One non-reentrant lock per workspace: on PG a nested transaction() on the same thread
        # would wait forever on the first one's FOR UPDATE, so the fake fails loudly instead.
        held = getattr(self._local, "held", set())
        _check(workspace not in held, "nested_transaction")
        with self._lock(workspace):
            self._local.held = held | {workspace}
            try:
                yield
            finally:
                self._local.held = held

    def _workspace(self, workspace):
        if workspace not in self._workspaces:
            raise api.LedgerError("ledger_unavailable", reason="workspace_missing")
        return self._workspaces[workspace]

    @staticmethod
    def _snapshot(workspace, data):
        return api.AuthoritySnapshot(workspace, data["epoch"], data["version"],
                                     copy.deepcopy(data["states"]),
                                     copy.deepcopy(data["activated_at"]))

    # ---- AuthorityAdmin -----------------------------------------------------------------------

    def ensure_workspace(self, workspace_id, owner_id, epoch):
        with self._locked(workspace_id):
            _check(bool(re.fullmatch(r"[0-9]{1,20}", owner_id)), "workspace_owner")
            if workspace_id in self._workspaces:
                data = self._workspace(workspace_id)
                if (data["owner_id"], data["epoch"]) != (owner_id, epoch):
                    # D1: an existing row with another (owner, epoch) is refused.
                    raise api.LedgerError("authority_changed", reason="workspace_conflict")
                return self._snapshot(workspace_id, data)
            _check(epoch == 1, "workspace_initial_epoch")
            now = self._now()
            data = dict(
                owner_id=owner_id, epoch=1, version=0, pending_seq=0,
                states={"core": "shadow", "statements": "shadow"},
                authority_epochs={"core": 1, "statements": 1},
                activated_at={"core": None, "statements": None},
                activated_by={"core": None, "statements": None},
                created_at=now, updated_at=now, statement_pepper_fp=None, scratch={},
                summaries={}, monthly_wire=[], last_bump={},
                tables={name: {} for name in _TABLES})
            self._workspaces[workspace_id] = data
            return self._snapshot(workspace_id, data)

    def state(self, workspace_id):
        with self._locked(workspace_id):
            return self._snapshot(workspace_id, self._workspace(workspace_id))

    def _bump(self, data):
        data["epoch"] += 1
        data["authority_epochs"] = dict.fromkeys(_DOMAINS, data["epoch"])
        data["updated_at"] = self._now()

    def transition(self, workspace_id, domain, to, *, expected_epoch, actor_owner_id):
        with self._locked(workspace_id):
            data = self._workspace(workspace_id)
            _check(domain in _DOMAINS, "authority_domain")
            _check(to in _STATES, "authority_state")
            if data["owner_id"] != actor_owner_id:
                raise api.LedgerError("authority_changed", reason="epoch_mismatch")
            current = data["states"][domain]
            # D2: a repeat of a bumping edge, seen after it already ran, is a no-op.
            if data["epoch"] == expected_epoch + 1 and data["last_bump"].get(domain) == (
                to, data["epoch"]
            ) and current == to:
                return self._snapshot(workspace_id, data)
            if data["epoch"] != expected_epoch:
                raise api.LedgerError("authority_changed", reason="epoch_mismatch")
            if current == to:
                return self._snapshot(workspace_id, data)
            edge = (current, to)
            if edge not in _PLAIN_EDGES | _BUMP_EDGES:
                raise api.LedgerError("authority_changed", reason="transition_refused")
            if edge == ("fenced", "shadow") and any(
                r.kind != "import" and r.epoch == data["epoch"]
                for r in data["tables"]["os_receipts"].values()
            ):
                raise api.LedgerError("authority_changed", reason="transition_refused")
            if edge in _BUMP_EDGES:
                self._bump(data)
                data["last_bump"][domain] = (to, data["epoch"])
            data["states"][domain] = to
            if to == "active":
                data["activated_at"][domain] = self._now()
                data["activated_by"][domain] = actor_owner_id
            data["updated_at"] = self._now()
            return self._snapshot(workspace_id, data)

    def bump_epoch(self, workspace_id, *, expected_epoch):
        with self._locked(workspace_id):
            data = self._workspace(workspace_id)
            if data["epoch"] == expected_epoch + 1:  # D3: idempotent
                return self._snapshot(workspace_id, data)
            if data["epoch"] != expected_epoch:
                raise api.LedgerError("authority_changed", reason="epoch_mismatch")
            self._bump(data)
            return self._snapshot(workspace_id, data)

    def pin_statement_pepper(self, workspace_id, fingerprint):
        with self._locked(workspace_id):
            data = self._workspace(workspace_id)
            _check(bool(re.fullmatch(r"[0-9a-f]{64}", fingerprint)), "statement_pepper_fp")
            if data["statement_pepper_fp"] not in {None, fingerprint}:
                raise api.LedgerError("ledger_unavailable", reason="statement_pepper_mismatch")
            data["statement_pepper_fp"] = fingerprint
            return self._snapshot(workspace_id, data)

    # ---- transactions -------------------------------------------------------------------------

    def _binding(self, ctx, data, domain):
        if (type(ctx.numeric_owner) is not int or str(ctx.numeric_owner) != data["owner_id"]
                or not (ctx.authority_epoch == self.settings_epoch() == data["epoch"]
                        == data["authority_epochs"][domain])):
            raise api.LedgerError("authority_changed")

    @contextmanager
    def transaction(self, ctx, *, domain, kind, purpose="write"):
        _check(domain in _DOMAINS, "transaction_domain")
        _check(kind in _KINDS, "transaction_kind")
        _check(purpose in {"write", "expire"}, "transaction_purpose")
        if purpose == "expire":
            if kind != "statement_batch" or ctx.channel != "WORKER":
                raise AssertionError("ledger_invariant:expire_scope")
        else:
            # Admission: (1 guard n/a) 2 enabled + kill switch, 3 backend, 5 workspace row,
            # 6 epoch layers, 7 state per kind. The freeze is not checked here (a decide opens
            # a transaction too); the callers that create new intent check it first.
            if not self.enabled():
                raise api.LedgerError("ledger_unavailable", reason="ledger_disabled")
            if self.kill_switch():
                raise api.LedgerError("kill_switch")
            if kind != "import" and self.backend() != "postgres":
                raise api.LedgerError("backend_not_postgres")
        with self._locked(ctx.workspace):
            data = self._workspace(ctx.workspace)
            self._binding(ctx, data, domain)
            if purpose != "expire":
                required = "shadow" if kind == "import" else "active"
                if data["states"]["core"] != required or (
                    kind == "statement_batch" and data["states"]["statements"] != "active"
                ):
                    raise api.LedgerError("authority_not_active")
            original = copy.deepcopy(data)
            tx = _Tx(self, ctx, data, kind, purpose)
            try:
                yield tx
                self._hit("before_commit")
            except BaseException as exc:
                self._workspaces[ctx.workspace] = original
                if isinstance(exc, (api.LedgerError, AssertionError, KeyboardInterrupt,
                                    SystemExit)):
                    raise
                raise api.LedgerError("ledger_unavailable") from None
            self._hit("commit_unknown")

    @contextmanager
    def _read(self, ctx):
        with self._locked(ctx.workspace):
            data = self._workspace(ctx.workspace)
            self._binding(ctx, data, "core")
            allowed = {"active", "fenced"} | ({"shadow"} if ctx.channel == "WORKER" else set())
            if data["states"]["core"] not in allowed:
                raise api.LedgerError("authority_not_active")
            yield _Tx(self, ctx, data, "proposal", "read")

    # ---- LedgerReader -------------------------------------------------------------------------

    def head(self, ctx):
        with self._read(ctx) as tx:
            mirrors = tx.data["tables"]["os_mirror_state"].values()
            verified = next(iter(mirrors), {}).get("verified_version", 0)
            return api.Head(tx.version, tx.data["epoch"], copy.deepcopy(tx.data["states"]),
                            tx.data["pending_seq"], verified)

    def _result(self, tx, receipt, *, any_epoch=False):
        if receipt is None:
            return api.ReceiptResult(False, None, None, (), tx.data["version"], None)
        if receipt.epoch != tx.ctx.authority_epoch and not any_epoch:
            raise api.LedgerError("authority_changed")  # I12: never read across epochs
        proposal = tx.proposal(receipt.proposal_id)
        records = []
        if receipt.status in {"committed", "target_changed"}:
            for mutation in proposal.mutations:
                record = tx.get(mutation.entity, mutation.record_id, include_deleted=True)
                if record is not None:
                    records.append((mutation.entity, record))
        undo = api.UndoView("none", None, None)
        if receipt.kind == "owner_direct" and receipt.status == "committed":
            prior = tx.undo_receipt_of(receipt.request_id)
            state = "undone" if prior else (
                "expired" if tx.now > receipt.undo_until else "available")
            undo = api.UndoView(state, receipt.undo_until, prior.request_id if prior else None)
        return api.ReceiptResult(True, copy.deepcopy(receipt), proposal, tuple(records),
                                 tx.data["version"], undo)

    def receipt(self, ctx, request_id):
        api.canonical_uuid(request_id)
        with self._read(ctx) as tx:
            return self._result(tx, tx.receipt(request_id))

    def creation_request(self, ctx, request_id):
        api.canonical_uuid(request_id)
        with self._read(ctx) as tx:
            proposal = tx.proposal_by_request(request_id)
            if proposal and proposal.epoch != ctx.authority_epoch:
                raise api.LedgerError("authority_changed")
            return api.CreationResult(
                bool(proposal), proposal,
                tx.receipt_for_proposal(proposal.proposal_id) if proposal else None,
                proposal.expires_at <= tx.now if proposal else None, tx.version)

    def _slice(self, tx, entity, rows, after, limit, mode):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise api.LedgerError("invalid_query")
        offset = 0
        if after is not None:
            binding = self._cursors.get(after) if isinstance(after, str) else None
            if binding is None or binding[:3] != (tx.ctx.workspace, entity, mode):
                raise api.LedgerError("invalid_cursor")
            if binding[3] != tx.ctx.authority_epoch or binding[4] != tx.version:
                raise api.LedgerError("cursor_invalidated",
                                      details={"observed_version": tx.version})
            offset = binding[5]
        selected = rows[offset:offset + limit]
        cursor = None
        if offset + limit < len(rows):
            # Opaque capability token. The registry rejects altered and cross-workspace tokens;
            # it never reads ``self.uuid4`` so a test's deterministic id source stays untouched.
            token = base64.urlsafe_b64encode(secrets.token_bytes(12)).decode().rstrip("=")
            cursor = "fake:" + token
            self._cursors[cursor] = (tx.ctx.workspace, entity, mode, tx.ctx.authority_epoch,
                                     tx.version, offset + limit)
        return selected, cursor

    def _page(self, ctx, entity, after, limit, deleted):
        if entity not in api.ENTITIES:
            raise api.LedgerError("invalid_query")
        with self._read(ctx) as tx:
            rows = list(tx._table("os_" + entity).values())
            total = sum(r["deleted_at"] is None for r in rows)
            rows = [r for r in rows if (r["deleted_at"] is not None) == deleted]
            if deleted:
                rows.sort(key=lambda r: (-r["deleted_at"].timestamp(), r["record_id"]))
            elif entity == "tasks":
                statuses = {s: 0 if s in ENUMS["task_status"][:3] else (
                    1 if s == ENUMS["task_status"][3] else 2) for s in ENUMS["task_status"]}
                rows.sort(key=lambda r: (statuses[r["inputs"]["status"]],
                                         r["due_date"] is None,
                                         r["due_date"] or date.min, r["record_id"]))
            else:
                rows.sort(key=lambda r: (
                    r["business_date"] is None,
                    -r["business_date"].toordinal() if r["business_date"] else 0,
                    r["record_id"]))
            selected, cursor = self._slice(tx, entity, rows, after, limit,
                                           "deleted" if deleted else "page")
            return api.Page(entity, tuple(_wire(r, entity) for r in selected), cursor,
                            total, tx.version)

    def page(self, ctx, entity, *, after, limit):
        return self._page(ctx, entity, after, limit, False)

    def deleted(self, ctx, entity, *, after, limit):
        return self._page(ctx, entity, after, limit, True)

    def pending(self, ctx, *, after, limit):
        with self._read(ctx) as tx:
            rows = [p for p in tx._table("os_proposals").values()
                    if p.kind == "proposal" and p.status == "pending" and p.expires_at > tx.now
                    and p.epoch == ctx.authority_epoch]
            # §8 does not freeze the pending order; the fake keeps insertion order.
            selected, cursor = self._slice(tx, "proposals", rows, after, limit, "pending")
            return api.ProposalPage(tuple(copy.deepcopy(selected)), cursor, tx.version)

    def labels(self, ctx, refs):
        """D4: the first non-empty of name, title, category, cut to 120; none -> omitted."""
        with self._read(ctx) as tx:
            result = {}
            for ref in refs:
                record = tx.get(ref.entity, ref.record_id)
                if record is None:
                    continue
                label = next((str(record[k]) for k in ("name", "title", "category")
                              if record.get(k)), None)
                if label is not None:
                    result.setdefault(ref.entity + "." + api.ID_KEYS[ref.entity], {})[
                        ref.record_id] = label[:120]
            return result

    def monthly_summary(self, ctx):
        with self._read(ctx) as tx:
            return tuple(copy.deepcopy(tx.data["monthly_wire"]))

    def proposal(self, ctx, proposal_id):
        """§22 E14(1): one stored proposal, read-only, any status or kind; None when absent or of
        another epoch (the I12 filter of ``pending``)."""
        with self._read(ctx) as tx:
            row = tx.proposal(proposal_id) if isinstance(proposal_id, str) else None
            return row if row is not None and row.epoch == ctx.authority_epoch else None

    # ---- LedgerCore ---------------------------------------------------------------------------

    def propose(self, ctx, req):
        if self.freeze():
            raise api.LedgerError("os_frozen")
        api.canonical_uuid(req.request_id)
        if ctx.channel not in {"OS", "MIA"}:
            raise api.LedgerError("channel_operation_denied")
        if type(req.expected_revision) is not int or req.expected_revision < 0:
            raise api.LedgerError("validation_error", details={"reason": "invalid_revision"})
        promote = req.entity == "promote_lead"
        try:
            if req.record_id is not None:
                canonical_input("tasks", {"person_id": req.record_id}, creating=False)
            if promote:
                if req.input or req.record_id is None or req.expected_revision < 1:
                    raise LocalPrimaryError("invalid_promotion_input")
                values = {}
            else:
                if not req.input:
                    raise LocalPrimaryError("non_input_field")
                values = canonical_input(req.entity, dict(req.input),
                                         creating=req.record_id is None)
        except LocalPrimaryError as exc:
            raise _domain_error(exc, "validation_error") from None
        request_digest = api.digest({
            "owner": str(ctx.numeric_owner), "workspace": ctx.workspace,
            "epoch": ctx.authority_epoch, "source": ctx.channel, "entity": req.entity,
            "record_id": req.record_id, "revision": req.expected_revision, "input": values,
            "outer_expiry": req.approval_expires_at.isoformat() if req.approval_expires_at
            else None})
        if req.approval_expires_at is not None and req.approval_expires_at.utcoffset() is None:
            raise api.LedgerError("validation_error",
                                  details={"reason": "invalid_approval_expiry"})
        with self.transaction(ctx, domain="core", kind="proposal") as tx:
            existing = tx.proposal_by_request(req.request_id)
            if existing:
                if existing.request_digest != request_digest or existing.kind != "proposal":
                    raise api.LedgerError("idempotency_mismatch")
                return existing
            target_entity = "leads" if promote else req.entity
            before = tx.get(target_entity, req.record_id) if req.record_id else None
            if (req.record_id is None and req.expected_revision != 0) or (
                req.record_id is not None and (before is None or
                                              before["revision"] != req.expected_revision)
            ):
                raise api.LedgerError("target_changed")
            record_id = req.record_id or PREFIXES[req.entity] + "_" + self.uuid4().replace("-", "")
            records = tx._records()
            try:
                mutations = (self._promotion(records, before, record_id, req.expected_revision)
                             if promote else self._single(tx, req.entity, before, record_id,
                                                          req.expected_revision, values, records))
                prospective = copy.deepcopy(records)
                for m in mutations:
                    key = api.ID_KEYS[m.entity]
                    prospective[m.entity] = [r for r in prospective[m.entity]
                                             if r[key] != m.record_id] + [dict(m.after)]
                    validate_transition(m.entity, m.before, dict(m.after), records)
                validate_state(prospective)
                build_summaries(prospective)
            except (LocalPrimaryError, SummaryInputError) as exc:
                raise _domain_error(exc, "validation_error") from None
            if req.entity == "leads" and any(
                r["person_id"] == mutations[0].after["person_id"] and r["record_id"] != record_id
                for r in tx._table("os_leads").values()
            ):
                raise api.LedgerError("lead_already_exists")
            expiry = tx.now + timedelta(minutes=10)
            if req.approval_expires_at is not None:
                expiry = min(expiry, req.approval_expires_at)
            if expiry <= tx.now:
                raise api.LedgerError("validation_error",
                                      details={"reason": "invalid_approval_expiry"})
            dependencies = _dependencies(records, [asdict(m) for m in mutations])
            return tx.insert_proposal(api.ProposalDraft(
                "proposal", ctx.channel, req.request_id, request_digest, req.entity, record_id,
                req.expected_revision, mutations, api.digest(dependencies), expiry))

    def _single(self, tx, entity, before, record_id, expected, values, records):
        after = (before or {}) | values | {api.ID_KEYS[entity]: record_id,
                                           "revision": expected + 1}
        if entity == "leads":
            after["person_id"] = (before["person_id"] if before else
                                  "PER_" + self.uuid4().replace("-", ""))
        return (api.Mutation(entity, record_id, "upsert", before, after),)

    def _promotion(self, records, before, record_id, expected):
        client_status = ENUMS["client_status"][0]
        lead_after = before | {"status": ENUMS["lead_status"][-1], "revision": expected + 1}
        mutations = [api.Mutation("leads", record_id, "upsert", before, lead_after)]
        person = before["person_id"]
        existing = next((r for r in records["clients"] if r["person_id"] == person), None)
        if existing is None:
            client = canonical_input("clients", {
                "name": before["name"], "phone": before.get("phone") or None,
                "email": before.get("email") or None, "status": client_status, "notes": None})
            mutations.append(api.Mutation("clients", person, "upsert", None,
                                          client | {"person_id": person, "revision": 1}))
        else:
            mutations.append(api.Mutation("clients", person, "upsert", existing,
                                          existing | {"revision": existing["revision"] + 1}))
        return tuple(mutations)

    def decide(self, ctx, proposal_id, decision_id, decision):
        api.canonical_uuid(decision_id)
        if ctx.channel not in {"OS", "TELEGRAM_APPROVAL"}:
            raise api.LedgerError("owner_approval_required")
        if decision not in {"approve", "reject"}:
            raise api.LedgerError("validation_error", details={"reason": "invalid_decision"})
        with self.transaction(ctx, domain="core", kind="proposal") as tx:
            proposal = tx.proposal(proposal_id)
            if proposal is None or proposal.kind != "proposal":
                raise api.LedgerError("proposal_not_found")
            expected = proposal.approval_id if decision == "approve" else proposal.rejection_id
            if decision_id != expected:
                raise api.LedgerError("decision_request_mismatch")
            previous = tx.receipt_for_proposal(proposal_id)
            if previous is not None:
                # The same decision replays the stored receipt; another is a second decision. A
                # replay is a read of an existing receipt, so it never crosses an epoch.
                if previous.request_id != decision_id:
                    raise api.LedgerError("already_decided")
                return self._result(tx, previous)
            status = "committed" if decision == "approve" else "rejected"
            reasons = ()
            if proposal.epoch != ctx.authority_epoch:
                status, decision_id, reasons = "expired", proposal.rejection_id, ("epoch_changed",)
            elif tx.now >= proposal.expires_at:
                status = "expired"
            elif decision == "approve":
                current_digest = api.digest(_dependencies(
                    tx._records(), [asdict(m) for m in proposal.mutations]))
                if current_digest != proposal.dependency_digest:
                    status = "target_changed"
                else:
                    try:
                        tx.apply(proposal.mutations, source=proposal.source,
                                 request_id=decision_id)
                    except api.LedgerError as exc:
                        # §22 E26: an apply-time has_dependents ends the proposal
                        # target_changed (reason has_dependents); every other refusal keeps
                        # its behaviour and ledger_integrity_failed is re-raised.
                        if exc.code == "has_dependents":
                            reasons = ("has_dependents",)
                        elif exc.code not in {"target_changed", "lead_already_exists"}:
                            raise
                        status = "target_changed"
            receipt = tx.finish(proposal, status=status, receipt_request_id=decision_id,
                applied=proposal.mutations if status == "committed" else (), reason_codes=reasons)
            return self._result(tx, receipt, any_epoch=True)


class _Tx:
    connection = None

    def __init__(self, ledger, ctx, data, kind, purpose):
        self.ledger, self.ctx, self.data = ledger, ctx, data
        self.kind, self.purpose = kind, purpose
        self.now, self.version = ledger._now(), data["version"]

    @property
    def scratch(self):
        return self.data["scratch"]

    def _scope(self):
        if self.purpose == "expire":
            raise AssertionError("ledger_invariant:expire_scope")

    def _table(self, name):
        return self.data["tables"][name]

    def _records(self):
        return {entity: [_wire(row, entity) for _, row in sorted(
            self._table("os_" + entity).items()) if row["deleted_at"] is None]
            for entity in api.ENTITIES}

    # ---- reads ----------------------------------------------------------------------------

    def get(self, entity, record_id, *, include_deleted=False):
        self._scope()
        row = self._table("os_" + entity).get(record_id)
        if row is None or (row["deleted_at"] is not None and not include_deleted):
            return None
        return _wire(row, entity)

    def get_many(self, refs, *, include_deleted=False):
        self._scope()
        return {ref: row for ref in refs if (row := self.get(
            ref.entity, ref.record_id, include_deleted=include_deleted)) is not None}

    def find_expenses(self, *, ids=(), source_key_prefix=None, manual_window=None):
        """Non-deleted expenses matching ANY given filter (a UNION; no filter returns []).

        §5.1 gives only the signature; the semantics are pinned by conformance (erratum E7 (c)).
        """
        self._scope()
        found = []
        for row in self._table("os_expenses").values():
            if row["deleted_at"] is not None:
                continue
            if row["record_id"] in ids or (
                source_key_prefix is not None and row["source_key"].startswith(source_key_prefix)
            ) or (manual_window is not None and row["business_date"] is not None
                  and manual_window[0] <= row["business_date"] <= manual_window[1]):
                found.append(_wire(row, "expenses"))
        return found

    def dependents(self, entity, record_id):
        self._scope()
        result = []
        for referrer, field, target in api.DEPENDENTS:
            if target != entity:
                continue
            for row in self._table("os_" + referrer).values():
                record = _wire(row, referrer)
                if record.get(field) == record_id and api.is_live(
                    referrer, record, deleted=row["deleted_at"] is not None
                ):
                    result.append(api.RecordRef(referrer, row["record_id"]))
        return result

    def proposal(self, proposal_id):
        return copy.deepcopy(self._table("os_proposals").get(proposal_id))

    def proposal_by_request(self, request_id):
        self._scope()
        return copy.deepcopy(next((p for p in self._table("os_proposals").values()
                                  if p.request_id == request_id), None))

    def receipt(self, request_id):
        self._scope()
        return copy.deepcopy(self._table("os_receipts").get(request_id))

    def receipt_for_proposal(self, proposal_id):
        self._scope()
        return copy.deepcopy(next((r for r in self._table("os_receipts").values()
                                  if r.proposal_id == proposal_id), None))

    def undo_receipt_of(self, request_id):
        self._scope()
        return copy.deepcopy(next((r for r in self._table("os_receipts").values()
                                  if r.undo_of == request_id), None))

    # ---- writes ---------------------------------------------------------------------------

    def insert_proposal(self, draft):
        self._scope()
        _check(draft.kind == self.kind, "proposal_kind_mismatch")
        _check(draft.expected_revision >= 0, "proposal_revision")
        _check(draft.expires_at > self.now, "proposal_expiry")
        _check(_valid_uuid(draft.request_id), "proposal_request_id")
        _check(re.fullmatch(r"[A-Z][A-Z0-9_]{1,31}", draft.source) is not None, "proposal_source")
        _check(not draft.source.startswith("ASSISTANT_") or draft.kind == "statement_batch",
               "proposal_assistant_kind")
        _check(draft.kind not in {"owner_direct", "owner_undo"} or draft.source == "OS_DIRECT",
               "proposal_direct_source")
        ttl = {"proposal": timedelta(minutes=10), "statement_batch": timedelta(days=7)}
        _check(draft.kind not in ttl or draft.expires_at <= self.now + ttl[draft.kind],
               "proposal_ttl")
        fused = draft.kind in {"owner_direct", "owner_undo", "import"}
        proposals = self._table("os_proposals")
        # The §5.3 keyspace rule, enforced here for the fused kinds: the request id must not be
        # any proposal's approval_id or rejection_id, nor any receipt key. Nothing is written.
        if fused and (draft.request_id in self._table("os_receipts") or any(
            draft.request_id in {p.approval_id, p.rejection_id} for p in proposals.values()
        )):
            raise api.LedgerError("idempotency_mismatch")
        approval = draft.request_id if fused else self.ledger.uuid4()
        rejection = (str(uuid5(api.REQUEST_NS, draft.request_id + ":reject"))
                     if fused else self.ledger.uuid4())
        proposal_id = "PRO_" + self.ledger.uuid4().replace("-", "")
        _check(not any(p.request_id == draft.request_id for p in proposals.values()),
               "proposal_request_unique")
        _check(proposal_id not in proposals, "proposal_pk")
        _check(not any(p.approval_id == approval for p in proposals.values()),
               "proposal_approval_unique")
        _check(not any(p.rejection_id == rejection for p in proposals.values()),
               "proposal_rejection_unique")
        _check(approval != rejection, "proposal_decision_distinct")
        row = api.ProposalRow(
            self.ctx.workspace, proposal_id, draft.request_id, draft.request_digest, "",
            str(self.ctx.numeric_owner), self.ctx.authority_epoch, approval, rejection,
            draft.entity, draft.record_id, draft.expected_revision, copy.deepcopy(draft.mutations),
            draft.dependency_digest, draft.source, draft.credential_id, draft.kind, "pending",
            self.now, draft.expires_at)
        envelope = _envelope(row)  # the exact prototype field set; no local digest extension
        envelope["mutations"] = [asdict(m) for m in row.mutations]
        row = replace(row, envelope_digest=api.digest(envelope))
        proposals[row.proposal_id] = row
        self.data["pending_seq"] += 1
        return copy.deepcopy(row)

    def apply(self, mutations, *, source, request_id):
        self._scope()
        original = copy.deepcopy(self.data)  # apply is atomic on its own (the SAVEPOINT rule)
        try:
            result = [self._apply_one(m, source, request_id, mutations) for m in mutations]
            self._check_references(mutations)
            if self.ledger.validator is None:
                try:
                    validate_state(self._records())
                except LocalPrimaryError as exc:
                    raise _domain_error(exc) from None
            else:
                self.ledger.validator.validate(self, mutations)
            if self.ledger.summaries is None:
                try:
                    summary = build_summaries(self._records())
                except SummaryInputError as exc:
                    raise _domain_error(exc) from None
                self.data["summaries"] = {"coverage": summary["coverage"],
                                          "calculated_version": self.version + 1}
                self.data["monthly_wire"] = summary["monthly"]
            else:
                self.ledger.summaries.on_apply(self, mutations)
            self.ledger._hit("after_apply")
            return tuple(result)
        except BaseException:
            self.data.clear()
            self.data.update(original)
            raise

    def _apply_one(self, mutation, source, request_id, batch):
        entity, key = mutation.entity, mutation.record_id
        table = self._table("os_" + entity)
        current = table.get(key)
        before = mutation.before
        revision = before["revision"] if before is not None else 0
        after_revision = mutation.after.get("revision")
        valid_revision = type(after_revision) is int and (
            after_revision > revision if self.kind == "import" else after_revision == revision + 1)
        matches = (current is None if before is None else
                   current is not None and current["revision"] == revision)
        if current is not None:
            tombstoned = current["deleted_at"] is not None
            matches = matches and tombstoned == (mutation.op == "restore")
        if mutation.op == "restore" and before is None:
            matches = False
        if not matches or not valid_revision:
            raise api.LedgerError("target_changed")
        # §22 E22: a strict compare-and-set (a LedgerError, as on PG, not a fake invariant).
        if before is not None and not same_record(before, _wire(current, entity)):
            raise api.LedgerError("ledger_integrity_failed", reason="apply_before_image")
        refuse_live_dependents(self, mutation, batch)  # §22 E20, the one shared rule
        system = {api.ID_KEYS[entity], "revision"} | ({"person_id"} if entity == "leads" else set())
        try:
            inputs = canonical_input(entity, {k: v for k, v in mutation.after.items()
                                               if k not in system})
            # Transition validation includes cross-record checks. A restore has no target in the
            # non-deleted projection, so its identity is checked by the compare-and-set above.
            validate_transition(entity, None if mutation.op == "restore" else before,
                                dict(mutation.after), self._records())
        except LocalPrimaryError as exc:
            raise _domain_error(exc) from None
        row = dict(workspace_id=self.ctx.workspace, record_id=key,
                   revision=mutation.after["revision"], inputs=inputs, source=source,
                   created_at=current["created_at"] if current else self.now,
                   updated_at=self.now, deleted_at=self.now if mutation.op == "tombstone" else None,
                   deleted_by_request_id=request_id if mutation.op == "tombstone" else None)
        business_date = inputs.get(DATE_KEYS.get(entity))
        row["business_date"] = date.fromisoformat(business_date) if business_date else None
        for field in _CLAIM_FIELDS:
            if field in mutation.after:
                row[field] = mutation.after[field]
        if entity in {"payments", "expenses"}:
            row["amount_minor"] = inputs["actual_amount_ils" if entity == "payments"
                                         else "amount_ils"]
        if entity == "works":
            row["agreed_amount_minor"] = inputs.get("agreed_amount_ils")
        if entity == "tasks":
            row.update(status=inputs.get("status"), due_date=row["business_date"])
        if entity == "expenses":
            row["source_digest"] = api.digest({
                "source_type": inputs["source_type"], "source_key": inputs["source_key"]})
            # The unique index covers tombstoned rows too, which the domain check does not see.
            if any(r["source_type"] == row["source_type"] and r["source_key"] == row["source_key"]
                   for k, r in table.items() if k != key):
                raise api.LedgerError("target_changed",
                                      details={"reason": "duplicate_expense_source"})
        if entity == "leads":
            # UNIQUE (workspace_id, person_id): a client-reachable constraint, so a business code.
            if any(r["person_id"] == row["person_id"] for k, r in table.items() if k != key):
                raise api.LedgerError("lead_already_exists")
        _check(row["updated_at"] >= row["created_at"], "record_timestamps")
        table[key] = row
        return _wire(row, entity)

    def _check_references(self, mutations):
        """FK mirror: every referenced row must exist (tombstoned rows still satisfy a FK)."""
        for mutation in mutations:
            row = self._table("os_" + mutation.entity)[mutation.record_id]
            for referrer, field, target in api.DEPENDENTS:
                if referrer == mutation.entity and row.get(field) is not None:
                    _check(row[field] in self._table("os_" + target),
                           f"fk_{referrer}_{field}")

    def finish(self, proposal, *, status, receipt_request_id, applied=(), undo_until=None,
               undo_of=None, reason_codes=()):
        if self.purpose == "expire":
            # §5.3 allows a pending statement_batch past its expires_at "or that has no batch
            # lines"; §12.2 adds one of an older epoch and names the no-lines case with the
            # reason code restored_without_detail. The batch lines are chunk-owned (V2-26's
            # scratch layout), so the fake trusts that reason code for "no lines".
            restored = tuple(reason_codes) == ("restored_without_detail",)
            if not (proposal.kind == "statement_batch" and proposal.status == "pending"
                    and status == "expired"
                    and (proposal.expires_at < self.now or restored
                         or proposal.epoch != self.ctx.authority_epoch)):
                raise AssertionError("ledger_invariant:expire_scope")
        current = self._table("os_proposals").get(proposal.proposal_id)
        _check(current is not None and current.status == "pending", "finish_pending")
        _check(status in _TERMINAL, "receipt_status")
        _check(status != "abandoned" or proposal.kind == "owner_direct", "abandoned_kind")
        # A fused kind (id = approval_id) finishes only committed or target_changed, plus
        # abandoned for owner_direct: it has no late approve or reject and no supersede.
        if proposal.kind in {"owner_direct", "owner_undo", "import"}:
            _check(status in {"committed", "target_changed", "abandoned"},
                   "receipt_fused_status")
        _check(len(reason_codes) <= 20, "receipt_reason_codes")
        _check((undo_of is not None) == (proposal.kind == "owner_undo"), "receipt_undo_kind")
        _check(undo_until is None or (proposal.kind == "owner_direct" and status == "committed"),
               "receipt_undo_until_kind")
        if proposal.kind == "owner_direct" and status == "committed":
            undo_until = self.now + timedelta(seconds=self.ledger.undo_window_s())
        _check(undo_until is None or undo_until > self.now, "receipt_undo_window")
        # §5.3 receipt-key table. An expiry from expire_due (purpose="expire") or from an epoch
        # change is keyed by the rejection_id; a late approve or reject by the id it supplied.
        allowed_keys = {
            "committed": {current.approval_id}, "target_changed": {current.approval_id},
            "abandoned": {current.approval_id}, "rejected": {current.rejection_id},
            "superseded": {current.rejection_id},
            "expired": {current.approval_id, current.rejection_id}}[status]
        if status == "expired" and (self.purpose == "expire" or "epoch_changed" in reason_codes):
            allowed_keys = {current.rejection_id}
        _check(receipt_request_id in allowed_keys, "receipt_key")
        receipts = self._table("os_receipts")
        _check(receipt_request_id not in receipts, "receipt_pk")
        _check(not any(r.proposal_id == proposal.proposal_id for r in receipts.values()),
               "receipt_proposal_unique")
        _check(undo_of is None or undo_of != receipt_request_id, "receipt_undo_self")
        _check(undo_of is None or undo_of in receipts, "receipt_undo_fk")
        _check(undo_of is None or not any(r.undo_of == undo_of for r in receipts.values()),
               "receipt_undo_unique")
        events, outbox = self._table("os_events"), self._table("os_outbox")
        version = self.data["version"] + 1
        event_id = self.ledger.uuid4()
        _check(event_id not in events, "event_pk")
        _check(not any(e["decision_id"] == receipt_request_id for e in events.values()),
               "event_decision_unique")
        _check(not any(e["commit_version"] == version for e in events.values()),
               "event_version_unique")
        _check(status != "committed"
               or not any(o["commit_version"] == version for o in outbox.values()),
               "outbox_version_unique")
        self.data["version"] = version
        self.data["pending_seq"] += 1
        refs = tuple({"entity": m.entity, "record_id": m.record_id,
                      "revision": m.after["revision"]} for m in applied)
        records = tuple(ref | {"digest": api.digest(m.after)} for ref, m in zip(refs, applied))
        receipt = api.ReceiptRow(self.ctx.workspace, receipt_request_id, proposal.proposal_id,
            proposal.epoch, status, proposal.kind, self.data["version"],
            records if status == "committed" and proposal.kind != "import" else (),
            self.now, undo_until, undo_of, tuple(reason_codes))
        self._table("os_proposals")[proposal.proposal_id] = replace(current, status=status)
        receipts[receipt_request_id] = receipt
        events[event_id] = dict(
            workspace_id=self.ctx.workspace, event_id=event_id, decision_id=receipt_request_id,
            commit_version=self.data["version"], created_at=self.now,
            body={"status": status, "kind": proposal.kind,
                  "effects": list(refs) if status == "committed" else []})
        if status == "committed":
            body = {"authority_epoch": proposal.epoch, "refs": list(refs)}
            if proposal.kind == "import":
                body.update(refs=[], full=True)
            outbox[event_id] = dict(
                workspace_id=self.ctx.workspace, event_id=event_id,
                commit_version=self.data["version"], body=body, status="pending", attempts=0,
                next_attempt_at=self.now, lease_owner=None, lease_generation=0, leased_at=None,
                last_error_code=None, created_at=self.now, updated_at=self.now)
        self.ledger._hit("after_receipt")
        return copy.deepcopy(receipt)

    # ---- erratum E1-rev: import staging --------------------------------------------------------

    def _import_guard(self):
        self._scope()
        if self.kind != "import" or self.purpose != "write":
            raise api.LedgerError("invalid_transaction_kind")

    def insert_import_batch(self, *, batch_id, authority_epoch, source_digest, source_binding,
                            provenance):
        self._import_guard()
        batches = self._table("os_import_batches")
        _check(_valid_uuid(batch_id), "import_batch_id")
        _check(batch_id not in batches, "import_batch_pk")
        _check(type(authority_epoch) is int and authority_epoch > 0, "import_batch_epoch")
        if authority_epoch != self.ctx.authority_epoch:  # I12: never stage across epochs
            raise api.LedgerError("authority_changed", reason="import_epoch_mismatch")
        _check(isinstance(source_digest, str) and 0 < len(source_digest) <= 64,
               "import_batch_source_digest")
        _check(isinstance(source_binding, str) and 0 < len(source_binding) <= 256,
               "import_batch_source_binding")
        _check(isinstance(provenance, Mapping), "import_batch_provenance")
        _check(not any(b["authority_epoch"] == authority_epoch
                       and b["source_digest"] == source_digest for b in batches.values()),
               "import_batch_source_unique")
        batches[batch_id] = dict(
            workspace_id=self.ctx.workspace, batch_id=batch_id, source_binding=source_binding,
            source_digest=source_digest, authority_epoch=authority_epoch, status="staged",
            provenance=copy.deepcopy(dict(provenance)), quarantine=None, created_at=self.now,
            completed_at=None)

    def insert_import_rows(self, batch_id, rows):
        self._import_guard()
        batch = self._table("os_import_batches").get(batch_id)
        _check(batch is not None, "import_row_batch_fk")
        if batch["status"] != "staged":  # rows are insert-only and a settled batch is immutable
            raise api.LedgerError("import_status_invalid")
        stored = self._table("os_import_rows")
        numbers = {r["row_number"] for r in stored.values() if r["batch_id"] == batch_id}
        digests = {r["source_digest"] for r in stored.values() if r["batch_id"] == batch_id}
        prepared = []
        for row in rows:
            _check(isinstance(row, api.ImportRow), "import_row_type")
            _check(type(row.row_number) is int and row.row_number > 0, "import_row_number")
            _check(row.entity in api.ENTITIES, "import_row_entity")
            _check(row.status in {"staged", "quarantined"}, "import_row_status")
            _check(row.record_id is None or (isinstance(row.record_id, str)
                                             and 0 < len(row.record_id) <= 64), "import_row_record")
            _check(isinstance(row.source_digest, str) and 0 < len(row.source_digest) <= 64,
                   "import_row_source_digest")
            _check(isinstance(row.raw_record, Mapping), "import_row_raw")
            _check(row.canonical_record is None or isinstance(row.canonical_record, Mapping),
                   "import_row_canonical")
            _check(isinstance(row.provenance, Mapping), "import_row_provenance")
            _check(row.quarantine is None or isinstance(row.quarantine, Mapping),
                   "import_row_quarantine_type")
            _check(row.status != "quarantined" or row.quarantine is not None,
                   "import_row_quarantine")
            _check(row.record_id is not None or row.status == "quarantined", "import_row_idless")
            _check(row.row_number not in numbers, "import_row_pk")
            _check(row.source_digest not in digests, "import_row_source_unique")
            numbers.add(row.row_number)
            digests.add(row.source_digest)
            prepared.append(dict(
                workspace_id=self.ctx.workspace, batch_id=batch_id, row_number=row.row_number,
                entity=row.entity, record_id=row.record_id, source_digest=row.source_digest,
                status=row.status, raw_record=copy.deepcopy(dict(row.raw_record)),
                canonical_record=copy.deepcopy(dict(row.canonical_record))
                if row.canonical_record is not None else None,
                provenance=copy.deepcopy(dict(row.provenance)),
                quarantine=copy.deepcopy(dict(row.quarantine))
                if row.quarantine is not None else None))
        for item in prepared:
            stored[(batch_id, item["row_number"])] = item
        return len(prepared)

    def set_import_status(self, batch_id, status, *, quarantine=None):
        self._import_guard()
        batch = self._table("os_import_batches").get(batch_id)
        _check(batch is not None, "import_status_batch_fk")
        _check(status in {s for edge in api.IMPORT_STATUS_EDGES for s in edge},
               "import_status_value")
        _check(quarantine is None or status == "quarantined", "import_batch_quarantine")
        if (batch["status"], status) not in api.IMPORT_STATUS_EDGES:
            raise api.LedgerError("import_status_invalid")
        flagged = any(r["batch_id"] == batch_id and r["status"] == "quarantined"
                      for r in self._table("os_import_rows").values())
        if status == "validated" and flagged:  # validated: the manifest, and no quarantined row
            raise api.LedgerError("import_status_invalid")
        if status == "quarantined" and not (flagged and isinstance(quarantine, Mapping)):
            raise api.LedgerError("import_status_invalid")
        batch["status"] = status
        if status == "quarantined":
            batch["quarantine"] = copy.deepcopy(dict(quarantine))
        if status in {"imported", "quarantined", "failed"}:
            batch["completed_at"] = self.now
        _check(batch["completed_at"] is None or batch["completed_at"] >= batch["created_at"],
               "import_batch_completed")

    def live_ids(self, entity):
        self._scope()
        if self.kind != "import" and self.purpose != "read":
            raise api.LedgerError("invalid_transaction_kind")
        if entity not in api.ENTITIES:
            raise api.LedgerError("invalid_entity")
        # Read at call time so the fake and PG cannot drift: a row is excluded only when it is
        # tombstoned or already in its FORK-DELETE end state (tasks have none: tombstone only).
        end = api.FORK_DELETE_END_STATE[entity]
        ids = frozenset(
            row["record_id"] for row in self._table("os_" + entity).values()
            if row["deleted_at"] is None and not (
                end is not None and row["inputs"].get(end[0]) == end[1]))
        if len(ids) > api.LIVE_IDS_LIMIT:
            raise api.LedgerError("local_record_limit")
        return ids

