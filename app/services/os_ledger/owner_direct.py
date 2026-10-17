"""Owner-direct commits, compensating undo, abandon and the deleted read (V2-24, contract §11.1).

One tap from the owner's OS session writes without a proposal round trip: propose and decide are
fused into ONE ledger transaction of kind ``owner_direct`` (``owner_undo`` for an undo), so either
the proposal, its receipt, one event and (when committed) one outbox row all exist, or nothing does.

* Only ``app/api/os_v2/commits.py`` calls this module (§9 AST allow-list). The context must be the
  OS channel with ``assaf-os:direct`` and a verified browser mutation (cookie + Origin + CSRF); the
  numeric owner is re-checked here on every call (I4, I6). ``OS_DIRECT`` is assigned here, never
  read from a body.
* Only the ``api.py`` protocol is used (``LedgerStore.transaction`` and ``LedgerTx``), so V2-16's
  PostgreSQL core drops in unchanged.
* A revision mismatch or a ``validate`` failure is a terminal 200 ``target_changed`` receipt with
  the fresh records and no business change. Every other refusal is a ``LedgerError`` with
  ``write_outcome:"not_started"`` and nothing written.
* Logs: ``os_commit op=<op> outcome=<status|code> ms=<int>`` only (I7). No body, record field or
  identifier is ever logged.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qsl

from app.prototypes.assaf_os_domain import ENUMS, canonical_input
from app.prototypes.assaf_os_ledger import PREFIXES
from app.prototypes.assaf_os_primary import LocalPrimaryError
from app.services.os_ledger import api

DIRECT_SCOPE = "assaf-os:direct"
READ_SCOPE = "assaf-os:read"
SOURCE: api.Source = "OS_DIRECT"
SINGLE_OPS = ("create", "update", "complete", "reopen", "delete", "restore")
BULK_OPS = ("bulk_complete", "bulk_delete_done")
TASK_ONLY_OPS = frozenset({"complete", "reopen", "restore"})
MAX_BULK_ITEMS = 100
MAX_DEPENDENTS = 20  # §5.2 has_dependents.details.dependents
MAX_CHANGED = 100  # §5.2 undo_conflict.details.changed
ID_PREFIXES: Mapping[str, str] = MappingProxyType(dict(PREFIXES))
TASK_OPEN = ENUMS["task_status"][0]  # לביצוע
TASK_DONE = ENUMS["task_status"][3]  # בוצע

_SINGLE_KEYS = frozenset({"request_id", "op", "entity", "record_id", "expected_revision", "patch"})
_BULK_KEYS = frozenset({"request_id", "op", "entity", "items"})
_ITEM_KEYS = frozenset({"record_id", "expected_revision"})
_LOG_OPS = frozenset(SINGLE_OPS + BULK_OPS)
_RECORD_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,63}")  # the domain's id rule
_MAX_REVISION = 2**53 - 1
_DELETED_KEYS = frozenset({"entity", "after", "limit"})
_LIMIT = re.compile(r"[1-9][0-9]{0,2}")
_NO_UNDO = MappingProxyType({"state": "none", "until": None, "undo_request_id": None})
_LOG = logging.getLogger("mia.os_commit")


# ---------------------------------------------------------------------------------------------
# Errors, admission and logging


def _invalid(reason: str) -> api.LedgerError:
    return api.LedgerError("validation_error", details={"reason": reason}, reason=reason)


def _integrity(reason: str) -> api.LedgerError:
    return api.LedgerError("ledger_integrity_failed", reason=reason)


def _admit(ctx: Any, settings: Any, *, scope: str, write: bool, intent: bool) -> None:
    """I4/I6/I8 for one call: the route gate, the OS context, the numeric owner, the switches."""
    if not isinstance(ctx, api.TrustedOwnerContext):
        raise api.LedgerError("invalid_trusted_context")
    if (getattr(settings, "assaf_os_owner_direct_enabled", False) is not True
            or getattr(settings, "assaf_os_backend", "sheets") != "postgres"):
        raise api.LedgerError("operation_unavailable", reason="owner_direct_off")
    if ctx.channel != "OS":
        raise api.LedgerError("channel_operation_denied")
    if scope not in ctx.scopes:
        raise api.LedgerError("scope_denied")
    if write and ctx.browser_mutation_verified is not True:
        raise api.LedgerError("browser_mutation_verification_required")
    owner = ctx.numeric_owner
    if (type(owner) is not int or owner <= 0
            or str(owner) not in settings.telegram_owner_user_id_set()):
        raise api.LedgerError("owner_auth_required", reason="owner_not_allowed")
    expires = ctx.auth_expires_at
    if (not isinstance(expires, datetime) or expires.utcoffset() is None
            or expires <= datetime.now(UTC)):
        raise api.LedgerError("owner_auth_required", reason="context_expired")
    if write and getattr(settings, "kill_switch", True) is not False:
        raise api.LedgerError("kill_switch")
    if intent and getattr(settings, "assaf_os_freeze", True) is not False:
        raise api.LedgerError("os_frozen")


def _window(settings: Any) -> timedelta:
    seconds = getattr(settings, "owner_undo_window_seconds", None)
    if type(seconds) is not int or not 10 <= seconds <= 60:
        raise api.LedgerError("ledger_unavailable", reason="undo_window_invalid")
    return timedelta(seconds=seconds)


def _log(op: str, outcome: str, started: float) -> None:
    elapsed = int((time.perf_counter() - started) * 1000)
    _LOG.info("os_commit op=%s outcome=%s ms=%d", op, outcome, elapsed)


def _logged(op: str, run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        body = run()
    except api.LedgerError as exc:
        _log(op, exc.code, started)
        raise
    except Exception:
        _log(op, "error", started)
        raise
    _log(op, body["status"], started)
    return body


# ---------------------------------------------------------------------------------------------
# Wire helpers (§10.1 binding + §10.2 receipt_v2, with §22 E12 and E14.3). V2-20 owns the shared
# serializer; until it lands, the bodies are built here with exactly its rules.


def _iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise _integrity("naive_datetime")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _binding(ctx: api.TrustedOwnerContext, observed_version: int) -> dict[str, Any]:
    return {"api_version": "assaf-os.v2", "source": "postgresql", "workspace_id": ctx.workspace,
            "authority_epoch": ctx.authority_epoch, "observed_version": observed_version}


def _receipt_body(tx: api.LedgerTx, ctx: api.TrustedOwnerContext,
                  receipt: api.ReceiptRow) -> dict[str, Any]:
    """``receipt_v2`` for an owner-direct or undo receipt, read inside the same transaction."""
    if receipt.epoch != ctx.authority_epoch:
        raise api.LedgerError("authority_changed")  # I12: never answer across epochs
    proposal = tx.proposal(receipt.proposal_id)
    if proposal is None:
        raise _integrity("receipt_without_proposal")
    records: list[dict[str, Any]] = []
    if receipt.status in {"committed", "target_changed"}:
        seen: set[tuple[str, str]] = set()
        for mutation in proposal.mutations:
            key = (mutation.entity, mutation.record_id)
            if key in seen:
                continue
            seen.add(key)
            record = tx.get(mutation.entity, mutation.record_id, include_deleted=True)
            if record is not None:
                records.append({"entity": mutation.entity, "record": dict(record)})
    if receipt.status == "committed" and not records:
        raise _integrity("receipt_records_missing")  # E14.3
    undo: dict[str, Any] | None = None
    if receipt.kind == "owner_direct":
        undo = dict(_NO_UNDO)
        if receipt.status == "committed":
            if receipt.undo_until is None:
                raise _integrity("receipt_undo_until_missing")
            prior = tx.undo_receipt_of(receipt.request_id)
            state = "undone" if prior else (
                "expired" if tx.now > receipt.undo_until else "available")
            undo = {"state": state, "until": _iso(receipt.undo_until),
                    "undo_request_id": prior.request_id if prior else None}
    committed = receipt.status == "committed"
    return _binding(ctx, max(tx.version, receipt.commit_version)) | {
        "status": receipt.status,
        "request_id": receipt.request_id,
        "proposal_id": receipt.proposal_id,
        "kind": receipt.kind,
        "actor_source": proposal.source,
        "commit_version": receipt.commit_version,
        "committed_at": _iso(receipt.completed_at) if committed else None,
        "server_now": _iso(tx.now),
        "records": records,
        "undo": undo,
        "undo_of": receipt.undo_of,
        "reason_codes": list(receipt.reason_codes),
    }


def _stored(tx: api.LedgerTx, ctx: api.TrustedOwnerContext,
            proposal: api.ProposalRow) -> dict[str, Any]:
    receipt = tx.receipt(proposal.approval_id)  # fused kinds: the receipt key is approval_id
    if receipt is None or receipt.proposal_id != proposal.proposal_id:
        raise _integrity("receipt_missing")
    return _receipt_body(tx, ctx, receipt)


def _replay(tx: api.LedgerTx, ctx: api.TrustedOwnerContext, existing: api.ProposalRow,
            kind: str, request_digest: str) -> dict[str, Any]:
    """§5.3 replay table. ``abandoned`` wins over a digest or kind mismatch (a late original)."""
    abandoned = existing.kind == "owner_direct" and existing.status == "abandoned"
    if not abandoned and (existing.kind != kind or existing.request_digest != request_digest):
        raise api.LedgerError("idempotency_mismatch")
    return _stored(tx, ctx, existing)


def _keyspace(tx: api.LedgerTx, request_id: str) -> None:
    """§5.3 keyspace rule, receipt half. ``insert_proposal`` refuses an approval_id or
    rejection_id collision itself (fake and PG), before anything is written."""
    if tx.receipt(request_id) is not None:
        raise api.LedgerError("idempotency_mismatch")


# ---------------------------------------------------------------------------------------------
# Records


def _system_keys(entity: str) -> frozenset[str]:
    return frozenset({api.ID_KEYS[entity], "revision"} | (
        {"person_id"} if entity == "leads" else set()))


def _inputs(entity: str, record: Mapping[str, Any]) -> dict[str, Any]:
    system = _system_keys(entity)
    return {key: value for key, value in record.items() if key not in system}


def _system(entity: str, record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in _system_keys(entity) if key in record}


def _bumped(entity: str, current: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
    """A full wire record: ``inputs`` over ``current``'s identity, at ``current.revision + 1``."""
    return dict(inputs) | _system(entity, current) | {"revision": current["revision"] + 1}


