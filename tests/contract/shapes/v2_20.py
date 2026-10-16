"""V2-20 golden shapes: every section 10.2 body, built by the real PostgresOsBackend (section 10.4).

Each shape builds its own deterministic world (a FakeLedger with a frozen clock, counter-made ids
and counter-made cursors) and returns the body the backend produced. ``scripts/
export_os_contract_fixtures.py`` writes them to ``tests/contract/golden/v2-20/<shape>.json``. The
frontend vendors those files by hash, and CI fails when the export differs from the committed copy.
A body that cannot be built by the backend does not belong here: nothing is hand-written.
"""

from __future__ import annotations

import contextlib
import itertools
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from unittest import mock
from uuid import UUID

from app.core.config import MiaEnv, Settings
from app.services.assaf_os import DecisionRequest, ProposeRequest
from app.services.os_backend_pg import PageQuery, PostgresOsBackend
from app.services.os_ledger import api
from tests.fakes.contexts import make_ctx
from tests.fakes.os_ledger import FakeLedger
from tests.fakes.os_ledger_builders import (
    abandon_draft,
    client,
    commit_direct,
    commit_undo,
    expense,
    lead,
    payment,
    task,
    time_entry,
    tombstone,
    update,
    work,
)
from tests.fakes.os_ledger_env import NOW, OWNER_ID, WORKSPACE, Knobs, LedgerEnv

OWNER_SCOPES = frozenset(
    {"assaf-os:read", "assaf-os:propose", "assaf-os:approve", "assaf-os:direct"}
)


def uuid_v4(number: int) -> str:
    """A deterministic, well-formed UUIDv4 (the frontend pattern needs the version nibble)."""
    return str(UUID(int=number, version=4))


class Counter:
    """Hands out ``uuid_v4(base + 1)``, ``uuid_v4(base + 2)``, ..."""

    def __init__(self, base: int) -> None:
        self._numbers = itertools.count(base + 1)

    def __call__(self) -> str:
        return uuid_v4(next(self._numbers))


@dataclass(frozen=True)
class StaticContexts:
    """The ``contexts`` seam of PostgresOsBackend, with fixed test contexts."""

    read: api.TrustedOwnerContext
    mutate: api.TrustedOwnerContext

    def for_read(self) -> api.TrustedOwnerContext:
        return self.read

    def for_propose(self) -> api.TrustedOwnerContext:
        return self.mutate

    def for_decide(self) -> api.TrustedOwnerContext:
        return self.mutate


@dataclass
class World:
    ledger: FakeLedger
    knobs: Knobs
    env: LedgerEnv
    backend: PostgresOsBackend
    settings: Settings
    owner: api.TrustedOwnerContext
    ids: Counter
    requests: Counter


def settings_for(**changes) -> Settings:
    values = dict(
        _env_file=None,
        env=MiaEnv.TEST,
        telegram_owner_user_ids=OWNER_ID,
        assaf_os_backend="postgres",
        assaf_os_ledger_enabled=True,
        assaf_os_authority_epoch=1,
        assaf_os_workspace_id=WORKSPACE + "-ws",
    )
    values.update(changes)
    return Settings(**values)


def build_world(*, actor: str = "OS", mirror=None, **knob_overrides) -> World:
    """A prepared workspace (epoch 1, core active) behind a PostgresOsBackend, all deterministic."""
    knobs = Knobs(**knob_overrides)
    ids = Counter(0x1000)
    ledger = FakeLedger(
        clock=lambda: NOW,
        uuid4=ids,
        settings_epoch=lambda: knobs.settings_epoch,
        backend=lambda: knobs.backend,
        kill_switch=lambda: knobs.kill_switch,
        freeze=lambda: knobs.freeze,
        undo_window_s=lambda: knobs.undo_window_s,
        enabled=lambda: knobs.enabled,
    )
    env = LedgerEnv(
        "fake", ledger, knobs, rows=ledger.rows, version=ledger.version,
        now=ledger._now, advance=ledger.advance,
    )
    ledger.ensure_workspace(WORKSPACE, OWNER_ID, 1)
    ledger.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    channel = actor
    owner = make_ctx(
        channel, owner=int(OWNER_ID), workspace=WORKSPACE, scopes=OWNER_SCOPES,
        browser_mutation_verified=True,
    )
    contexts = StaticContexts(read=owner, mutate=owner)
    settings = settings_for(assaf_os_authority_epoch=knobs.settings_epoch)
    backend = PostgresOsBackend(
        settings, OWNER_ID, actor=actor, ledger=ledger, contexts=contexts,
        clock=ledger._now, mirror=mirror,
    )
    return World(ledger, knobs, env, backend, settings, owner, ids, Counter(0xA000))


