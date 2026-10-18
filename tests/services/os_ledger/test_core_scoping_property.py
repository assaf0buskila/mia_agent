"""§22 E23 (V2-16b): the scoped core checks equal V2-16's full ones, on the fake and on PG.

V2-16 checked every write against the transaction's complete record state: ``apply`` ran
``validate_transition`` over ``snapshot_records()`` for each mutation, ``decide`` digested the
full snapshot, and ``propose`` ran ``validate_transition``, ``validate_state`` and
``build_summaries`` over all of it. V2-16b scopes each to the records the write can affect
(``tx.ScopedCoreChecks``); the full versions stay as ``tx.FullCoreChecks``, the reference. The
proofs:

* fake, per call: seeded random commit/undo/propose/decide sequences (the V2-18 generator, plus
  leads and promotions) drive a FakeLedger. Before every operation the full function (over the
  fake's complete state) and the scoped one (over ``FakeCoreReader``) run on the SAME state: each
  mutation of a batch at its point in the batch, each proposal, each decision's digest. Same
  accept/reject, same code and reason, same digest.
* PG, per call and end to end: a twin whose reference is V2-16 in full (``FullCoreChecks``,
  ``FullStateValidator``, ``FullSummaries``) and whose system under test is ``build_ledger()``'s
  defaults with ``DualCoreChecks`` (both checks on the same state, the scoped result used). Every
  call agrees, and both sides agree on every outcome and every row after every step.
* scripted scenarios for what random sequences reach rarely (bulk orders, a referrer left behind by
  an earlier tombstone, refund siblings, leads, promotions, a summary overflow), on both.
* PG: no write path loads the whole workspace any more.
"""

from __future__ import annotations

import os
import random
from uuid import NAMESPACE_URL, uuid5

import pytest
from app.services.os_ledger import api
from app.services.os_ledger.domain import DomainError, canonical_input, validate_transition
from app.services.os_ledger.ids import dependency_digest
from app.services.os_ledger.summaries import SummaryInputError

from tests.fakes.os_ledger import FakeLedger, _wire
from tests.fakes.os_ledger_builders import (
    CLIENT_ACTIVE,
    LEAD_NEW,
    LEAD_RELEVANT,
    PATH_AUTOMATION,
    PATH_LEARN,
    PAYMENT_REFUND,
    TASK_DONE,
    TASK_OPEN,
    WORK_CANCELLED,
    client,
    commit_direct,
    ctx,
    current,
    expense,
    lead,
    payment,
    restore,
    rid,
    task,
    time_entry,
    tombstone,
    update,
    work,
)
from tests.fakes.os_ledger_env import NOW, OWNER_ID, WORKSPACE, Knobs, LedgerEnv
from tests.services.os_ledger.conftest import PG_URL_ENV, _prepare, build_pg_env, pg_enabled
from tests.services.os_ledger.test_summaries_property_engine import (
    MAX_SAFE,
    RECORD_ENTITIES,
    TWIN,
    Generator,
    Side,
    Twin,
    attempt,
    compare_state,
    model_of,
    run,
    uuid_source,
)

FAKE_SEEDS = range(6)
PG_SEEDS = (100, 113, 140)
PHONES = ("0501234567", "050-123-4567", "+972501234567", "0521112222", "+14155550100", "12345")
EMAILS = ("a@x.io", " A@X.io", "b@y.io", "c@z.io")


def checks():
    """The V2-16b API, imported on use so that each test fails on its own before it exists."""
    from app.services.os_ledger import tx

    return tx


def needs_pg():
    if not pg_enabled():
        pytest.skip("disposable MIA_OS_LEDGER_TEST_POSTGRES_URL required")


def outcome(call):
    try:
        return ("ok", call())
    except (DomainError, SummaryInputError) as exc:
        return ("refused", exc.code)


# --- the fake's reader and per-call probe -----------------------------------------------------


