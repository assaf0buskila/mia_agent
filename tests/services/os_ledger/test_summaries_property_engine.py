"""The differential engine behind the V2-18 property tests (§8, §20). It holds no test itself.

A ``Twin`` is two ledgers of one kind (fake or PG) driven by ONE seeded random sequence of owner
commits, undos and propose/decide rounds: the reference runs the full validator and the full
summary recompute; the system under test runs ``ScopedStateValidator`` and/or
``IncrementalSummaries``. After every step the engine asserts the same outcome on both sides
(accepted, or refused with the same code and reason) and the same state: every record row, the
version, ``pending_seq``, ``os_workspaces.summaries``, every ``os_summary_monthly`` row and the
``monthly_summary`` read. On PG both sides share one database (workspaces ``test`` and ``twin``).

The fake keeps its summaries in memory (``monthly_wire``), so the fake side reaches the shared
V2-18 logic through two test adapters, ``FakeScope`` and ``FakeMonthStore``, the fake's
counterparts of ``validate_scoped.PgScope`` and ``summaries_incremental.PgMonthStore``.
"""

from __future__ import annotations

import copy
import os
import random
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from app.prototypes.assaf_os_domain import ENUMS, validate_state
from app.prototypes.assaf_os_primary import LocalPrimaryError
from app.prototypes.assaf_os_summaries import SummaryInputError, build_summaries
from app.services.os_ledger import api

from tests.fakes.os_ledger import FakeLedger, _domain_error, _wire
from tests.fakes.os_ledger_builders import (
    CLIENT_ACTIVE,
    CLIENT_INACTIVE,
    EXPENSE_ACTUAL,
    EXPENSE_CANCELLED,
    EXPENSE_PAID,
    EXPENSE_UNPAID,
    EXPENSE_USAGE,
    PATH_AUTOMATION,
    PATH_LEARN,
    PAYMENT_CANCELLED,
    PAYMENT_RECEIVED,
    PAYMENT_REFUND,
    TIME_ACTIVE,
    TIME_CANCELLED,
    create,
    ctx,
    direct_draft,
    fresh_workspace,
    restore,
    tombstone,
    undo_draft,
    update,
)
from tests.fakes.os_ledger_env import NOW, WORKSPACE, Knobs, LedgerEnv

MAX_SAFE = 9_007_199_254_740_991
TWIN = "twin"
WINDOW = 30
RECORD_ENTITIES = ("clients", "works", "payments", "time", "expenses", "tasks")
DATES = ("2026-07-03", "2026-07-28", "2026-08-15", "2026-09-01", "2026-09-30", "2026-10-10")
PATHS = (PATH_LEARN, PATH_AUTOMATION)
WORK_STATUSES = ENUMS["work_status"]
TASK_STATUSES = ENUMS["task_status"]


def uuid_source(seed: int):
    """Identical v4 UUID sequences for both sides, so generated ids line up."""
    rng = random.Random(f"uuid-{seed}")
    return lambda: str(UUID(int=rng.getrandbits(128), version=4))


def month_of(value: date) -> date:
    return value.replace(day=1)


# --- the fake's adapters for the shared V2-18 logic ------------------------------------------


def _live_rows(tx, entity):
    return [r for r in tx.data["tables"]["os_" + entity].values() if r["deleted_at"] is None]


class FakeScope:
    """``validate_scoped.ScopeReader`` over the FakeLedger's in-memory tables."""

    def __init__(self, tx):
        self.tx = tx

    def referrers(self, entity, field_name, ids):
        wanted = set(ids)
        found = [_wire(r, entity) for r in _live_rows(self.tx, entity)]
        return sorted((w for w in found if w.get(field_name) in wanted),
                      key=lambda w: w[api.ID_KEYS[entity]])

    def duplicate_source(self, expenses):
        rows = _live_rows(self.tx, "expenses")
        return any(sum(1 for r in rows if (r["inputs"]["source_type"], r["inputs"]["source_key"])
                       == (e["source_type"], e["source_key"])) > 1 for e in expenses)


