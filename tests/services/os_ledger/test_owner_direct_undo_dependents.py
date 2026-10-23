"""V2-24 undo vs live dependents (§11.1 step 6, §22 E20 and E26's last bullet), fake and PostgreSQL.

E26 asks V2-24 to pre-check dependents for an undo that lands a record in its end state, mapped to
``undo_conflict(dependent_exists)``. The ledger's own ``apply`` refuses such a move too
(``has_dependents``, E20), but only after the undo proposal was inserted and, on a rollback, as a
different error. These cases pin the refusal as V2-24's own, BEFORE any write: the transaction never
calls ``insert_proposal``, ``apply`` or ``finish``, every table and the version are unchanged, and
the answer is ``undo_conflict`` with ``reason: dependent_exists`` and the live referrers in
``changed``. They cover every referenced entity (clients, works, payments) and both ways an undo
lands in the end state: the undo of a create, and the undo of an update that revived the record.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from app.services.os_ledger import api, owner_direct

from tests.fakes.os_ledger_builders import (
    CLIENT_ACTIVE,
    CLIENT_INACTIVE,
    PAYMENT_CANCELLED,
    PAYMENT_RECEIVED,
    PAYMENT_REFUND,
    WORK_ACTIVE,
    WORK_CANCELLED,
    client,
    commit_direct,
    current,
    payment,
    rid,
    task,
    work,
)
from tests.services.os_ledger.owner_direct_support import (
    BACKENDS,
    TASK_DONE,
    KnobSettings,
    ledger_case,
    owner_ctx,
    single,
    state,
)

WRITES = frozenset({"insert_proposal", "apply", "finish"})


@pytest.fixture(params=BACKENDS)
def env(request):
    return ledger_case(request.param)


class _SpiedTx:
    """A ``LedgerTx`` that records every write call by name and passes everything through."""

    def __init__(self, tx, log):
        self._tx, self._log = tx, log

    def __getattr__(self, name):
        attribute = getattr(self._tx, name)
        if name not in WRITES:
            return attribute

        def write(*args, **kwargs):
            self._log.append(name)
            return attribute(*args, **kwargs)

        return write


class SpiedLedger:
    """The ledger ``owner_direct`` is given, logging the writes each transaction attempts."""

    def __init__(self, env):
        self._env = env
        self.writes: list[str] = []

    @contextmanager
    def transaction(self, *args, **kwargs):
        with self._env.transaction(*args, **kwargs) as tx:
            yield _SpiedTx(tx, self.writes)


def commit(env, body):
    return owner_direct.commit(env, owner_ctx(), body, settings=KnobSettings(env))


def undo(env, request_id, ledger=None):
    return owner_direct.undo(ledger or env, owner_ctx(), request_id, {"undo_request_id": rid()},
                             settings=KnobSettings(env))


def seed(env, *mutations):
    commit_direct(env, owner_ctx(), list(mutations))


def refused_with_dependents(env, original, referrers):
    """Undo ``original`` through a spied ledger: refused, as the contract says, and unwritten."""
    spied = SpiedLedger(env)
    snapshot = state(env)
    with pytest.raises(api.LedgerError) as caught:
        undo(env, original["request_id"], ledger=spied)
    error = caught.value
    assert error.code == "undo_conflict" and error.write_outcome == "not_started"
    assert error.details == {"reason": "dependent_exists", "changed": [
        {"entity": entity, "record_id": record_id} for entity, record_id in referrers]}
    assert spied.writes == [], "refused before the proposal, the apply or the receipt"
    assert state(env) == snapshot, "no row, event, outbox entry or version moved"


# --- the undo of an update that revived a cancelled record ----------------------------------------


def revive_client(env):
    seed(env, client(status=CLIENT_INACTIVE))
    revived = commit(env, single("update", entity="clients", record_id="PER_c1", revision=1,
                                 patch={"status": CLIENT_ACTIVE}))
    assert current(env, "clients", "PER_c1")["status"] == CLIENT_ACTIVE
    seed(env, work())
    return revived, [("works", "WOR_w1")]


def revive_work(env):
    seed(env, client(), work(status=WORK_CANCELLED))
    revived = commit(env, single("update", entity="works", record_id="WOR_w1", revision=1,
                                 patch={"status": WORK_ACTIVE}))
    assert current(env, "works", "WOR_w1")["status"] == WORK_ACTIVE
    seed(env, payment())
    return revived, [("payments", "PAY_p1")]


def revive_payment(env):
    seed(env, client(), work(), payment(kind=PAYMENT_CANCELLED))
    revived = commit(env, single("update", entity="payments", record_id="PAY_p1", revision=1,
                                 patch={"kind": PAYMENT_RECEIVED}))
    assert current(env, "payments", "PAY_p1")["kind"] == PAYMENT_RECEIVED
    seed(env, payment("PAY_r1", kind=PAYMENT_REFUND, original_payment_id="PAY_p1"))
    return revived, [("payments", "PAY_r1")]


@pytest.mark.parametrize("revive", [revive_client, revive_work, revive_payment],
                         ids=["clients", "works", "payments"])
def test_an_undo_that_would_re_cancel_a_referenced_record_is_dependent_exists(env, revive):
    original, referrers = revive(env)
    refused_with_dependents(env, original, referrers)


# --- the undo of a create of a record that gained a live referrer ---------------------------------


def create_client(env):
    created = commit(env, single("create", entity="clients",
                                 patch={"name": "C", "status": CLIENT_ACTIVE}))
    person = created["records"][0]["record"]["person_id"]
    seed(env, work("WOR_w9", person_id=person))
    return created, [("works", "WOR_w9")]


def create_work(env):
    seed(env, client())
    created = commit(env, single("create", entity="works", patch={
        "person_id": "PER_c1", "title": "W", "path": work().after["path"],
        "status": WORK_ACTIVE, "currency": "ILS"}))
    work_id = created["records"][0]["record"]["work_id"]
    seed(env, payment("PAY_p9", work_id=work_id))
    return created, [("payments", "PAY_p9")]


def create_payment(env):
    seed(env, client(), work())
    created = commit(env, single("create", entity="payments", patch={
        k: v for k, v in payment().after.items() if k not in {"payment_id", "revision"}}))
    payment_id = created["records"][0]["record"]["payment_id"]
    seed(env, payment("PAY_r9", kind=PAYMENT_REFUND, original_payment_id=payment_id))
    return created, [("payments", "PAY_r9")]


@pytest.mark.parametrize("make", [create_client, create_work, create_payment],
                         ids=["clients", "works", "payments"])
def test_an_undo_of_a_create_with_a_live_referrer_is_dependent_exists(env, make):
    original, referrers = make(env)
    refused_with_dependents(env, original, referrers)


# --- referrers that do not block ------------------------------------------------------------------


def test_a_done_task_and_a_cancelled_work_do_not_block_the_undo_of_a_create(env):
    created = commit(env, single("create", entity="clients",
                                 patch={"name": "C", "status": CLIENT_ACTIVE}))
    person = created["records"][0]["record"]["person_id"]
    seed(env, task("TAS_t9", person_id=person, status=TASK_DONE),
         work("WOR_w9", person_id=person, status=WORK_CANCELLED))
    body = undo(env, created["request_id"])
    assert (body["status"], body["kind"]) == ("committed", "owner_undo")
    assert current(env, "clients", person)["status"] == CLIENT_INACTIVE


def test_the_refusal_lifts_once_the_referrer_is_gone(env):
    original, referrers = revive_work(env)
    refused_with_dependents(env, original, referrers)
    removed = commit(env, single("delete", entity="payments", record_id="PAY_p1", revision=1))
    assert removed["status"] == "committed"
    body = undo(env, original["request_id"])
    assert body["status"] == "committed" and current(env, "works", "WOR_w1")["status"] == (
        WORK_CANCELLED)