@contextlib.contextmanager
def deterministic_cursors() -> Iterator[None]:
    """The fake mints cursors from ``secrets``; goldens need them stable."""
    counter = itertools.count(1)
    with mock.patch(
        "tests.fakes.os_ledger.secrets.token_bytes",
        side_effect=lambda n: next(counter).to_bytes(n, "big"),
    ):
        yield


def verified_mirror(head: api.Head) -> dict:
    """What V2-29 hands the backend once the mirror verified the head."""
    return {
        "state": "verified",
        "last_verified_at": "2026-10-16T11:59:30.000Z",
        "verified_version": head.observed_version,
        "lag_s": 0,
    }


# ---- the business data every shape reads ---------------------------------------------------------


def seed(world: World) -> None:
    """One client with a work, a payment, a time entry, an expense, three tasks and a lead."""
    commit_direct(
        world.env, world.owner,
        [
            client(name="דנה כהן", email="dana@example.com"),
            work(title="סדנת אוטומציה", agreed_amount_ils=1_200_000),
            payment(actual_amount_ils=600_000),
            time_entry(minutes=90, description="ייעוץ ראשוני"),
            expense(vendor="Meta Platforms", category="פרסום", amount_ils=45_000),
            task("TAS_t1", title="להכין הצעת מחיר", person_id="PER_c1", work_id="WOR_w1",
                 due_date="2026-10-20"),
            task("TAS_t2", title="לחזור ללקוח", status="ממתין", person_id="PER_c1"),
            task("TAS_t3", title="לשלוח חשבונית", status="בוצע", due_date="2026-10-01"),
            lead("LEA_l1", person_id="PER_l1", name="יוסי לוי", source_note="דרך האתר"),
        ],
        request_id=uuid_v4(0x5EED),
    )


def propose(world: World, entity: str, input_: dict, *, record_id=None, revision=0) -> dict:
    return world.backend.propose(ProposeRequest(
        request_id=world.requests(), entity=entity, record_id=record_id,
        expected_revision=revision, input=input_))


def decide(world: World, operation: str, proposal: dict) -> dict:
    key = "approval_request_id" if operation == "approve" else "rejection_request_id"
    return world.backend.decide(
        operation, proposal["proposal_id"], DecisionRequest(request_id=proposal[key]))


def query(entity="tasks", *, after=None, limit=50, since=None) -> PageQuery:
    return PageQuery(entity=entity, after=after, limit=limit, since=since)


def current(world: World, entity: str, record_id: str) -> dict:
    with world.ledger.transaction(world.owner, domain="core", kind="owner_direct") as tx:
        return tx.get(entity, record_id)


def _in_world(build: Callable[[World], dict], **world_options) -> Callable[[], dict]:
    def shape() -> dict:
        with deterministic_cursors():
            world = build_world(**world_options)
            seed(world)
            return build(world)

    return shape


# ---- shapes --------------------------------------------------------------------------------------


def schema(world: World) -> dict:
    return world.backend.schema()


def snapshot_tasks_page(world: World) -> dict:
    propose(world, "tasks", {"title": "לבדוק חשבונית", "status": "לביצוע"})
    return world.backend.snapshot(query("tasks", limit=2))


def snapshot_tasks_second_page(world: World) -> dict:
    first = world.backend.snapshot(query("tasks", limit=2))
    return world.backend.snapshot(query("tasks", after=first["next_cursor"], limit=2))


def snapshot_expenses_page(world: World) -> dict:
    return world.backend.snapshot(query("expenses"))


def snapshot_unchanged(world: World) -> dict:
    first = world.backend.snapshot(query("tasks"))
    return world.backend.snapshot(query("tasks", since=first["since"]))