class FakeMonthStore:
    """``summaries_incremental.MonthStore`` over the fake's ``monthly_wire`` (v1 rows)."""

    def __init__(self, tx):
        self.tx = tx

    def magnitudes(self):
        money = sum(r["amount_minor"] for e in ("payments", "expenses")
                    for r in _live_rows(self.tx, e))
        return money, sum(r["minutes"] for r in _live_rows(self.tx, "time"))

    def work_months(self, work_ids):
        wanted = set(work_ids)
        return {month_of(r["business_date"]) for e in ("payments", "time")
                for r in _live_rows(self.tx, e) if r.get("work_id") in wanted}

    def records_in(self, months):
        wanted = set(months)
        return {e: sorted((_wire(r, e) for r in _live_rows(self.tx, e)
                           if month_of(r["business_date"]) in wanted),
                          key=lambda w: w[api.ID_KEYS[e]])
                for e in ("payments", "expenses", "time")}

    def stored(self, months):
        grouped: dict[date, list] = {}
        for row in self.tx.data["monthly_wire"]:
            grouped.setdefault(date.fromisoformat(row["month"] + "-01"), []).append(row)
        return {m: {"rows": copy.deepcopy(grouped[m])} for m in months if m in grouped}

    def write(self, month, content, version, now):
        key = month.isoformat()[:7]
        kept = [r for r in self.tx.data["monthly_wire"] if r["month"] != key]
        merged = kept + copy.deepcopy(list(content["rows"]))
        self.tx.data["monthly_wire"] = sorted(merged, key=lambda r: (r["month"], r["currency"]))

    def header(self, value):
        self.tx.data["summaries"] = dict(value)

    def full(self, applied):
        self.tx.data.setdefault("full_fallbacks", 0)
        self.tx.data["full_fallbacks"] += 1
        try:
            summary = build_summaries(self.tx._records())
        except SummaryInputError as exc:
            raise _domain_error(exc) from None
        self.tx.data["summaries"] = {"coverage": summary["coverage"],
                                     "calculated_version": self.tx.version + 1}
        self.tx.data["monthly_wire"] = summary["monthly"]


class FakeFullValidator:
    """The fake's own default check (prototype ``validate_state``), as a validator object."""

    def validate(self, tx, applied):
        try:
            validate_state(tx._records())
        except LocalPrimaryError as exc:
            raise _domain_error(exc) from None


class DualValidator:
    """Runs the full and the scoped validator on the SAME post-apply state and records both."""

    def __init__(self, scoped, full):
        self.scoped, self.full, self.calls = scoped, full, []

    def validate(self, tx, applied):
        outcomes = []
        for validator in (self.full, self.scoped):
            try:
                validator.validate(tx, applied)
                outcomes.append(None)
            except api.LedgerError as exc:
                outcomes.append((exc.code, exc.details.get("reason")))
        self.calls.append(tuple(outcomes))
        if outcomes[1] is not None:
            raise api.LedgerError(outcomes[1][0], details={"reason": outcomes[1][1]})


# --- the two sides ---------------------------------------------------------------------------