class FakeCoreReader:
    """``tx.CoreReader`` over the FakeLedger's in-memory tables (the live rows, as wire records)."""

    def __init__(self, tx):
        self.tx = tx

    def _live(self, entity):
        return [_wire(r, entity) for r in self.tx._table("os_" + entity).values()
                if r["deleted_at"] is None]

    def live(self, entity, ids):
        wanted = set(ids)
        return {w[api.ID_KEYS[entity]]: w for w in self._live(entity)
                if w[api.ID_KEYS[entity]] in wanted}

    def referrers(self, entity, field, ids):
        wanted = set(ids)
        return sorted((w for w in self._live(entity) if w.get(field) in wanted),
                      key=lambda w: w[api.ID_KEYS[entity]])

    def same_source(self, source_type, source_key):
        return [w for w in self._live("expenses")
                if (w["source_type"], w["source_key"]) == (source_type, source_key)]

    def contact_leads(self):
        return [w for w in self._live("leads")
                if w.get("phone") is not None or w.get("email") is not None]

    def magnitudes(self):
        money = sum(r["amount_minor"] for e in ("payments", "expenses")
                    for r in self.tx._table("os_" + e).values() if r["deleted_at"] is None)
        minutes = sum(r["minutes"] for r in self.tx._table("os_time").values()
                      if r["deleted_at"] is None)
        return money, minutes

    def snapshot(self):
        return self.tx._records()


class _Rollback(AssertionError):
    """Ends a probe's transaction; the FakeLedger restores its tables and re-raises it."""


def probe(fake, kind, body):
    try:
        with fake.transaction(ctx(), domain="core", kind=kind) as tx:
            body(tx)
            raise _Rollback
    except _Rollback:
        pass


def proposed(tx, entity, record_id, expected, values, mint):
    """The mutations ``LedgerCore.propose`` builds, or None where it refuses before its checks."""
    promote = entity == "promote_lead"
    before = tx.get("leads" if promote else entity, record_id) if record_id else None
    if (record_id is None and expected != 0) or (
            record_id is not None and (before is None or before["revision"] != expected)):
        return None
    if promote:
        from app.services.os_ledger.core import PgLedger

        try:
            return PgLedger._promotion(tx.get("clients", before["person_id"]), before,
                                       record_id, expected)
        except DomainError:
            return None
    try:
        values = canonical_input(entity, dict(values), creating=record_id is None)
    except DomainError:
        return None
    record_id = record_id or mint(entity)
    after = (before or {}) | values | {api.ID_KEYS[entity]: record_id, "revision": expected + 1}
    if entity == "leads":
        after["person_id"] = before["person_id"] if before else mint("clients")
    return (api.Mutation(entity, record_id, "upsert", before, after),)


class FakeProbe:
    """Runs the full and the scoped function on the fake's current state; records both results."""

    def __init__(self, fake):
        self.fake, self.calls, self.n = fake, [], 0

    def mint(self, entity):
        self.n += 1
        return {"clients": "PER", "leads": "LEA"}.get(entity, entity[:3].upper()) + f"_m{self.n}"

    def batch(self, mutations, kind):
        tx_module = checks()

        def body(tx):
            reader = FakeCoreReader(tx)
            for index, m in enumerate(mutations):
                before = None if m.op == "restore" else m.before
                full = outcome(lambda m=m, b=before: validate_transition(
                    m.entity, b, dict(m.after), tx._records()))
                scoped = outcome(lambda m=m, b=before, i=index: tx_module.scoped_transition(
                    reader, m.entity, b, dict(m.after), tuple(mutations[:i])))
                self.calls.append(("transition", full, scoped))
                if full[0] != "ok":
                    return
                try:
                    tx._apply_one(m, "OS_DIRECT", rid(), mutations)
                except api.LedgerError:
                    return

        probe(self.fake, kind, body)

    def proposal(self, entity, record_id, expected, values):
        tx_module = checks()

        def body(tx):
            mutations = proposed(tx, entity, record_id, expected, values, self.mint)
            if mutations is None:
                return
            reader, records = FakeCoreReader(tx), tx._records()
            self.calls.append(("proposal",
                               outcome(lambda: tx_module.full_proposal_check(records, mutations)),
                               outcome(lambda: tx_module.scoped_proposal_check(reader,
                                                                               mutations))))
            self.calls.append(("digest", ("ok", dependency_digest(records, mutations)),
                               ("ok", tx_module.scoped_dependency_digest(reader, mutations))))

        probe(self.fake, "proposal", body)

    def decision(self, row):
        tx_module = checks()

        def body(tx):
            full = dependency_digest(tx._records(), row.mutations)
            scoped = tx_module.scoped_dependency_digest(FakeCoreReader(tx), row.mutations)
            self.calls.append(("decide_digest", ("ok", full), ("ok", scoped)))
            self.calls.append(("digest_moved", full != row.dependency_digest, None))

        probe(self.fake, "proposal", body)


