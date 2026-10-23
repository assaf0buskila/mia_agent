"""V2-24 golden shapes: the §11.1 owner-direct bodies, built by the real ``owner_direct`` service.

Each shape builds its own deterministic world (a FakeLedger with a frozen clock and counter-made
ids) and returns the body the service produced. V2-20's ``scripts/export_os_contract_fixtures.py``
registry loads this module and writes ``tests/contract/golden/v2-24/<shape>.json``; until V2-20
merges, ``tests/contract/test_v2_24_goldens.py`` pins the same files byte for byte. Nothing here
is hand-written: a body the service cannot build does not belong here.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.core.config import MiaEnv, Settings
from app.services.os_ledger import api, owner_direct
from tests.fakes.contexts import make_ctx
from tests.fakes.os_ledger import FakeLedger
from tests.fakes.os_ledger_builders import (
    PAYMENT_RECEIVED,
    TASK_DONE,
    client,
    commit_direct,
    payment,
    task,
    work,
)
from tests.fakes.os_ledger_env import NOW, OWNER_ID, WORKSPACE, Knobs, LedgerEnv

OWNER_SCOPES = frozenset({"assaf-os:read", "assaf-os:direct"})


def uuid_v4(number: int) -> str:
    """A deterministic, well-formed UUIDv4."""
    return str(UUID(int=number, version=4))


class Counter:
    def __init__(self, base: int, render: Callable[[int], str]) -> None:
        self._numbers = itertools.count(base + 1)
        self._render = render

    def __call__(self) -> str:
        return self._render(next(self._numbers))


@dataclass
class World:
    ledger: FakeLedger
    env: LedgerEnv  # the LedgerTx-level seeding helpers read env.knobs
    settings: Settings
    owner: api.TrustedOwnerContext
    new_id: Counter

    def commit(self, body: dict[str, Any]) -> dict[str, Any]:
        return owner_direct.commit(self.ledger, self.owner, body, settings=self.settings,
                                   new_id=self.new_id)

    def undo(self, request_id: str, undo_request_id: str) -> dict[str, Any]:
        return owner_direct.undo(self.ledger, self.owner, request_id,
                                 {"undo_request_id": undo_request_id}, settings=self.settings)

    def abandon(self, request_id: str) -> dict[str, Any]:
        return owner_direct.abandon(self.ledger, self.owner, request_id, {},
                                    settings=self.settings)

    def seed(self, number: int, mutations) -> None:
        commit_direct(self.env, self.owner, mutations, request_id=uuid_v4(number))


def build_world() -> World:
    """Epoch 1, core active, three open tasks and one done task (all at revision 1)."""
    ledger = FakeLedger(clock=lambda: NOW, uuid4=Counter(0x1000, uuid_v4),
                        settings_epoch=lambda: 1)
    ledger.ensure_workspace(WORKSPACE, OWNER_ID, 1)
    ledger.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    settings = Settings(
        _env_file=None, env=MiaEnv.TEST, telegram_owner_user_ids=OWNER_ID, kill_switch=False,
        assaf_os_backend="postgres", assaf_os_ledger_enabled=True, assaf_os_authority_epoch=1,
        assaf_os_owner_direct_enabled=True, assaf_os_freeze=False, owner_undo_window_seconds=30,
        assaf_os_workspace_id=WORKSPACE)
    owner = make_ctx("OS", owner=int(OWNER_ID), workspace=WORKSPACE, scopes=OWNER_SCOPES,
                     browser_mutation_verified=True)
    env = LedgerEnv("fake", ledger, Knobs(), rows=ledger.rows, version=ledger.version,
                    now=ledger._now, advance=ledger.advance)
    world = World(ledger, env, settings, owner, Counter(0xB000, lambda n: f"{n:032x}"))
    world.seed(0x7001, [
        task("TAS_t1", title="להכין הצעת מחיר", due_date="2026-10-20"),
        task("TAS_t2", title="לחזור ללקוח"),
        task("TAS_t3", title="לשלוח חשבונית", status=TASK_DONE, due_date="2026-10-01"),
        task("TAS_t4", title="לסגור חודש", status=TASK_DONE),
    ])
    return world


def single(number: int, op: str, *, entity: str = "tasks", record_id: str | None = None,
           revision: int = 0, patch: dict | None = None) -> dict[str, Any]:
    return {"request_id": uuid_v4(number), "op": op, "entity": entity, "record_id": record_id,
            "expected_revision": revision, "patch": patch or {}}


def bulk(number: int, op: str, items) -> dict[str, Any]:
    return {"request_id": uuid_v4(number), "op": op, "entity": "tasks",
            "items": [{"record_id": i, "expected_revision": r} for i, r in items]}


# --- POST /commits ------------------------------------------------------------------------------


def commit_create_committed() -> dict:
    return build_world().commit(single(0x7101, "create", patch={
        "title": "להזמין ציוד", "status": "לביצוע", "due_date": "2026-10-25"}))


def commit_update_committed() -> dict:
    return build_world().commit(single(0x7102, "update", record_id="TAS_t1", revision=1,
                                       patch={"title": "להכין הצעת מחיר מעודכנת"}))


def commit_complete_committed() -> dict:
    return build_world().commit(single(0x7103, "complete", record_id="TAS_t1", revision=1))


def commit_reopen_committed() -> dict:
    return build_world().commit(single(0x7104, "reopen", record_id="TAS_t3", revision=1))


def commit_delete_committed() -> dict:
    return build_world().commit(single(0x7105, "delete", record_id="TAS_t2", revision=1))


def commit_delete_payment_committed() -> dict:
    world = build_world()
    world.seed(0x7002, [client(name="לקוח"), work(title="עבודה"),
                        payment(kind=PAYMENT_RECEIVED)])
    return world.commit(single(0x7106, "delete", entity="payments", record_id="PAY_p1",
                               revision=1))


def commit_restore_committed() -> dict:
    world = build_world()
    world.commit(single(0x7107, "delete", record_id="TAS_t2", revision=1))
    return world.commit(single(0x7108, "restore", record_id="TAS_t2", revision=2))


def commit_bulk_complete_committed() -> dict:
    return build_world().commit(bulk(0x7109, "bulk_complete", [("TAS_t2", 1), ("TAS_t1", 1)]))


def commit_bulk_delete_done_committed() -> dict:
    return build_world().commit(bulk(0x710A, "bulk_delete_done",
                                     [("TAS_t3", 1), ("TAS_t4", 1)]))


def commit_target_changed() -> dict:
    world = build_world()
    world.commit(single(0x710B, "update", record_id="TAS_t1", revision=1,
                        patch={"title": "שונה בינתיים"}))
    return world.commit(single(0x710C, "complete", record_id="TAS_t1", revision=1))


def commit_bulk_target_changed() -> dict:
    return build_world().commit(bulk(0x710D, "bulk_delete_done",
                                     [("TAS_t3", 1), ("TAS_t1", 1)]))


def commit_undone_replay() -> dict:
    world = build_world()
    body = single(0x710E, "complete", record_id="TAS_t1", revision=1)
    world.commit(body)
    world.undo(body["request_id"], uuid_v4(0x720E))
    return world.commit(body)


# --- POST /commits/{id}/undo and /abandon --------------------------------------------------------


def undo_committed() -> dict:
    world = build_world()
    body = single(0x710F, "complete", record_id="TAS_t1", revision=1)
    world.commit(body)
    return world.undo(body["request_id"], uuid_v4(0x720F))


def undo_bulk_committed() -> dict:
    world = build_world()
    body = bulk(0x7110, "bulk_delete_done", [("TAS_t3", 1), ("TAS_t4", 1)])
    world.commit(body)
    return world.undo(body["request_id"], uuid_v4(0x7210))


def abandon_abandoned() -> dict:
    return build_world().abandon(uuid_v4(0x7111))


def abandon_late_original() -> dict:
    world = build_world()
    world.abandon(uuid_v4(0x7112))
    return world.commit(single(0x7112, "create", patch={"title": "מאוחר", "status": "לביצוע"}))


# --- GET /deleted --------------------------------------------------------------------------------


def deleted_page() -> dict:
    world = build_world()
    world.commit(single(0x7113, "delete", record_id="TAS_t2", revision=1))
    return owner_direct.deleted(world.ledger, world.owner, "entity=tasks&limit=50",
                                settings=world.settings)


SHAPES: dict[str, Callable[[], dict]] = {
    "commit_create_committed": commit_create_committed,
    "commit_update_committed": commit_update_committed,
    "commit_complete_committed": commit_complete_committed,
    "commit_reopen_committed": commit_reopen_committed,
    "commit_delete_committed": commit_delete_committed,
    "commit_delete_payment_committed": commit_delete_payment_committed,
    "commit_restore_committed": commit_restore_committed,
    "commit_bulk_complete_committed": commit_bulk_complete_committed,
    "commit_bulk_delete_done_committed": commit_bulk_delete_done_committed,
    "commit_target_changed": commit_target_changed,
    "commit_bulk_target_changed": commit_bulk_target_changed,
    "commit_undone_replay": commit_undone_replay,
    "undo_committed": undo_committed,
    "undo_bulk_committed": undo_bulk_committed,
    "abandon_abandoned": abandon_abandoned,
    "abandon_late_original": abandon_late_original,
    "deleted_page": deleted_page,
}