class Side:
    def __init__(self, ledger, workspace):
        self.ledger, self.workspace = ledger, workspace
        self.receipts: dict[str, api.ReceiptRow] = {}
        self.proposals: dict[str, api.ProposalRow] = {}

    def ctx(self):
        return ctx(workspace=self.workspace)

    def commit(self, mutations, request_id, *, kind="owner_direct", original=None):
        mutations = list(mutations)
        with self.ledger.transaction(self.ctx(), domain="core", kind=kind) as tx:
            if kind == "owner_direct":
                draft = direct_draft(tx, mutations, request_id, WINDOW)
            else:
                draft = undo_draft(tx, self.receipts[original], request_id, mutations, WINDOW,
                                   mutations[0].entity, mutations[0].record_id)
            proposal = tx.insert_proposal(draft)
            tx.apply(mutations, source="OS_DIRECT", request_id=request_id)
            receipt = tx.finish(proposal, status="committed", receipt_request_id=request_id,
                                applied=mutations, undo_of=original)
        self.receipts[request_id] = receipt
        return receipt.status, receipt.commit_version

    def propose(self, request_id, entity, record_id, expected, values):
        row = self.ledger.propose(self.ctx(), api.ProposeInput(
            request_id, entity, record_id, expected, values))
        self.proposals[request_id] = row
        return row.status, row.entity, row.record_id, row.expected_revision

    def decide(self, request_id, decision):
        row = self.proposals[request_id]
        decision_id = row.approval_id if decision == "approve" else row.rejection_id
        result = self.ledger.decide(self.ctx(), row.proposal_id, decision_id, decision)
        return result.receipt.status, result.receipt.reason_codes, result.receipt.commit_version


def attempt(call):
    try:
        return ("ok", call())
    except api.LedgerError as exc:
        return ("refused", exc.code, exc.details.get("reason"))


@dataclass
class Twin:
    kind: str
    ref: Side
    sut: Side
    tables: Any  # (table) -> (ref rows, sut rows), workspace_id dropped
    advance: Any
    stats: dict = field(default_factory=dict)

    def both(self, call):
        return attempt(lambda: call(self.ref)), attempt(lambda: call(self.sut))


def _drop_ws(rows):
    return [{k: v for k, v in r.items() if k != "workspace_id"} for r in rows]


def fake_twin(seed, *, validator=None, summaries=None) -> Twin:
    """``validator``/``summaries`` are the SUT's; the reference keeps the fake's full defaults."""
    sides = []
    for options in ({}, {"validator": validator, "summaries": summaries}):
        fake = FakeLedger(clock=lambda: NOW, uuid4=uuid_source(seed), settings_epoch=lambda: 1,
                          **options)
        env = LedgerEnv("fake", fake, Knobs(), rows=fake.rows, version=fake.version,
                        now=fake._now, advance=fake.advance)
        env.ensure_workspace(WORKSPACE, "123", 1)
        env.transition(WORKSPACE, "core", "active", expected_epoch=1, actor_owner_id="123")
        sides.append(Side(fake, WORKSPACE))

    def tables(table):
        return tuple(_drop_ws(side.ledger.rows(table)) for side in sides)

    def advance(seconds):
        for side in sides:
            side.ledger.advance(seconds)

    return Twin("fake", sides[0], sides[1], tables, advance)


def pg_twin(seed, *, validator=None, summaries=None) -> Twin:
    from app.services.os_ledger import core
    from app.services.os_ledger.summaries import FullSummaries
    from app.services.os_ledger.validate_full import FullStateValidator

    from tests.services.os_ledger.conftest import PG_URL_ENV, _prepare, build_pg_env

    env = _prepare(build_pg_env(os.environ[PG_URL_ENV]))
    fresh_workspace(env, TWIN, core="active")
    base = env.transaction.__self__
    ref = core.build_ledger(base.settings, clock=base.clock, uuid4=uuid_source(seed),
                            validator=FullStateValidator(), summaries=FullSummaries())
    sut = core.build_ledger(base.settings, clock=base.clock, uuid4=uuid_source(seed),
                            validator=validator or FullStateValidator(),
                            summaries=summaries or FullSummaries())

    def tables(table):
        rows = env.rows(table)
        return tuple(_drop_ws([r for r in rows if r["workspace_id"] == ws])
                     for ws in (WORKSPACE, TWIN))

    return Twin("pg", Side(ref, WORKSPACE), Side(sut, TWIN), tables, env.advance)


# --- state ------------------------------------------------------------------------------------