def new_fake(seed):
    fake = FakeLedger(clock=lambda: NOW, uuid4=uuid_source(seed), settings_epoch=lambda: 1)
    env = LedgerEnv("fake", fake, Knobs(), rows=fake.rows, version=fake.version, now=fake._now,
                    advance=fake.advance)
    env.ensure_workspace(WORKSPACE, OWNER_ID, 1)
    env.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id=OWNER_ID)
    return fake


def disagreements(calls):
    return [c for c in calls if c[0] != "digest_moved" and c[1] != c[2]]


def refusals(calls):
    return {c[1][1] for c in calls if c[0] in {"transition", "proposal"} and c[1][0] != "ok"}


# --- leads and promotions (the V2-18 generator makes none) ------------------------------------


def lead_op(rng, leads):
    """A lead create, a lead contact change or a promotion, as a ``propose`` step to approve."""
    live = sorted(i for i, (w, dead) in leads.items() if not dead)
    roll = rng.random()
    if live and roll < 0.3:
        lead_id = rng.choice(live)
        return ("propose", "promote_lead", lead_id, leads[lead_id][0]["revision"], {}, "approve")
    if live and roll < 0.55:
        lead_id = rng.choice(live)
        change = rng.choice(({"phone": rng.choice(PHONES)}, {"email": rng.choice(EMAILS)},
                             {"status": LEAD_RELEVANT}))
        return ("propose", "leads", lead_id, leads[lead_id][0]["revision"], change, "approve")
    values = {"name": f"Lead {rng.randint(0, 999)}", "path": rng.choice((PATH_LEARN,
                                                                          PATH_AUTOMATION)),
              "status": LEAD_NEW}
    if rng.random() < 0.8:
        values["phone"] = rng.choice(PHONES)
    if rng.random() < 0.5:
        values["email"] = rng.choice(EMAILS)
    return ("propose", "leads", None, 0, values, "approve")