def pending_list(world: World) -> dict:
    propose(world, "tasks", {"title": "לבדוק חשבונית", "status": "לביצוע"})
    world.backend.propose(ProposeRequest(
        request_id=world.requests(), entity="promote_lead", record_id="LEA_l1",
        expected_revision=1, input={}))
    return world.backend.pending()


def proposal_create(world: World) -> dict:
    return propose(world, "tasks", {"title": "לבדוק חשבונית", "status": "לביצוע"})


def proposal_update(world: World) -> dict:
    return propose(world, "tasks", {"title": "להכין הצעת מחיר מעודכנת"},
                   record_id="TAS_t1", revision=1)


def proposal_promote_lead(world: World) -> dict:
    return world.backend.propose(ProposeRequest(
        request_id=world.requests(), entity="promote_lead", record_id="LEA_l1",
        expected_revision=1, input={}))


def receipt_committed(world: World) -> dict:
    proposal = proposal_create(world)
    return decide(world, "approve", proposal)


def receipt_rejected(world: World) -> dict:
    return decide(world, "reject", proposal_create(world))


def receipt_expired(world: World) -> dict:
    proposal = proposal_create(world)
    world.ledger.advance(601)
    return decide(world, "approve", proposal)


def receipt_target_changed(world: World) -> dict:
    proposal = proposal_update(world)
    before = current(world, "tasks", "TAS_t1")
    commit_direct(world.env, world.owner, [update(before, title="שונה בינתיים")],
                  request_id=uuid_v4(0x7001))
    return decide(world, "approve", proposal)


def receipt_unknown(world: World) -> dict:
    return world.backend.receipt(uuid_v4(0x9999))


def _direct_task(world: World, request_id: str):
    before = current(world, "tasks", "TAS_t1")
    commit_direct(world.env, world.owner, [update(before, status="בוצע")], request_id=request_id)
    return before


def receipt_direct_committed(world: World) -> dict:
    _direct_task(world, uuid_v4(0x7002))
    return world.backend.receipt(uuid_v4(0x7002))


def receipt_direct_undone(world: World) -> dict:
    _direct_task(world, uuid_v4(0x7002))
    original = world.backend.receipt(uuid_v4(0x7002))
    current_record = current(world, "tasks", "TAS_t1")
    with world.ledger.transaction(world.owner, domain="core", kind="owner_direct") as tx:
        original_row = tx.receipt(original["request_id"])
    undone = update(current_record, status="לביצוע")
    commit_undo(world.env, world.owner, original_row, [undone], uuid_v4(0x7003))
    return world.backend.receipt(uuid_v4(0x7002))


def receipt_direct_undo_expired(world: World) -> dict:
    _direct_task(world, uuid_v4(0x7002))
    world.ledger.advance(world.knobs.undo_window_s + 1)
    return world.backend.receipt(uuid_v4(0x7002))


def receipt_direct_abandoned(world: World) -> dict:
    request_id = uuid_v4(0x7004)
    with world.ledger.transaction(world.owner, domain="core", kind="owner_direct") as tx:
        proposal = tx.insert_proposal(abandon_draft(tx, request_id, world.knobs.undo_window_s))
        tx.finish(proposal, status="abandoned", receipt_request_id=request_id)
    return world.backend.receipt(request_id)