def model_of(rows_by_entity) -> dict[str, dict[str, tuple[dict, bool]]]:
    model = {}
    for entity, rows in rows_by_entity.items():
        model[entity] = {}
        for row in rows:
            wire = dict(row["inputs"]) | {api.ID_KEYS[entity]: row["record_id"],
                                          "revision": int(row["revision"])}
            model[entity][row["record_id"]] = (wire, row["deleted_at"] is not None)
    return model


def compare_state(twin: Twin, where: str) -> dict:
    """Assert equal rows and summaries on both sides; return the reference's record rows."""
    ref_rows = {}
    for entity in RECORD_ENTITIES:
        ref, sut = twin.tables("os_" + entity)
        assert ref == sut, f"{where}: os_{entity} differs"
        ref_rows[entity] = ref
    for table in ("os_summary_monthly", "os_summary_cycle"):
        ref, sut = twin.tables(table)
        assert ref == sut, f"{where}: {table} differs"
    ref_ws, sut_ws = twin.tables("os_workspaces")
    keys = ("version", "pending_seq", "summaries")
    assert [{k: r[k] for k in keys} for r in ref_ws] == [{k: r[k] for k in keys} for r in sut_ws], (
        f"{where}: os_workspaces differs")
    assert twin.ref.ledger.monthly_summary(twin.ref.ctx()) == twin.sut.ledger.monthly_summary(
        twin.sut.ctx()), f"{where}: monthly_summary differs"
    return ref_rows


# --- the random sequence ----------------------------------------------------------------------


