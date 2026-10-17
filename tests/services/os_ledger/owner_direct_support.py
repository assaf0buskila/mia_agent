"""V2-24 test support: the ledger case per backend, a settings view over its knobs, and bodies.

The owner-direct service tests run against ``LedgerEnv`` only (the same seam as the conformance
suite), so one test proves the fake and, once V2-16's ``core.py`` lands, PostgreSQL. The pg case is
generated exactly when the conftest's ``LEDGER_CASES`` holds it (core present and the URL set),
so it is never skipped (§3.4) and never stubbed.
"""

from __future__ import annotations

import os
from typing import Any

from app.prototypes.assaf_os_domain import ENUMS
from app.services.os_ledger import api

from tests.fakes.os_ledger_builders import ctx, rid
from tests.fakes.os_ledger_env import OWNER_ID, LedgerEnv, build_fake_env
from tests.services.os_ledger.conftest import (
    LEDGER_CASES,
    PG_URL_ENV,
    _prepare,
    build_pg_env,
)

# The conftest's conditional case list: "pg" only when V2-16's core and the URL are both present.
BACKENDS = tuple(LEDGER_CASES)
TASK_OPEN, TASK_DOING, TASK_WAITING, TASK_DONE, TASK_CANCELLED = ENUMS["task_status"]
ENTITY_TABLES = tuple("os_" + entity for entity in api.ENTITIES)
LEDGER_TABLES = ("os_proposals", "os_receipts", "os_events", "os_outbox")


def ledger_case(case: str) -> LedgerEnv:
    """A prepared workspace (epoch 1, core active) on ``case``, a member of ``BACKENDS``."""
    if case == "fake":
        return _prepare(build_fake_env())
    assert case == "pg" and case in BACKENDS, case
    return _prepare(build_pg_env(os.environ[PG_URL_ENV]))


class KnobSettings:
    """The settings the owner-direct service reads, following the ledger's ``Knobs``."""

    def __init__(self, env: LedgerEnv, *, owner: str = OWNER_ID, direct: bool = True) -> None:
        self._knobs = env.knobs
        self.owner = owner
        self.assaf_os_owner_direct_enabled = direct

    @property
    def kill_switch(self) -> bool:
        return self._knobs.kill_switch

    @property
    def assaf_os_freeze(self) -> bool:
        return self._knobs.freeze

    @property
    def assaf_os_backend(self) -> str:
        return self._knobs.backend

    @property
    def assaf_os_authority_epoch(self) -> int:
        return self._knobs.settings_epoch

    @property
    def owner_undo_window_seconds(self) -> int:
        return self._knobs.undo_window_s

    def telegram_owner_user_id_set(self) -> set[str]:
        return {self.owner}


def single(op: str, entity: str = "tasks", record_id: str | None = None, revision: int = 0,
           patch: dict | None = None, request_id: str | None = None) -> dict[str, Any]:
    return {"request_id": request_id or rid(), "op": op, "entity": entity,
            "record_id": record_id, "expected_revision": revision, "patch": patch or {}}


def bulk(op: str, items, request_id: str | None = None) -> dict[str, Any]:
    return {"request_id": request_id or rid(), "op": op, "entity": "tasks",
            "items": [{"record_id": i, "expected_revision": r} for i, r in items]}


def patch_of(mutation: api.Mutation) -> dict[str, Any]:
    """The input fields of a builder mutation: its ``after`` minus the system keys."""
    system = {api.ID_KEYS[mutation.entity], "revision"} | (
        {"person_id"} if mutation.entity == "leads" else set())
    return {k: v for k, v in mutation.after.items() if k not in system}


def state(env: LedgerEnv) -> dict[str, Any]:
    """Every row a ledger write can touch, plus the version and ``pending_seq``."""
    tables = {name: sorted(map(repr, env.rows(name))) for name in ENTITY_TABLES + LEDGER_TABLES}
    workspace = env.rows("os_workspaces")[0]
    return tables | {"version": env.version(), "pending_seq": workspace["pending_seq"]}


def owner_ctx(channel: str = "OS", **fields):
    return ctx(channel, **fields)