def _superseded_batch(world: World) -> str:
    world.ledger.transition(
        WORKSPACE, "statements", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    request_id = uuid_v4(0x7005)
    with world.ledger.transaction(world.owner, domain="statements", kind="statement_batch") as tx:
        proposal = tx.insert_proposal(api.ProposalDraft(
            kind="statement_batch", source="ASSISTANT_PASTE", request_id=request_id,
            request_digest=api.digest({"batch": 1}), entity="statements",
            record_id="STM_" + "1" * 32, expected_revision=0, mutations=(),
            dependency_digest=api.digest([]), expires_at=tx.now + timedelta(days=7)))
        tx.finish(proposal, status="superseded", receipt_request_id=proposal.rejection_id,
                  reason_codes=("batch_superseded",))
    return request_id


def receipt_superseded(world: World) -> dict:
    request_id = _superseded_batch(world)
    with world.ledger.transaction(world.owner, domain="core", kind="owner_direct") as tx:
        key = tx.proposal_by_request(request_id).rejection_id
    return world.backend.receipt(key)


def proposal_request_pending(world: World) -> dict:
    proposal = proposal_create(world)
    return world.backend.proposal_request(proposal["request_id"])


def proposal_request_committed(world: World) -> dict:
    proposal = proposal_create(world)
    decide(world, "approve", proposal)
    return world.backend.proposal_request(proposal["request_id"])


def proposal_request_unknown(world: World) -> dict:
    return world.backend.proposal_request(uuid_v4(0x9998))


def proposal_request_abandoned(world: World) -> dict:
    receipt_direct_abandoned(world)
    return world.backend.proposal_request(uuid_v4(0x7004))


def proposal_request_superseded(world: World) -> dict:
    return world.backend.proposal_request(_superseded_batch(world))


def deleted_page(world: World) -> dict:
    before = current(world, "tasks", "TAS_t3")
    commit_direct(world.env, world.owner, [tombstone("tasks", before)], request_id=uuid_v4(0x7006))
    return world.backend.deleted("tasks")


_MIRROR = {"mirror": verified_mirror}

SHAPES: dict[str, Callable[[], dict]] = {
    "schema": _in_world(schema),
    "snapshot_tasks_page": _in_world(snapshot_tasks_page, **_MIRROR),
    "snapshot_tasks_second_page": _in_world(snapshot_tasks_second_page, **_MIRROR),
    "snapshot_expenses_page": _in_world(snapshot_expenses_page, **_MIRROR),
    "snapshot_unchanged": _in_world(snapshot_unchanged, **_MIRROR),
    "pending_list": _in_world(pending_list),
    "proposal_create": _in_world(proposal_create),
    "proposal_update": _in_world(proposal_update),
    "proposal_promote_lead": _in_world(proposal_promote_lead),
    "receipt_committed": _in_world(receipt_committed),
    "receipt_rejected": _in_world(receipt_rejected),
    "receipt_expired": _in_world(receipt_expired),
    "receipt_target_changed": _in_world(receipt_target_changed),
    "receipt_unknown": _in_world(receipt_unknown),
    "receipt_direct_committed": _in_world(receipt_direct_committed),
    "receipt_direct_undone": _in_world(receipt_direct_undone),
    "receipt_direct_undo_expired": _in_world(receipt_direct_undo_expired),
    "receipt_direct_abandoned": _in_world(receipt_direct_abandoned),
    "receipt_superseded": _in_world(receipt_superseded),
    "proposal_request_pending": _in_world(proposal_request_pending),
    "proposal_request_committed": _in_world(proposal_request_committed),
    "proposal_request_unknown": _in_world(proposal_request_unknown),
    "proposal_request_abandoned": _in_world(proposal_request_abandoned),
    "proposal_request_superseded": _in_world(proposal_request_superseded),
    "deleted_page": _in_world(deleted_page),
}

# Which strict reader (contract.READERS key) each shape must satisfy.
READER_OF: dict[str, str] = {
    "schema": "schema",
    "snapshot_tasks_page": "snapshot",
    "snapshot_tasks_second_page": "snapshot",
    "snapshot_expenses_page": "snapshot",
    "snapshot_unchanged": "snapshot",
    "pending_list": "pending",
    "proposal_create": "proposal",
    "proposal_update": "proposal",
    "proposal_promote_lead": "proposal",
    "receipt_committed": "receipt",
    "receipt_rejected": "receipt",
    "receipt_expired": "receipt",
    "receipt_target_changed": "receipt",
    "receipt_unknown": "receipt",
    "receipt_direct_committed": "receipt",
    "receipt_direct_undone": "receipt",
    "receipt_direct_undo_expired": "receipt",
    "receipt_direct_abandoned": "receipt",
    "receipt_superseded": "receipt",
    "proposal_request_pending": "proposal_request",
    "proposal_request_committed": "proposal_request",
    "proposal_request_unknown": "proposal_request",
    "proposal_request_abandoned": "proposal_request",
    "proposal_request_superseded": "proposal_request",
    "deleted_page": "deleted",
}