class Generator:
    def __init__(self, rng: random.Random, steps: int, *, huge: bool):
        self.rng, self.steps, self.huge, self.n = rng, steps, huge, 0
        self.commits: list[str] = []
        self.undone: set[str] = set()

    def rid(self) -> str:
        return str(UUID(int=self.rng.getrandbits(128), version=4))

    def new_id(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}_g{self.n:04d}"

    def pick(self, items):
        items = sorted(items)
        return self.rng.choice(items) if items else None

    @staticmethod
    def live(model, entity):
        return {i: w for i, (w, dead) in model[entity].items() if not dead}

    # Creations. Each returns a list of mutations, or None when its prerequisites are missing.
    def client(self, model):
        return [create("clients", self.new_id("PER"), name=f"Client {self.n}",
                       status=self.rng.choice((CLIENT_ACTIVE, CLIENT_ACTIVE, CLIENT_INACTIVE)))]

    def work(self, model):
        person = self.pick(self.live(model, "clients"))
        if person is None:
            return self.client(model)
        values = {"person_id": person, "title": f"Work {self.n}",
                  "path": self.rng.choice(PATHS), "status": self.rng.choice(WORK_STATUSES)}
        currency = self.rng.choice(("ILS", "ILS", "ILS", "USD", None))
        if currency:
            values["currency"] = currency
        if self.rng.random() < 0.5:
            values["start_date"] = self.rng.choice(DATES)
        return [create("works", self.new_id("WOR"), **values)]

    def _work_refs(self, model):
        works = self.live(model, "works")
        work_id = self.pick(works)
        if work_id is None:
            return None
        work = works[work_id]
        person = work["person_id"]
        if self.rng.random() < 0.08:
            person = self.pick(self.live(model, "clients")) or person
        path = work["path"] if self.rng.random() < 0.95 else self.rng.choice(PATHS)
        return work_id, work, person, path

    def payment(self, model, *, amount=None):
        refs = self._work_refs(model)
        if refs is None:
            return self.work(model)
        work_id, work, person, path = refs
        currency = (work.get("currency") or "ILS") if self.rng.random() < 0.92 else "USD"
        return [create("payments", self.new_id("PAY"), person_id=person, work_id=work_id,
                       path=path, date=self.rng.choice(DATES),
                       actual_amount_ils=amount or self.rng.randint(1, 5000), currency=currency,
                       kind=PAYMENT_RECEIVED if self.rng.random() < 0.85 else PAYMENT_CANCELLED)]

    def refund(self, model):
        originals = {i: w for i, w in self.live(model, "payments").items()
                     if w.get("kind") == PAYMENT_RECEIVED}
        original_id = self.pick(originals)
        if original_id is None:
            return self.payment(model)
        o = originals[original_id]
        return [create("payments", self.new_id("PAY"), person_id=o["person_id"],
                       work_id=o["work_id"], path=o["path"], currency=o["currency"],
                       date=o["date"] if self.rng.random() < 0.8 else self.rng.choice(DATES),
                       actual_amount_ils=self.rng.randint(1, o["actual_amount_ils"] + 100),
                       kind=PAYMENT_REFUND, original_payment_id=original_id)]

    def time(self, model):
        refs = self._work_refs(model)
        if refs is None:
            return self.work(model)
        work_id, _, person, path = refs
        return [create("time", self.new_id("TIM"), person_id=person, work_id=work_id, path=path,
                       date=self.rng.choice(DATES), minutes=self.rng.randint(0, 240),
                       status=TIME_ACTIVE if self.rng.random() < 0.8 else TIME_CANCELLED)]

    def expense(self, model):
        return [create("expenses", self.new_id("EXP"), date=self.rng.choice(DATES),
                       category="Ads", platform=self.rng.choice(("Meta", "Google Ads")),
                       path=PATH_LEARN, amount_ils=self.rng.randint(1, 3000),
                       currency=self.rng.choice(("ILS", "ILS", "USD")),
                       payment_status=self.rng.choice((EXPENSE_PAID, EXPENSE_PAID,
                                                       EXPENSE_UNPAID, EXPENSE_CANCELLED)),
                       source_type=self.rng.choice((EXPENSE_ACTUAL, EXPENSE_ACTUAL,
                                                    EXPENSE_USAGE)),
                       source_key=f"k{self.rng.randint(0, 30)}")]

    def task(self, model):
        values = {"title": f"Task {self.n}", "status": self.rng.choice(TASK_STATUSES)}
        if self.rng.random() < 0.4:
            refs = self._work_refs(model)
            if refs:
                values.update(work_id=refs[0], person_id=refs[2])
        elif self.rng.random() < 0.5:
            person = self.pick(self.live(model, "clients"))
            if person:
                values["person_id"] = person
        if self.rng.random() < 0.6:
            values["due_date"] = self.rng.choice(DATES)
        return [create("tasks", self.new_id("TAS"), **values)]

    def change(self, model):
        entity = self.rng.choice(("clients", "works", "works", "payments", "payments", "time",
                                  "time", "expenses", "tasks"))
        live = self.live(model, entity)
        record_id = self.pick(live)
        if record_id is None:
            return None
        before = live[record_id]
        r = self.rng
        options = {
            "clients": [{"status": r.choice((CLIENT_ACTIVE, CLIENT_INACTIVE))},
                        {"name": f"Renamed {self.n}"}],
            "works": [{"currency": r.choice(("ILS", "USD", None))},
                      {"person_id": self.pick(self.live(model, "clients"))},
                      {"status": r.choice(WORK_STATUSES)}, {"start_date": r.choice(DATES)}],
            "payments": [{"actual_amount_ils": r.randint(1, 6000)}, {"date": r.choice(DATES)},
                         {"currency": r.choice(("ILS", "USD"))}]
            + ([{"kind": r.choice((PAYMENT_RECEIVED, PAYMENT_CANCELLED))}]
               if before.get("kind") != PAYMENT_REFUND else
               # §22 E20 now refuses tombstoning an original under a live refund (has_dependents)
               # before validation, so the refund side is re-pointed instead: to any payment, a
               # tombstoned or cancelled one, or none, keeping refund_original_not_found reachable.
               [{"original_payment_id": r.choice((*model["payments"], "PAY_none"))}]),
            "time": [{"minutes": r.randint(0, 300)}, {"date": r.choice(DATES)},
                     {"status": r.choice((TIME_ACTIVE, TIME_CANCELLED))},
                     {"work_id": self.pick(self.live(model, "works"))}],
            "expenses": [{"amount_ils": r.randint(1, 4000)}, {"date": r.choice(DATES)},
                         {"payment_status": r.choice((EXPENSE_PAID, EXPENSE_UNPAID))},
                         {"source_type": r.choice((EXPENSE_ACTUAL, EXPENSE_USAGE))}],
            "tasks": [{"status": r.choice(TASK_STATUSES)}, {"due_date": r.choice(DATES)}],
        }[entity]
        changes = r.choice(options)
        return [update(before, **changes)]

    def drop(self, model):
        entity = self.rng.choice(("clients", "clients", "works", "works", "works", "payments",
                                  "payments", "time", "expenses", "tasks"))
        live = self.live(model, entity)
        record_id = self.pick(live)
        if record_id is None:
            return None
        mutations = [tombstone(entity, live[record_id])]
        if self.rng.random() < 0.25:  # a bulk: the record's live referrers go with it
            refs = [(e, w) for e in ("works", "payments", "time", "tasks")
                    for w in self.live(model, e).values()
                    if w.get("person_id" if entity == "clients" else "work_id") == record_id
                    and entity in {"clients", "works"}]
            mutations += [tombstone(e, w) for e, w in refs[:4]]
        return mutations

    def revive(self, model):
        entity = self.rng.choice(RECORD_ENTITIES)
        dead = {i: w for i, (w, gone) in model[entity].items() if gone}
        record_id = self.pick(dead)
        return None if record_id is None else [restore(entity, dead[record_id])]

    def inverse(self, model, original: tuple[str, list[api.Mutation]]):
        _, mutations = original
        inverse = []
        for m in reversed(mutations):
            current, gone = model[m.entity][m.record_id]
            if m.before is None or m.op == "restore":
                if not gone:
                    inverse.append(tombstone(m.entity, current))
            elif m.op == "tombstone":
                if gone:
                    inverse.append(api.Mutation(m.entity, m.record_id, "restore", current, {
                        **dict(m.before), "revision": current["revision"] + 1}))
            elif not gone:
                inverse.append(api.Mutation(m.entity, m.record_id, "upsert", current, {
                    **dict(m.before), "revision": current["revision"] + 1}))
        return inverse

    def next(self, step: int, model, history) -> tuple:
        r = self.rng.random()
        late = step > self.steps * 0.7
        forced = step in {int(self.steps * 0.75), int(self.steps * 0.85)}  # never vacuous
        if self.huge and late and (r < 0.04 or forced):
            amount = self.rng.choice((MAX_SAFE // 60 + self.rng.randint(0, 9),
                                      MAX_SAFE - self.rng.randint(0, 9)))
            return ("commit", self.payment(model, amount=amount) or [])
        if r < 0.10 and history:
            pending = [rid for rid in self.commits if rid not in self.undone]
            choice = self.pick(pending)
            if choice is not None:
                mutations = self.inverse(model, history[choice])
                if mutations:
                    self.undone.add(choice)
                    return ("undo", choice, mutations)
        if r < 0.20:
            return self.proposal(model)
        makers = (self.client, self.work, self.work, self.payment, self.payment, self.payment,
                  self.refund, self.refund, self.time, self.time, self.time, self.expense,
                  self.task, self.change, self.change, self.change, self.change, self.drop,
                  self.drop, self.drop, self.revive)
        for _ in range(10):
            mutations = self.rng.choice(makers)(model)
            if mutations:
                if self.rng.random() < 0.15:
                    extra = self.rng.choice((self.payment, self.time, self.change))(model)
                    ids = {(m.entity, m.record_id) for m in mutations}
                    mutations += [m for m in extra or [] if (m.entity, m.record_id) not in ids]
                return ("commit", mutations)
        return ("commit", self.client(model))

    def proposal(self, model):
        then = self.rng.choice(("approve", "approve", "approve", "reject", "change", "expire"))
        live_time = self.live(model, "time")
        if live_time and self.rng.random() < 0.4:
            record_id = self.pick(live_time)
            record = live_time[record_id]
            return ("propose", "time", record_id, record["revision"],
                    {"minutes": self.rng.randint(0, 300)}, then)
        made = self.rng.choice((self.payment, self.time, self.task, self.expense))(model)
        m = (made or self.client(model))[0]
        values = {k: v for k, v in m.after.items() if k not in {api.ID_KEYS[m.entity],
                                                                  "revision"}}
        return ("propose", m.entity, None, 0, values, then)