def fake_run(seed, steps):
    """The V2-18 random sequence on one FakeLedger, probing every operation first."""
    fake = new_fake(seed)
    side, check = Side(fake, WORKSPACE), FakeProbe(fake)
    rng = random.Random(seed)
    gen = Generator(rng, steps, huge=True)
    history: dict[str, tuple[str, list[api.Mutation]]] = {}
    for step in range(steps):
        rows = {e: fake.rows("os_" + e) for e in (*RECORD_ENTITIES, "leads")}
        model = model_of(rows)
        fake.advance(rng.choice((1, 2, 5)))
        op = lead_op(rng, model["leads"]) if rng.random() < 0.12 else gen.next(
            step, model, history)
        if op[0] == "commit" and op[1]:
            check.batch(op[1], "owner_direct")
            request_id = gen.rid()
            if attempt(lambda m=op[1], r=request_id: side.commit(m, r))[0] == "ok":
                gen.commits.append(request_id)
                history[request_id] = (request_id, list(op[1]))
        elif op[0] == "undo":
            check.batch(op[2], "owner_undo")
            attempt(lambda o=op: side.commit(o[2], gen.rid(), kind="owner_undo", original=o[1]))
        elif op[0] == "propose":
            _, entity, record_id, expected, values, then = op
            check.proposal(entity, record_id, expected, values)
            request_id = gen.rid()
            made = attempt(lambda: side.propose(request_id, entity, record_id, expected, values))
            if made[0] != "ok":
                continue
            if then == "change" and record_id is not None:
                record = model[entity][record_id][0]
                nudge = [update(record, minutes=(record["minutes"] + 1) % 300)]
                check.batch(nudge, "owner_direct")
                attempt(lambda: side.commit(nudge, gen.rid()))
            if then == "expire":
                fake.advance(601)
            check.decision(side.proposals[request_id])
            attempt(lambda: side.decide(request_id, "reject" if then == "reject" else "approve"))
    return check.calls


def test_fake_scoped_core_checks_equal_the_full_ones_over_random_sequences():
    seen, kinds, moved = set(), set(), 0
    for seed in FAKE_SEEDS:
        calls = fake_run(seed, 160)
        differ = disagreements(calls)
        assert not differ, f"seed {seed}: full != scoped on {differ[:5]}"
        seen |= refusals(calls)
        kinds |= {c[0] for c in calls}
        moved += sum(1 for c in calls if c[0] == "digest_moved" and c[1])
    assert {"transition", "proposal", "digest", "decide_digest"} <= kinds, kinds
    assert moved, "no decision ever saw a changed dependency digest"
    assert {"client_not_found", "work_not_found", "work_person_mismatch",
            "refund_exceeds_original", "lead_already_exists"} <= seen, seen


# --- PG: the twin with a dual check ------------------------------------------------------------


class DualCoreChecks:
    """Runs the full and the scoped checks on the SAME state, records both, and returns (or
    raises) the scoped result."""

    def __init__(self, full, scoped):
        self.full, self.scoped, self.calls = full, scoped, []

    def _both(self, what, call):
        results = []
        for implementation in (self.full, self.scoped):
            try:
                results.append(("ok", call(implementation), None))
            except (DomainError, SummaryInputError) as exc:
                results.append(("refused", exc.code, exc))
        self.calls.append((what, results[0][:2], results[1][:2]))
        if results[1][2] is not None:
            raise results[1][2]
        return results[1][1]

    def transition(self, tx, entity, before, after, earlier):
        return self._both("transition", lambda c: c.transition(tx, entity, before, after,
                                                                earlier))

    def dependency_digest(self, tx, mutations):
        return self._both("digest", lambda c: c.dependency_digest(tx, mutations))

    def proposal(self, tx, mutations):
        return self._both("proposal", lambda c: c.proposal(tx, mutations))


def pg_core_twin(seed):
    """Reference: V2-16 in full. System under test: ``build_ledger()``'s defaults, dual-checked."""
    from app.services.os_ledger import core
    from app.services.os_ledger.summaries import FullSummaries
    from app.services.os_ledger.validate_full import FullStateValidator

    from tests.fakes.os_ledger_builders import fresh_workspace

    tx_module = checks()
    env = _prepare(build_pg_env(os.environ[PG_URL_ENV]))
    fresh_workspace(env, TWIN, core="active")
    base = env.transaction.__self__
    dual = DualCoreChecks(tx_module.FullCoreChecks(), tx_module.ScopedCoreChecks())
    ref = core.build_ledger(base.settings, clock=base.clock, uuid4=uuid_source(seed),
                            checks=tx_module.FullCoreChecks(), validator=FullStateValidator(),
                            summaries=FullSummaries())
    sut = core.build_ledger(base.settings, clock=base.clock, uuid4=uuid_source(seed),
                            checks=dual)

    def tables(table):
        rows = env.rows(table)
        return tuple([{k: v for k, v in r.items() if k != "workspace_id"}
                      for r in rows if r["workspace_id"] == ws] for ws in (WORKSPACE, TWIN))

    return Twin("pg", Side(ref, WORKSPACE), Side(sut, TWIN), tables, env.advance), dual