def _end_state(entity: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """FORK-DELETE for a non-task entity: its cancellation status (``api`` holds the one map)."""
    column, value = api.FORK_DELETE_END_STATE[entity]
    inputs = _inputs(entity, record) | {column: value}
    if entity == "payments":
        # The domain allows original_payment_id only on a refund (החזר); a cancelled refund
        # drops the link, and an undo restores it from the before-image.
        inputs.pop("original_payment_id", None)
    return inputs


def _is_delete(mutation: api.Mutation) -> bool:
    if mutation.op == "tombstone":
        return True
    end = api.FORK_DELETE_END_STATE.get(mutation.entity)
    return end is not None and mutation.after.get(end[0]) == end[1] and (
        mutation.before or {}).get(end[0]) != end[1]


def _refs(pairs: Iterable[tuple[str, str]], limit: int) -> list[dict[str, str]]:
    return [{"entity": entity, "record_id": record_id}
            for entity, record_id in sorted(set(pairs))[:limit]]


def _before_images(mutations: Sequence[api.Mutation]) -> list[dict[str, Any]]:
    return [{"entity": m.entity, "record_id": m.record_id,
             "revision": (m.before or {"revision": 0})["revision"]} for m in mutations]


# ---------------------------------------------------------------------------------------------
# POST /commits


@dataclass(frozen=True)
class _CommitRequest:
    request_id: str
    op: str
    entity: str
    record_id: str | None
    expected_revision: int
    patch: Mapping[str, Any]
    items: tuple[tuple[str, int], ...] = ()

    @property
    def bulk(self) -> bool:
        return self.op in BULK_OPS


def _record_id(value: Any) -> str:
    if type(value) is not str or _RECORD_ID.fullmatch(value) is None:
        raise _invalid("invalid_record_id")
    return value


def _revision(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_REVISION:
        raise _invalid("invalid_revision")
    return value


def _canonical(entity: str, raw: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    try:
        return canonical_input(entity, raw, creating=creating)
    except LocalPrimaryError as exc:
        raise _invalid(exc.code) from None


def _parse_commit(body: Any) -> _CommitRequest:
    if not isinstance(body, Mapping):
        raise _invalid("invalid_body")
    keys = set(body)
    if keys not in (_SINGLE_KEYS, _BULK_KEYS):
        raise _invalid("invalid_body")
    op = body["op"]
    if op not in (BULK_OPS if keys == _BULK_KEYS else SINGLE_OPS):
        raise _invalid("invalid_op")
    request_id = api.canonical_uuid(body["request_id"], version=4)
    entity = body["entity"]
    if entity not in api.ENTITIES:
        raise _invalid("invalid_entity")
    if op in BULK_OPS:
        if entity != "tasks":
            raise _invalid("invalid_op_entity")
        return _parse_bulk(request_id, op, body["items"])
    if op in TASK_ONLY_OPS and entity != "tasks":
        raise _invalid("invalid_op_entity")
    record_id, revision, patch = body["record_id"], body["expected_revision"], body["patch"]
    if op == "create":
        if record_id is not None or type(revision) is not int or revision != 0:
            raise _invalid("invalid_create")
    else:
        record_id, revision = _record_id(record_id), _revision(revision)
    if type(patch) is not dict:
        raise _invalid("invalid_patch")
    if op == "create":
        canonical = _canonical(entity, patch, creating=True)
        patch = {key: value for key, value in canonical.items() if value is not None}
    elif op == "update":
        if not patch:
            raise _invalid("empty_patch")
        patch = _canonical(entity, patch, creating=False)
    elif patch:
        raise _invalid("invalid_patch")
    return _CommitRequest(request_id, op, entity, record_id, revision, MappingProxyType(patch))


def _parse_bulk(request_id: str, op: str, items: Any) -> _CommitRequest:
    if type(items) is not list or not 1 <= len(items) <= MAX_BULK_ITEMS:
        raise _invalid("invalid_items")
    pairs: list[tuple[str, int]] = []
    seen: set[str] = set()
    for item in items:
        if type(item) is not dict or set(item) != _ITEM_KEYS:
            raise _invalid("invalid_items")
        record_id = _record_id(item["record_id"])
        revision = _revision(item["expected_revision"])
        if record_id in seen:
            raise _invalid("duplicate_item")
        seen.add(record_id)
        pairs.append((record_id, revision))
    return _CommitRequest(request_id, op, "tasks", pairs[0][0], pairs[0][1],
                          MappingProxyType({}), tuple(pairs))


def _commit_digest(ctx: api.TrustedOwnerContext, request: _CommitRequest) -> str:
    value: dict[str, Any] = {
        "owner": str(ctx.numeric_owner), "workspace": ctx.workspace,
        "epoch": ctx.authority_epoch, "source": SOURCE, "op": request.op,
        "entity": request.entity}
    if request.bulk:
        value["items"] = [{"record_id": record_id, "expected_revision": revision}
                          for record_id, revision in request.items]
    else:
        value.update(record_id=request.record_id, expected_revision=request.expected_revision,
                     patch=dict(request.patch))
    return api.digest(value)


def _probe(entity: str, record_id: str, shown: Mapping[str, Any] | None,
           op: str) -> api.Mutation:
    """A never-applied mutation naming a stale target, so the target_changed receipt (and any
    later ``GET /receipts``) shows its fresh record."""
    revision = shown["revision"] if shown is not None else 0
    after = dict(shown) if shown is not None else {api.ID_KEYS[entity]: record_id}
    before = dict(shown) if shown is not None else None
    return api.Mutation(entity, record_id, op, before, after | {"revision": revision + 1})


def _refuse_dependents(tx: api.LedgerTx, entity: str, record_id: str) -> None:
    found = tx.dependents(entity, record_id)  # api.DEPENDENTS edges whose referrer is live
    if found:
        raise api.LedgerError("has_dependents", details={"dependents": _refs(
            ((ref.entity, ref.record_id) for ref in found), MAX_DEPENDENTS)})


def _plan_single(tx: api.LedgerTx, request: _CommitRequest,
                 new_id: Callable[[], str]) -> tuple[tuple[api.Mutation, ...], bool]:
    entity, op = request.entity, request.op
    if op == "create":
        record_id = _record_id(f"{ID_PREFIXES[entity]}_{new_id()}")
        after = dict(request.patch) | {api.ID_KEYS[entity]: record_id, "revision": 1}
        if entity == "leads":
            after["person_id"] = _record_id(f"{ID_PREFIXES['clients']}_{new_id()}")
        return (api.Mutation(entity, record_id, "upsert", None, after),), False
    record_id, expected = request.record_id or "", request.expected_revision
    if op == "restore":  # tombstoned tasks only
        shown = tx.get(entity, record_id, include_deleted=True)
        live = tx.get(entity, record_id)
        if shown is None or live is not None or shown["revision"] != expected:
            return (_probe(entity, record_id, shown, "restore"),), True
        return (api.Mutation(entity, record_id, "restore", shown,
                             _bumped(entity, shown, _inputs(entity, shown))),), False
    deleting_task = op == "delete" and entity == "tasks"
    current = tx.get(entity, record_id)
    if current is None or current["revision"] != expected:
        shown = current or tx.get(entity, record_id, include_deleted=True)
        return (_probe(entity, record_id, shown,
                       "tombstone" if deleting_task else "upsert"),), True
    if op == "update":
        merged = _canonical(entity, _inputs(entity, current) | dict(request.patch),
                            creating=True)
        inputs = {key: value for key, value in merged.items() if value is not None}
    elif op in {"complete", "reopen"}:
        inputs = _inputs(entity, current) | {"status": TASK_DONE if op == "complete"
                                             else TASK_OPEN}
    else:  # delete, per FORK-DELETE
        _refuse_dependents(tx, entity, record_id)
        if deleting_task:
            return (api.Mutation(entity, record_id, "tombstone", current,
                                 _bumped(entity, current, _inputs(entity, current))),), False
        inputs = _end_state(entity, current)
    mutation = api.Mutation(entity, record_id, "upsert", current, _bumped(entity, current, inputs))
    if op == "update" and _is_delete(mutation):
        # An update that lands in the FORK-DELETE end state is a delete (review r1).
        _refuse_dependents(tx, entity, record_id)
    return (mutation,), False


def _plan_bulk(tx: api.LedgerTx,
               request: _CommitRequest) -> tuple[tuple[api.Mutation, ...], bool]:
    refs = [api.RecordRef("tasks", record_id) for record_id, _ in request.items]
    found = tx.get_many(refs)
    deleting = request.op == "bulk_delete_done"
    stale = any(
        (current := found.get(ref)) is None or current["revision"] != revision
        or (deleting and current.get("status") != TASK_DONE)
        for ref, (_, revision) in zip(refs, request.items, strict=True))
    if stale:  # all-or-nothing: the whole bulk is target_changed
        shown = tx.get_many(refs, include_deleted=True)
        return tuple(_probe("tasks", ref.record_id, shown.get(ref),
                            "tombstone" if deleting else "upsert") for ref in refs), True
    mutations = []
    for ref in refs:
        current = found[ref]
        inputs = _inputs("tasks", current)
        if not deleting:
            inputs["status"] = TASK_DONE
        mutations.append(api.Mutation("tasks", ref.record_id,
                                      "tombstone" if deleting else "upsert", current,
                                      _bumped("tasks", current, inputs)))
    return tuple(mutations), False


def _commit_tx(tx: api.LedgerTx, ctx: api.TrustedOwnerContext, request: _CommitRequest,
               request_digest: str, window: timedelta,
               new_id: Callable[[], str]) -> dict[str, Any]:
    existing = tx.proposal_by_request(request.request_id)
    if existing is not None:
        return _replay(tx, ctx, existing, "owner_direct", request_digest)
    _keyspace(tx, request.request_id)
    if request.bulk:
        mutations, stale = _plan_bulk(tx, request)
    else:
        mutations, stale = _plan_single(tx, request, new_id)
    first = mutations[0]
    proposal = tx.insert_proposal(api.ProposalDraft(
        kind="owner_direct", source=SOURCE, request_id=request.request_id,
        request_digest=request_digest, entity=request.entity, record_id=first.record_id,
        expected_revision=request.expected_revision, mutations=mutations,
        dependency_digest=api.digest(_before_images(mutations)), expires_at=tx.now + window))
    status = "target_changed"
    if not stale:
        try:
            tx.apply(mutations, source=SOURCE, request_id=request.request_id)
            status = "committed"
        except api.LedgerError as exc:  # apply is atomic on its own (§5.3 SAVEPOINT rule)
            if exc.code != "target_changed":
                raise
    receipt = tx.finish(proposal, status=status, receipt_request_id=request.request_id,
                        applied=mutations if status == "committed" else ())
    return _receipt_body(tx, ctx, receipt)


def commit(ledger: api.LedgerStore, ctx: api.TrustedOwnerContext, body: Any, *,
           settings: Any, new_id: Callable[[], str] | None = None) -> dict[str, Any]:
    """``POST /commits``: one fused owner_direct transaction; returns ``receipt_v2``."""
    op = body.get("op") if isinstance(body, Mapping) else None
    mint = new_id or (lambda: uuid.uuid4().hex)

    def run() -> dict[str, Any]:
        _admit(ctx, settings, scope=DIRECT_SCOPE, write=True, intent=True)
        request = _parse_commit(body)
        request_digest = _commit_digest(ctx, request)
        window = _window(settings)
        with ledger.transaction(ctx, domain="core", kind="owner_direct") as tx:
            return _commit_tx(tx, ctx, request, request_digest, window, mint)

    return _logged(op if isinstance(op, str) and op in _LOG_OPS else "commit", run)


# ---------------------------------------------------------------------------------------------
# POST /commits/{request_id}/undo


def _parse_undo(request_id: Any, body: Any) -> tuple[str, str]:
    original = api.canonical_uuid(request_id, version=4)
    if not isinstance(body, Mapping) or set(body) != {"undo_request_id"}:
        raise _invalid("invalid_body")
    return original, api.canonical_uuid(body["undo_request_id"], version=4)


def _compensate(original: api.Mutation, current: Mapping[str, Any]) -> api.Mutation:
    """The before-image as revision + 1 (§11.1 step 7)."""
    entity, record_id = original.entity, original.record_id
    if original.before is None:  # undo of a create is that entity's delete
        if entity == "tasks":
            return api.Mutation(entity, record_id, "tombstone", current,
                                _bumped(entity, current, _inputs(entity, current)))
        return api.Mutation(entity, record_id, "upsert", current,
                            _bumped(entity, current, _end_state(entity, current)))
    op = {"tombstone": "restore", "restore": "tombstone"}.get(original.op, "upsert")
    return api.Mutation(entity, record_id, op, current,
                        _bumped(entity, current, _inputs(entity, original.before)))


def _undo_tx(tx: api.LedgerTx, ctx: api.TrustedOwnerContext, original_id: str, undo_id: str,
             request_digest: str, window: timedelta) -> dict[str, Any]:
    # 1. replay, else the keyspace rule
    existing = tx.proposal_by_request(undo_id)
    if existing is not None:
        return _replay(tx, ctx, existing, "owner_undo", request_digest)
    _keyspace(tx, undo_id)
    # 2. the original: a committed owner_direct at this owner and epoch
    proposal = tx.proposal_by_request(original_id)
    if proposal is None:
        raise api.LedgerError("commit_not_found")
    receipt = tx.receipt(original_id)
    if (proposal.kind != "owner_direct" or proposal.status != "committed" or receipt is None
            or receipt.kind != "owner_direct" or receipt.status != "committed"
            or receipt.proposal_id != proposal.proposal_id
            or proposal.owner_id != str(ctx.numeric_owner)
            or proposal.epoch != ctx.authority_epoch or receipt.epoch != ctx.authority_epoch
            or not receipt.records or not proposal.mutations):
        raise api.LedgerError("not_undoable")
    # 3. a prior undo
    prior = tx.undo_receipt_of(original_id)
    if prior is not None:
        if prior.request_id == undo_id:
            return _receipt_body(tx, ctx, prior)
        raise api.LedgerError("already_undone", details={"undo_request_id": prior.request_id})
    # 4. the window: nothing is written after it
    if receipt.undo_until is None or tx.now > receipt.undo_until:
        raise api.LedgerError("undo_expired")
    # 5. every touched record is still at the original receipt's revision
    currents: dict[tuple[str, str], Mapping[str, Any]] = {}
    changed = []
    for item in receipt.records:
        key = (item["entity"], item["record_id"])
        current = tx.get(item["entity"], item["record_id"], include_deleted=True)
        if current is None or current["revision"] != item["revision"]:
            changed.append(key)
        else:
            currents[key] = current
    if changed:
        raise api.LedgerError("undo_conflict", details={
            "reason": "record_changed", "changed": _refs(changed, MAX_CHANGED)})
    compensations = tuple(_compensate(m, currents[(m.entity, m.record_id)])
                          for m in proposal.mutations)
    # 6. a record the undo deletes (a created record, or one moved back into its cancellation
    # status) must have no live referrer outside this undo
    # The undo of a create is that entity's delete even when the record was created already in
    # its end state (review r1), so every compensation of a create counts.
    deleting = {(c.entity, c.record_id)
                for original, c in zip(proposal.mutations, compensations, strict=True)
                if _is_delete(c) or original.before is None}
    blocking = [(ref.entity, ref.record_id)
                for entity, record_id in sorted(deleting)
                for ref in tx.dependents(entity, record_id)
                if (ref.entity, ref.record_id) not in deleting]
    if blocking:
        raise api.LedgerError("undo_conflict", details={
            "reason": "dependent_exists", "changed": _refs(blocking, MAX_CHANGED)})
    # 7. apply the before-images at revision + 1, all or nothing
    draft = api.ProposalDraft(
        kind="owner_undo", source=SOURCE, request_id=undo_id, request_digest=request_digest,
        entity=proposal.entity, record_id=proposal.record_id,
        expected_revision=receipt.records[0]["revision"], mutations=compensations,
        dependency_digest=api.digest([dict(item) for item in receipt.records]),
        expires_at=tx.now + window)
    undo_proposal = tx.insert_proposal(draft)
    try:
        tx.apply(compensations, source=SOURCE, request_id=undo_id)
    except api.LedgerError as exc:
        if exc.code != "target_changed":
            raise
        raise api.LedgerError("undo_conflict", details={
            "reason": "record_changed",
            "changed": _refs(((m.entity, m.record_id) for m in compensations), MAX_CHANGED)},
            reason="undo_validate_failed") from None
    # 8. finish, naming the original
    undone = tx.finish(undo_proposal, status="committed", receipt_request_id=undo_id,
                       applied=compensations, undo_of=original_id)
    return _receipt_body(tx, ctx, undone)


def undo(ledger: api.LedgerStore, ctx: api.TrustedOwnerContext, request_id: Any, body: Any, *,
         settings: Any) -> dict[str, Any]:
    """``POST /commits/{request_id}/undo``: a compensating ``owner_undo`` commit."""

    def run() -> dict[str, Any]:
        _admit(ctx, settings, scope=DIRECT_SCOPE, write=True, intent=True)
        original_id, undo_id = _parse_undo(request_id, body)
        request_digest = api.digest({
            "owner": str(ctx.numeric_owner), "workspace": ctx.workspace,
            "epoch": ctx.authority_epoch, "op": "undo", "of": original_id})
        window = _window(settings)
        with ledger.transaction(ctx, domain="core", kind="owner_undo") as tx:
            return _undo_tx(tx, ctx, original_id, undo_id, request_digest, window)

    return _logged("undo", run)


# ---------------------------------------------------------------------------------------------
# POST /commits/{request_id}/abandon


def _abandon_tx(tx: api.LedgerTx, ctx: api.TrustedOwnerContext, request_id: str,
                window: timedelta) -> dict[str, Any]:
    existing = tx.proposal_by_request(request_id)
    if existing is not None:
        if existing.kind not in {"owner_direct", "owner_undo"}:
            raise api.LedgerError("idempotency_mismatch")
        return _stored(tx, ctx, existing)
    _keyspace(tx, request_id)
    proposal = tx.insert_proposal(api.ProposalDraft(
        kind="owner_direct", source=SOURCE, request_id=request_id,
        request_digest=api.digest({"abandon": request_id}), entity="abandon",
        record_id=request_id, expected_revision=0, mutations=(),
        dependency_digest=api.digest([]), expires_at=tx.now + window))
    receipt = tx.finish(proposal, status="abandoned", receipt_request_id=request_id)
    return _receipt_body(tx, ctx, receipt)


def abandon(ledger: api.LedgerStore, ctx: api.TrustedOwnerContext, request_id: Any, body: Any,
            *, settings: Any) -> dict[str, Any]:
    """``POST /commits/{request_id}/abandon``: settle a lost request id; the freeze allows it."""

    def run() -> dict[str, Any]:
        _admit(ctx, settings, scope=DIRECT_SCOPE, write=True, intent=False)
        canonical = api.canonical_uuid(request_id, version=4)
        if not isinstance(body, Mapping) or body:
            raise _invalid("invalid_body")
        window = _window(settings)
        with ledger.transaction(ctx, domain="core", kind="owner_direct") as tx:
            return _abandon_tx(tx, ctx, canonical, window)

    return _logged("abandon", run)


# ---------------------------------------------------------------------------------------------
# GET /deleted (§10.2)


def _deleted_query(query: Any) -> tuple[str, str | None, int]:
    if not isinstance(query, str):
        raise api.LedgerError("invalid_query")
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query),
                          encoding="utf-8", errors="strict", max_num_fields=3)
    except (ValueError, UnicodeError):
        raise api.LedgerError("invalid_query") from None
    values: dict[str, str] = {}
    for key, value in pairs:
        if key not in _DELETED_KEYS or key in values:
            raise api.LedgerError("invalid_query")
        values[key] = value
    entity = values.get("entity", "tasks")
    after = values.get("after")
    limit = values.get("limit", "50")
    if (entity not in api.ENTITIES or (after is not None and not 1 <= len(after) <= 512)
            or _LIMIT.fullmatch(limit) is None or not 1 <= int(limit) <= 200):
        raise api.LedgerError("invalid_query")
    return entity, after, int(limit)


def deleted(ledger: api.LedgerReader, ctx: api.TrustedOwnerContext, query: Any, *,
            settings: Any) -> dict[str, Any]:
    """``GET /deleted?entity&after&limit``: the binding + entity, records, next_cursor."""
    _admit(ctx, settings, scope=READ_SCOPE, write=False, intent=False)
    entity, after, limit = _deleted_query(query)
    page = ledger.deleted(ctx, entity, after=after, limit=limit)
    return _binding(ctx, page.observed_version) | {
        "entity": page.entity,
        "records": [dict(record) for record in page.records],
        "next_cursor": page.next_cursor,
    }


__all__ = [
    "BULK_OPS",
    "DIRECT_SCOPE",
    "ID_PREFIXES",
    "MAX_BULK_ITEMS",
    "SINGLE_OPS",
    "abandon",
    "commit",
    "deleted",
    "undo",
]