def run(twin: Twin, seed: int, steps: int, *, huge: bool = True) -> dict:
    """Drive ``twin`` through ``steps`` random steps; assert both sides agree after each."""
    rng = random.Random(seed)
    gen = Generator(rng, steps, huge=huge)
    history: dict[str, tuple[str, list[api.Mutation]]] = {}
    stats = {"steps": 0, "accepted": 0, "refused": {}, "decided": {}}
    rows = compare_state(twin, f"seed {seed} start")
    for step in range(steps):
        model = model_of(rows)
        op = gen.next(step, model, history)
        where = f"seed {seed} step {step} op {op[0]} {_describe(op)}"
        twin.advance(rng.choice((1, 2, 5)))
        if op[0] == "commit" and op[1]:
            request_id = gen.rid()
            outcomes = twin.both(lambda side, m=op[1]: side.commit(m, request_id))
            if outcomes[0][0] == "ok":
                gen.commits.append(request_id)
                history[request_id] = (request_id, list(op[1]))
        elif op[0] == "undo":
            request_id = gen.rid()
            outcomes = twin.both(lambda side: side.commit(op[2], request_id, kind="owner_undo",
                                                          original=op[1]))
        elif op[0] == "propose":
            request_id = gen.rid()
            _, entity, record_id, expected, values, then = op
            outcomes = twin.both(lambda side: side.propose(request_id, entity, record_id,
                                                           expected, values))
            assert outcomes[0] == outcomes[1], f"{where}: propose {outcomes}"
            if outcomes[0][0] == "ok":
                if then == "change" and record_id is not None:
                    current = model[entity][record_id][0]
                    nudge = [update(current, minutes=(current["minutes"] + 1) % 300)]
                    change_id = gen.rid()
                    changed = twin.both(lambda side: side.commit(nudge, change_id))
                    assert changed[0] == changed[1], f"{where}: nudge {changed}"
                if then == "expire":
                    twin.advance(601)
                decision = "reject" if then == "reject" else "approve"
                outcomes = twin.both(lambda side: side.decide(request_id, decision))
                if outcomes[0][0] == "ok":
                    stats["decided"][outcomes[0][1][0]] = stats["decided"].get(
                        outcomes[0][1][0], 0) + 1
        else:
            continue
        assert outcomes[0] == outcomes[1], f"{where}: {outcomes[0]} != {outcomes[1]}"
        if outcomes[0][0] == "ok":
            stats["accepted"] += 1
        else:
            key = f"{outcomes[0][1]}:{outcomes[0][2]}"
            stats["refused"][key] = stats["refused"].get(key, 0) + 1
        stats["steps"] += 1
        rows = compare_state(twin, where)
    return stats


def _describe(op) -> str:
    if op[0] in {"commit", "undo"}:
        mutations = op[1] if op[0] == "commit" else op[2]
        return ",".join(f"{m.op}:{m.entity}:{m.record_id}" for m in mutations or [])
    return f"{op[1]}:{op[2]}:{op[5]}"