def test_pg_scoped_core_checks_equal_the_full_ones_over_random_sequences():
    needs_pg()
    seen, kinds = set(), set()
    for seed in PG_SEEDS:
        twin, dual = pg_core_twin(seed)
        stats = run(twin, seed, 70)  # asserts equal outcomes and rows after every step
        differ = disagreements(dual.calls)
        assert not differ, f"pg seed {seed}: full != scoped on {differ[:5]}"
        seen |= refusals(dual.calls)
        kinds |= {c[0] for c in dual.calls}
        assert stats["accepted"] > 10, stats
    assert {"transition", "proposal", "digest"} <= kinds, kinds
    assert seen, "no check ever refused"


# --- scripted: what random sequences reach rarely --------------------------------------------


def key_id(key):
    return str(uuid5(NAMESPACE_URL, "v2-16b:" + key))


class FakeDriver:
    def __init__(self):
        self.fake = new_fake(7)
        self.side, self.check = Side(self.fake, WORKSPACE), FakeProbe(self.fake)

    def commit(self, key, mutations):
        self.check.batch(mutations, "owner_direct")
        return attempt(lambda: self.side.commit(mutations, key_id(key)))

    def propose(self, key, entity, record_id, expected, values):
        self.check.proposal(entity, record_id, expected, values)
        return attempt(lambda: self.side.propose(key_id(key), entity, record_id, expected,
                                                 values))

    def decide(self, key, decision="approve"):
        self.check.decision(self.side.proposals[key_id(key)])
        return attempt(lambda: self.side.decide(key_id(key), decision))

    def get(self, entity, record_id):
        with self.fake.transaction(ctx(), domain="core", kind="owner_direct") as tx:
            return tx.get(entity, record_id)

    def calls(self):
        return self.check.calls


class PgDriver:
    def __init__(self):
        self.twin, self.dual = pg_core_twin(7)

    def _both(self, call):
        both = self.twin.both(call)
        assert both[0] == both[1], both
        compare_state(self.twin, "scripted")
        return both[0]

    def commit(self, key, mutations):
        return self._both(lambda side: side.commit(mutations, key_id(key)))

    def propose(self, key, entity, record_id, expected, values):
        return self._both(lambda side: side.propose(key_id(key), entity, record_id, expected,
                                                    values))

    def decide(self, key, decision="approve"):
        return self._both(lambda side: side.decide(key_id(key), decision))

    def get(self, entity, record_id):
        side = self.twin.sut
        with side.ledger.transaction(side.ctx(), domain="core", kind="owner_direct") as tx:
            return tx.get(entity, record_id)

    def calls(self):
        return self.dual.calls


def label(result):
    """'ok', a decided receipt's status, or a refusal's reason (its code when it has none)."""
    if result[0] == "ok":
        value = result[1]
        return value[0] if isinstance(value, tuple) and value[0] in {
            "committed", "target_changed", "rejected", "expired"} else "ok"
    return result[2] or result[1]


def bulk_orders(d):
    seed = [client(), work(), time_entry(), task("TAS_d", status=TASK_DONE, work_id="WOR_w1",
                                                person_id="PER_c1")]
    out = [d.commit("seed", seed)]
    c, w, t = d.get("clients", "PER_c1"), d.get("works", "WOR_w1"), d.get("time", "TIM_t1")
    # an earlier tombstone leaves its referrers behind for the next step's check
    out.append(d.commit("wrong", [tombstone("clients", c), tombstone("works", w),
                                  tombstone("time", t)]))
    out.append(d.commit("unrelated", [tombstone("time", t), tombstone("works", w),
                                      task("TAS_x")]))
    out.append(d.commit("back", [tombstone("time", t), tombstone("works", w),
                                 restore("works", {**w, "revision": 2})]))
    return out


