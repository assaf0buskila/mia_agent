"""V2-24 D4: the owner-direct wire bodies are V2-20's (``contract.py``, §10.2), not a copy.

``receipt_v2`` and the ``GET /deleted`` body used to be built by private helpers in
``owner_direct.py`` that mirrored V2-20's rules. V2-20 is on master now, so ``owner_direct`` builds
both through ``contract.receipt_v2`` and ``contract.deleted_body``. The byte-equal goldens
(``tests/contract/test_v2_24_goldens.py``) prove the output did not move; these cases prove the
builders are the ones in use and that V2-20's exact-key readers (the frontend's mirror) accept every
body the service emits, on the fake and on PostgreSQL.
"""

from __future__ import annotations

import pytest
from app.services.os_ledger import contract, owner_direct

from tests.fakes.os_ledger_builders import commit_direct, rid, task
from tests.services.os_ledger.owner_direct_support import (
    BACKENDS,
    TASK_OPEN,
    KnobSettings,
    ledger_case,
    owner_ctx,
    single,
)


@pytest.fixture(params=BACKENDS)
def env(request):
    return ledger_case(request.param)


def spy(monkeypatch, name: str) -> list:
    """Record what ``contract.<name>`` returns, still returning it."""
    original = getattr(contract, name)
    seen: list = []

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        seen.append(result)
        return result

    monkeypatch.setattr(contract, name, wrapper)
    return seen


def receipts(env) -> list[dict]:
    """One body of every kind and status the service emits, in call order."""
    settings, context = KnobSettings(env), owner_ctx()
    commit_direct(env, context, [task("TAS_t1"), task("TAS_t2")])
    bodies = []
    created = owner_direct.commit(env, context, single("create", patch={
        "title": "New", "status": TASK_OPEN}), settings=settings)
    bodies.append(created)
    bodies.append(owner_direct.commit(  # a replay of the same request id
        env, context, single("create", patch={"title": "New", "status": TASK_OPEN},
                             request_id=created["request_id"]), settings=settings))
    bodies.append(owner_direct.commit(  # a stale revision: a target_changed receipt
        env, context, single("complete", record_id="TAS_t1", revision=9), settings=settings))
    done = owner_direct.commit(
        env, context, single("complete", record_id="TAS_t2", revision=1), settings=settings)
    bodies.append(done)
    bodies.append(owner_direct.undo(env, context, done["request_id"],
                                    {"undo_request_id": rid()}, settings=settings))
    bodies.append(owner_direct.abandon(env, context, rid(), {}, settings=settings))
    return bodies


def deleted_page(env) -> dict:
    settings, context = KnobSettings(env), owner_ctx()
    commit_direct(env, context, [task("TAS_d1")])
    owner_direct.commit(env, context, single("delete", record_id="TAS_d1", revision=1),
                        settings=settings)
    return owner_direct.deleted(env, context, "entity=tasks&limit=10", settings=settings)


def test_every_receipt_is_built_by_contract_receipt_v2(env, monkeypatch):
    built = spy(monkeypatch, "receipt_v2")
    bodies = receipts(env)
    assert len(bodies) == 6
    assert built == bodies, "each owner-direct answer is exactly contract.receipt_v2's output"


def test_the_deleted_page_is_built_by_contract_deleted_body(env, monkeypatch):
    built = spy(monkeypatch, "deleted_body")
    page = deleted_page(env)
    assert built == [page]
    assert [record["task_id"] for record in page["records"]] == ["TAS_d1"]


def test_v2_20_readers_accept_every_body_the_service_emits(env):
    bodies = receipts(env)
    assert {b["status"] for b in bodies} == {"committed", "target_changed", "abandoned"}
    assert {b["kind"] for b in bodies} == {"owner_direct", "owner_undo"}
    for body in bodies:
        assert contract.read_receipt_v2(body) == body
    page = deleted_page(env)
    assert contract.read_deleted_page(page) == page
