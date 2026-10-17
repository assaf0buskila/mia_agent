"""Builders for ledger tests: records, mutations, fused-kind drafts and workspaces (V2-16f).

They talk to a ``LedgerEnv`` (or any object with the ``Ledger`` surface) through ``LedgerTx`` only,
so the same tests run on the fake and on PG. Later chunks (V2-24, V2-26, V2-30) reuse them.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID, uuid4, uuid5

import pytest
from app.prototypes.assaf_os_domain import ENUMS
from app.services.os_ledger import api

from tests.fakes.contexts import make_ctx
from tests.fakes.os_ledger_env import OWNER_ID, WORKSPACE

PATH_LEARN, PATH_AUTOMATION = ENUMS["path"]
LEAD_NEW, LEAD_RELEVANT, LEAD_IRRELEVANT, LEAD_NO_ANSWER, LEAD_CONVERTED = ENUMS["lead_status"]
CLIENT_ACTIVE, CLIENT_INACTIVE = ENUMS["client_status"]
TASK_OPEN, TASK_DOING, TASK_WAITING, TASK_DONE, TASK_CANCELLED = ENUMS["task_status"]
WORK_PLANNED, WORK_ACTIVE, WORK_DONE, WORK_CANCELLED = ENUMS["work_status"]
TIME_ACTIVE, TIME_CANCELLED = ENUMS["time_status"]
PAYMENT_RECEIVED, PAYMENT_REFUND, PAYMENT_CANCELLED = ENUMS["payment_kind"]
EXPENSE_PAID, EXPENSE_UNPAID, EXPENSE_CANCELLED = ENUMS["payment_status"]
EXPENSE_ACTUAL, EXPENSE_USAGE = ENUMS["expense_source"]


def rid() -> str:
    return str(uuid4())


def rejection_of(request_id: str) -> str:
    return str(uuid5(api.REQUEST_NS, request_id + ":reject"))


def ctx(channel="OS", **fields):
    return make_ctx(channel, **({"workspace": WORKSPACE} | fields))


def wire(entity: str, record_id: str, revision: int = 1, **inputs) -> dict:
    return {api.ID_KEYS[entity]: record_id, "revision": revision, **inputs}


def create(entity: str, record_id: str, **inputs) -> api.Mutation:
    return api.Mutation(entity, record_id, "upsert", None, wire(entity, record_id, 1, **inputs))


def entity_of(record: dict) -> str:
    """The entity of a wire record. ``work_id`` and ``person_id`` are inputs of other entities."""
    order = ("leads", "payments", "time", "expenses", "tasks", "works", "clients")
    return next(e for e in order if api.ID_KEYS[e] in record)


def update(before: dict, **changes) -> api.Mutation:
    entity = entity_of(before)
    record_id = before[api.ID_KEYS[entity]]
    after = {**before, **changes, "revision": before["revision"] + 1}
    return api.Mutation(entity, record_id, "upsert", before, after)


def tombstone(entity: str, before: dict) -> api.Mutation:
    after = {**before, "revision": before["revision"] + 1}
    return api.Mutation(entity, before[api.ID_KEYS[entity]], "tombstone", before, after)


def restore(entity: str, before: dict) -> api.Mutation:
    after = {**before, "revision": before["revision"] + 1}
    return api.Mutation(entity, before[api.ID_KEYS[entity]], "restore", before, after)


# Valid inputs per entity (the references line up when used together: PER_c, WOR_w).
def client(record_id="PER_c1", **over) -> api.Mutation:
    return create("clients", record_id, **({"name": "Client", "status": CLIENT_ACTIVE} | over))


def work(record_id="WOR_w1", person_id="PER_c1", **over) -> api.Mutation:
    return create("works", record_id, **({
        "person_id": person_id, "title": "Work", "path": PATH_LEARN, "status": WORK_ACTIVE,
        "currency": "ILS"} | over))


def payment(record_id="PAY_p1", person_id="PER_c1", work_id="WOR_w1", **over) -> api.Mutation:
    return create("payments", record_id, **({
        "person_id": person_id, "work_id": work_id, "path": PATH_LEARN, "date": "2026-10-01",
        "actual_amount_ils": 1000, "currency": "ILS", "kind": PAYMENT_RECEIVED} | over))


def time_entry(record_id="TIM_t1", person_id="PER_c1", work_id="WOR_w1", **over) -> api.Mutation:
    return create("time", record_id, **({
        "person_id": person_id, "work_id": work_id, "path": PATH_LEARN, "date": "2026-10-01",
        "minutes": 30, "status": TIME_ACTIVE} | over))


def task(record_id="TAS_t1", **over) -> api.Mutation:
    return create("tasks", record_id, **({"title": "Task", "status": TASK_OPEN} | over))


def expense(record_id="EXP_e1", source_key="key-1", **over) -> api.Mutation:
    return create("expenses", record_id, **({
        "date": "2026-10-01", "category": "Ads", "platform": "Meta", "path": PATH_LEARN,
        "amount_ils": 500, "currency": "ILS", "payment_status": EXPENSE_PAID,
        "source_type": EXPENSE_ACTUAL, "source_key": source_key} | over))


def lead(record_id="LEA_l1", person_id="PER_l1", **over) -> api.Mutation:
    m = create("leads", record_id, **({
        "name": "Lead", "phone": "0501234567", "path": PATH_LEARN, "status": LEAD_NEW} | over))
    return api.Mutation(m.entity, m.record_id, m.op, None, {**m.after, "person_id": person_id})


def window_s(env) -> int:
    return env.knobs.undo_window_s


def before_images(mutations: Sequence[api.Mutation]) -> list[dict]:
    return [{"entity": m.entity, "record_id": m.record_id,
             "revision": (m.before or {"revision": 0})["revision"]} for m in mutations]


def direct_draft(tx, mutations: Sequence[api.Mutation], request_id: str, window: int,
                 request_digest: str | None = None) -> api.ProposalDraft:
    """An ``owner_direct`` draft with the §5.3 fused-kind columns."""
    first = mutations[0]
    return api.ProposalDraft(
        kind="owner_direct", source="OS_DIRECT", request_id=request_id,
        request_digest=request_digest or api.digest({"direct": request_id}),
        entity=first.entity, record_id=first.record_id,
        expected_revision=(first.before or {"revision": 0})["revision"],
        mutations=tuple(mutations), dependency_digest=api.digest(before_images(mutations)),
        expires_at=tx.now + timedelta(seconds=window))


def abandon_draft(tx, request_id: str, window: int) -> api.ProposalDraft:
    return api.ProposalDraft(
        kind="owner_direct", source="OS_DIRECT", request_id=request_id,
        request_digest=api.digest({"abandon": request_id}), entity="abandon",
        record_id=request_id, expected_revision=0, mutations=(),
        dependency_digest=api.digest([]), expires_at=tx.now + timedelta(seconds=window))


def undo_draft(tx, original: api.ReceiptRow, undo_request_id: str, mutations, window: int,
               entity: str, record_id: str) -> api.ProposalDraft:
    return api.ProposalDraft(
        kind="owner_undo", source="OS_DIRECT", request_id=undo_request_id,
        request_digest=api.digest({"undo": undo_request_id, "of": original.request_id}),
        entity=entity, record_id=record_id,
        expected_revision=original.records[0]["revision"], mutations=tuple(mutations),
        dependency_digest=api.digest(list(original.records)),
        expires_at=tx.now + timedelta(seconds=window))


def import_draft(tx, batch_id: str, manifest_digest: str, epoch: int, window: int,
                 ) -> api.ProposalDraft:
    return api.ProposalDraft(
        kind="import", source="IMPORT", request_id=api.import_request_uuid(epoch, manifest_digest),
        request_digest=api.digest({"import": manifest_digest, "epoch": epoch}), entity="import",
        record_id=batch_id, expected_revision=0, mutations=(), dependency_digest=manifest_digest,
        expires_at=tx.now + timedelta(seconds=window))


def commit_direct(env, context, mutations: Sequence[api.Mutation], request_id: str | None = None,
                  domain: str = "core"):
    """Run a service-shaped owner-direct commit at the ``LedgerTx`` level."""
    request_id = request_id or rid()
    with env.transaction(context, domain=domain, kind="owner_direct") as tx:
        proposal = tx.insert_proposal(direct_draft(tx, mutations, request_id, window_s(env)))
        tx.apply(mutations, source="OS_DIRECT", request_id=request_id)
        receipt = tx.finish(proposal, status="committed", receipt_request_id=request_id,
                            applied=mutations)
    return proposal, receipt


def commit_undo(env, context, original: api.ReceiptRow, mutations: Sequence[api.Mutation],
                undo_request_id: str | None = None):
    """Run a service-shaped undo (kind ``owner_undo``, ``undo_of`` = the original receipt key)."""
    undo_request_id = undo_request_id or rid()
    first = mutations[0]
    with env.transaction(context, domain="core", kind="owner_undo") as tx:
        proposal = tx.insert_proposal(undo_draft(tx, original, undo_request_id, mutations,
                                                 window_s(env), first.entity, first.record_id))
        tx.apply(mutations, source="OS_DIRECT", request_id=undo_request_id)
        receipt = tx.finish(proposal, status="committed", receipt_request_id=undo_request_id,
                            applied=mutations, undo_of=original.request_id)
    return proposal, receipt


def commit_import(env, context, mutations: Sequence[api.Mutation], manifest_digest: str,
                  batch_id: str | None = None, epoch: int = 1):
    """Run a service-shaped import promote (manifest revisions) at the ``LedgerTx`` level."""
    batch_id = batch_id or rid()
    request_id = api.import_request_uuid(epoch, manifest_digest)
    with env.transaction(context, domain="core", kind="import") as tx:
        proposal = tx.insert_proposal(import_draft(tx, batch_id, manifest_digest, epoch,
                                                   window_s(env)))
        tx.apply(mutations, source="IMPORT", request_id=request_id)
        receipt = tx.finish(proposal, status="committed", receipt_request_id=request_id,
                            applied=mutations)
    return proposal, receipt


def current(env, entity: str, record_id: str, context=None, *,
            include_deleted=False) -> dict | None:
    with env.transaction(context or ctx(), domain="core", kind="owner_direct") as tx:
        return tx.get(entity, record_id, include_deleted=include_deleted)


def seed_chain(env, context=None) -> None:
    """client, work, payment, time entry and an open task, each at revision 1."""
    commit_direct(env, context or ctx(),
                  [client(), work(), payment(), time_entry(), task()])


def propose_task(env, context=None, *, title="Synthetic", request_id=None):
    return env.propose(context or ctx(), api.ProposeInput(
        request_id or rid(), "tasks", None, 0, {"title": title, "status": TASK_OPEN}))


def workspace_row(env, name: str = WORKSPACE) -> dict:
    return next(r for r in env.rows("os_workspaces") if r["workspace_id"] == name)


def counts(env) -> dict:
    """Row counts of every table a ledger write touches, plus the version."""
    names = ("os_proposals", "os_receipts", "os_events", "os_outbox", "os_tasks")
    return {name: len(env.rows(name)) for name in names} | {
        "version": env.version(), "pending_seq": workspace_row(env)["pending_seq"]}


def fresh_workspace(env, name: str, *, core="shadow", statements="shadow",
                    owner: str = OWNER_ID) -> None:
    """Create ``name`` at epoch 1 and walk each domain to the wanted state by legal edges."""
    path = {"shadow": (), "active": ("active",), "fenced": ("fenced",),
            "restoring": ("fenced", "restoring")}
    env.ensure_workspace(name, owner, 1)
    for domain, wanted in (("core", core), ("statements", statements)):
        for step in path[wanted]:
            env.transition(name, domain, step, expected_epoch=1, actor_owner_id=owner)


def uuid_of(value: int) -> str:
    return str(UUID(int=value))


@contextmanager
def invariant_violation(env, name: str):
    """The fake raises ``fake_invariant:<name>``; PG maps SQLSTATE class 23 to a ledger error."""
    if env.kind == "fake":
        with pytest.raises(AssertionError, match=f"^fake_invariant:{name}$"):
            yield
    else:
        with pytest.raises(api.LedgerError) as caught:
            yield
        assert caught.value.code == "ledger_integrity_failed"