def left_behind(d):
    out = [d.commit("seed", [client(), work(status=WORK_CANCELLED), task("TAS_x")])]
    c, x = d.get("clients", "PER_c1"), d.get("tasks", "TAS_x")
    # the cancelled work is not live (E20 lets the tombstone through), but it still names the
    # client: the next mutation's full check sees client_not_found
    out.append(d.commit("gone", [tombstone("clients", c), update(x, title="Renamed")]))
    out.append(d.commit("alone", [tombstone("clients", c)]))
    return out


def refund_siblings(d):
    out = [d.commit("seed", [client(), work(), payment(), expense()])]
    out.append(d.commit("r1", [payment("PAY_r1", kind=PAYMENT_REFUND, original_payment_id="PAY_p1",
                                       actual_amount_ils=400)]))
    out.append(d.commit("r2", [payment("PAY_r2", kind=PAYMENT_REFUND, original_payment_id="PAY_p1",
                                       actual_amount_ils=500)]))
    refund = {k: v for k, v in payment("PAY_x", kind=PAYMENT_REFUND, original_payment_id="PAY_p1",
                                       actual_amount_ils=200).after.items()
              if k not in {"payment_id", "revision"}}
    out.append(d.propose("r3", "payments", None, 0, refund))
    out.append(d.propose("r3-fits", "payments", None, 0, refund | {"actual_amount_ils": 100}))
    original, r1 = d.get("payments", "PAY_p1"), d.get("payments", "PAY_r1")
    out.append(d.commit("grow", [update(r1, actual_amount_ils=600)]))
    out.append(d.commit("shrink", [update(original, actual_amount_ils=800)]))
    out.append(d.commit("late", [update(original, date="2026-12-01")]))
    out.append(d.commit("usd", [update(original, currency="USD")]))
    out.append(d.propose("dup-src", "expenses", None, 0, {
        k: v for k, v in expense("EXP_y").after.items() if k not in {"expense_id", "revision"}}))
    return out


def leads_and_promotions(d):
    out = [d.commit("seed", [lead(), lead("LEA_l2", person_id="PER_l2", phone="0529998888",
                                          email="b@y.io")])]
    base = {"name": "New", "path": PATH_LEARN, "status": LEAD_NEW}
    out.append(d.propose("same-phone", "leads", None, 0, base | {"phone": "+972501234567"}))
    out.append(d.propose("same-email", "leads", None, 0, base | {"email": " B@Y.io"}))
    out.append(d.propose("fresh", "leads", None, 0, base | {"phone": "0541231234"}))
    out.append(d.decide("fresh"))
    l1 = d.get("leads", "LEA_l1")
    out.append(d.propose("contact", "leads", "LEA_l1", l1["revision"], {"email": "q@q.io"}))
    out.append(d.commit("twin-contact", [lead("LEA_l3", person_id="PER_l3", phone="0000000",
                                              email="q@q.io")]))
    out.append(d.decide("contact"))  # the contacts in its digest changed: target_changed
    out.append(d.propose("promote", "promote_lead", "LEA_l1", l1["revision"], {}))
    out.append(d.decide("promote"))
    out.append(d.commit("client-l2", [client("PER_l2", status=CLIENT_ACTIVE)]))
    l2 = d.get("leads", "LEA_l2")
    out.append(d.propose("promote-existing", "promote_lead", "LEA_l2", l2["revision"], {}))
    out.append(d.decide("promote-existing"))
    return out


def summary_overflow(d):
    out = [d.commit("seed", [client(), work(), payment(actual_amount_ils=MAX_SAFE - 5),
                             client("PER_c2"), work("WOR_w2", person_id="PER_c2",
                                                    currency="USD")])]
    values = {k: v for k, v in payment("PAY_x", actual_amount_ils=10).after.items()
              if k not in {"payment_id", "revision"}}
    out.append(d.propose("overflow", "payments", None, 0, values))
    # another client's USD work: no month, currency, work or client total reaches the bound,
    # but the workspace's money does, so the scoped check falls back to the full recompute
    out.append(d.propose("usd", "payments", None, 0, values | {
        "person_id": "PER_c2", "work_id": "WOR_w2", "currency": "USD"}))
    out.append(d.propose("near", "tasks", None, 0, {"title": "T", "status": TASK_OPEN}))
    return out


SCRIPTS = {
    "bulk_orders": (bulk_orders, ["ok", "client_not_found", "work_not_found", "ok"]),
    "left_behind": (left_behind, ["ok", "client_not_found", "client_not_found"]),
    "refund_siblings": (refund_siblings, [
        "ok", "ok", "ok", "refund_exceeds_original", "ok", "refund_exceeds_original",
        "refund_exceeds_original", "refund_before_original", "work_currency_mismatch",
        "duplicate_expense_source"]),
    "leads_and_promotions": (leads_and_promotions, [
        "ok", "lead_already_exists", "lead_already_exists", "ok", "committed", "ok", "ok",
        "target_changed", "ok", "committed", "ok", "ok", "committed"]),
    "summary_overflow": (summary_overflow, ["ok", "amount_overflow", "ok", "ok"]),
}


def drivers():
    yield "fake", FakeDriver
    if pg_enabled():
        yield "pg", PgDriver


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_scripted_scenarios_agree_full_and_scoped_on_every_backend(name):
    script, expected = SCRIPTS[name]
    for kind, build in drivers():
        driver = build()
        assert [label(r) for r in script(driver)] == expected, (kind, name)
        differ = disagreements(driver.calls())
        assert not differ, (kind, name, differ[:5])
        assert driver.calls(), (kind, name)


# --- PG: the write paths no longer load the whole workspace -----------------------------------


def test_pg_write_paths_never_load_the_whole_workspace(monkeypatch):
    needs_pg()
    from app.services.os_ledger.tx import PgLedgerTx

    env = _prepare(build_pg_env(os.environ[PG_URL_ENV]))
    commit_direct(env, ctx(), [client(), work(), payment(), time_entry(), task(), expense(),
                               lead()])

    def whole_workspace(self):
        raise AssertionError("a write path loaded the whole workspace")

    monkeypatch.setattr(PgLedgerTx, "_load_records", whole_workspace)
    commit_direct(env, ctx(), [payment("PAY_r1", kind=PAYMENT_REFUND,
                                       original_payment_id="PAY_p1", actual_amount_ils=5),
                               task("TAS_t2"), expense("EXP_e2", source_key="key-2")])
    commit_direct(env, ctx(), [update(current(env, "tasks", "TAS_t2"), title="Renamed")])
    commit_direct(env, ctx(), [tombstone("tasks", current(env, "tasks", "TAS_t2"))])
    proposals = [
        env.propose(ctx(), api.ProposeInput(rid(), "time", "TIM_t1", 1, {"minutes": 45})),
        env.propose(ctx(), api.ProposeInput(rid(), "leads", None, 0, {
            "name": "Other", "path": PATH_LEARN, "status": LEAD_NEW, "phone": "0547654321"})),
        env.propose(ctx(), api.ProposeInput(rid(), "promote_lead", "LEA_l1", 1, {})),
        env.propose(ctx(), api.ProposeInput(rid(), "expenses", None, 0, {
            k: v for k, v in expense("EXP_x", source_key="key-3").after.items()
            if k not in {"expense_id", "revision"}})),
    ]
    for proposal in proposals:
        result = env.decide(ctx(), proposal.proposal_id, proposal.approval_id, "approve")
        assert result.receipt.status == "committed", result.receipt
