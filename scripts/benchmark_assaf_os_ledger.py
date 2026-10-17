"""V2-18 §8 budget benchmark: ledger page and write p95 at 20k records, under a concurrent pg_dump.

Method (recorded in the JSON it prints):
* a DISPOSABLE database is created on the given server (a resource-limited container, e.g.
  ``docker run --cpus 0.5 --memory 1g``) and dropped at the end;
* the ledger schema, roles and grants are set up exactly as the conformance PG fixture does
  (``tests/services/os_ledger/conftest.build_pg_env``), and every measured call runs as the
  runtime role ``assaf_os_ledger`` through ``core.build_ledger()`` (the V2-18 defaults);
* ``--records`` valid records (the entity mix below) are seeded with owner-role SQL, with their
  ``os_summary_monthly`` rows from ``build_summaries``; the verifier then checks the seed;
* reads: the first page and cursor pages of every entity, ``deleted``, ``pending``, ``head`` and
  ``monthly_summary``; writes: owner-direct commits (a task, a payment) and approvals of pending
  proposals; plus a few V2-16 ``propose`` calls and full-default commits for comparison;
* ``docker exec <container> pg_dump -U postgres <db> > /dev/null`` runs in a loop throughout.
Times are wall-clock per ledger call from this process (the app side is not CPU-limited).

The v2 read routes (V2-19/V2-20) are not on this base, so this measures the ledger read and write
paths directly, not "through the real FastAPI and auth stack" (§8); that run is a follow-up.

Usage: ``uv run python scripts/benchmark_assaf_os_ledger.py --server
postgresql+psycopg://postgres@127.0.0.1:56239/postgres --container assaf-os-v218-bench-pg``
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
from datetime import date, timedelta
from pathlib import Path
from time import perf_counter
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MIA_ENV", "test")

from app.services.os_ledger import api, core, verifier  # noqa: E402
from app.services.os_ledger.domain import ENUMS, canonical_input, validate_state  # noqa: E402
from app.services.os_ledger.ids import DATE_KEYS, dependency_digest  # noqa: E402
from app.services.os_ledger.summaries import FullSummaries, build_summaries  # noqa: E402
from app.services.os_ledger.tx import TYPED_COLUMNS, jsonb  # noqa: E402
from app.services.os_ledger.validate_full import FullStateValidator  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

# §8 budgets. The page budget is the issue's "15-50 ms of server time" upper end; the write
# budget is the one-tap 0.3-0.8 s target's upper end. A failure keeps an explicit raised cap
# (RAISED) and is reported as a fork; it never silently passes.
BUDGETS_MS = {"page_p95": 50.0, "write_p95": 800.0}
# Raised after the 2026-10-18 run (0.5 cpu / 1 GB, 20k records, concurrent pg_dump): page p95
# 91 ms (p50 12-17 ms; the tail is CPU contention with pg_dump), write p95 ~1.0 s, of which the
# full-state load and validate_transition in V2-16's ``apply`` are most (``write_breakdown``).
RAISED_CAPS_MS = {"page_p95": 100.0, "write_p95": 1200.0}
MIX = {"leads": 0.10, "clients": 0.10, "works": 0.10, "payments": 0.25, "time": 0.225,
       "expenses": 0.125, "tasks": 0.10}
_CLAIM_FIELDS = ("person_id", "work_id", "original_payment_id", "currency", "source_type",
                 "source_key", "minutes")
P_RECEIVED, P_REFUND, P_CANCELLED = ENUMS["payment_kind"]


def distribution(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def percentile(p: float) -> float:
        return round(ordered[max(0, math.ceil(len(ordered) * p) - 1)], 2)

    return {"n": len(ordered), "p50_ms": percentile(0.5), "p95_ms": percentile(0.95),
            "max_ms": round(ordered[-1], 2)}


def timed(samples: list[float], call):
    started = perf_counter()
    result = call()
    samples.append((perf_counter() - started) * 1000)
    return result


# --- the synthetic workspace ------------------------------------------------------------------


def synthesize(total: int, seed: int) -> dict[str, list[dict]]:
    rng = random.Random(seed)
    counts = {entity: int(total * share) for entity, share in MIX.items()}
    counts["payments"] += total - sum(counts.values())
    months = [date(2024 + m // 12, m % 12 + 1, 1) for m in range(24)]

    def day() -> str:
        return (rng.choice(months) + timedelta(days=rng.randint(0, 27))).isoformat()

    def make(entity: str, record_id: str, raw: dict, **system) -> dict:
        return canonical_input(entity, raw) | {api.ID_KEYS[entity]: record_id, "revision": 1,
                                               **system}

    records: dict[str, list[dict]] = {entity: [] for entity in api.ENTITIES}
    for i in range(counts["clients"]):
        records["clients"].append(make("clients", f"PER_c{i:06d}", {
            "name": f"Client {i}", "status": ENUMS["client_status"][i % 7 == 0]}))
    for i in range(counts["leads"]):
        records["leads"].append(make("leads", f"LEA_l{i:06d}", {
            "name": f"Lead {i}", "phone": f"05{i:08d}", "path": ENUMS["path"][i % 2],
            "status": ENUMS["lead_status"][i % 4]}, person_id=f"PER_l{i:06d}"))
    for i in range(counts["works"]):
        client = records["clients"][i % len(records["clients"])]
        records["works"].append(make("works", f"WOR_w{i:06d}", {
            "person_id": client["person_id"], "title": f"Work {i}", "path": ENUMS["path"][i % 2],
            "status": ENUMS["work_status"][i % 3], "currency": "USD" if i % 10 == 0 else "ILS",
            "start_date": day()}))
    works = records["works"]
    received: list[tuple[dict, int]] = []
    for i in range(counts["payments"]):
        if received and i % 10 == 0:
            index = rng.randrange(len(received))
            original, left = received[index]
            if left > 1:
                amount = rng.randint(1, left // 2)
                received[index] = (original, left - amount)
                records["payments"].append(make("payments", f"PAY_p{i:06d}", {
                    "person_id": original["person_id"], "work_id": original["work_id"],
                    "path": original["path"], "date": original["date"],
                    "actual_amount_ils": amount, "currency": original["currency"],
                    "kind": P_REFUND, "original_payment_id": original["payment_id"]}))
                continue
        work = rng.choice(works)
        record = make("payments", f"PAY_p{i:06d}", {
            "person_id": work["person_id"], "work_id": work["work_id"], "path": work["path"],
            "date": day(), "actual_amount_ils": rng.randint(100, 50_000),
            "currency": work["currency"], "kind": P_RECEIVED if i % 9 else P_CANCELLED})
        records["payments"].append(record)
        if record["kind"] == P_RECEIVED:
            received.append((record, record["actual_amount_ils"]))
    for i in range(counts["time"]):
        work = rng.choice(works)
        records["time"].append(make("time", f"TIM_t{i:06d}", {
            "person_id": work["person_id"], "work_id": work["work_id"], "path": work["path"],
            "date": day(), "minutes": rng.randint(15, 240),
            "status": ENUMS["time_status"][i % 10 == 0]}))
    for i in range(counts["expenses"]):
        records["expenses"].append(make("expenses", f"EXP_e{i:06d}", {
            "date": day(), "category": "Ads", "platform": "Meta", "path": ENUMS["path"][0],
            "amount_ils": rng.randint(100, 20_000), "currency": "ILS",
            "payment_status": ENUMS["payment_status"][i % 3 == 0],
            "source_type": ENUMS["expense_source"][i % 4 == 0], "source_key": f"bench-{i}"}))
    for i in range(total - sum(len(v) for v in records.values())):
        raw = {"title": f"Task {i}", "status": ENUMS["task_status"][i % 5]}
        if i % 2:
            work = rng.choice(works)
            raw.update(person_id=work["person_id"], work_id=work["work_id"])
        if i % 3:
            raw["due_date"] = day()
        records["tasks"].append(make("tasks", f"TAS_t{i:06d}", raw))
    for entity in api.ENTITIES:
        records[entity].sort(key=lambda r, e=entity: r[api.ID_KEYS[e]])
    return records


def row_of(entity: str, record: dict, now) -> dict:
    """The stored row of a wire record, mapped exactly as ``PgLedgerTx._apply_one`` maps it."""
    system = {api.ID_KEYS[entity], "revision"} | ({"person_id"} if entity == "leads" else set())
    inputs = {k: v for k, v in record.items() if k not in system}
    raw_date = inputs.get(DATE_KEYS.get(entity))
    business_date = date.fromisoformat(raw_date) if raw_date else None
    typed = {c: record.get(c) for c in TYPED_COLUMNS[entity] if c in _CLAIM_FIELDS}
    if entity in {"payments", "expenses"}:
        typed["amount_minor"] = inputs["actual_amount_ils" if entity == "payments"
                                       else "amount_ils"]
    if entity == "works":
        typed["agreed_amount_minor"] = inputs.get("agreed_amount_ils")
    if entity == "tasks":
        typed["due_date"] = business_date
    if entity == "expenses":
        typed["source_digest"] = api.digest({"source_type": inputs["source_type"],
                                             "source_key": inputs["source_key"]})
    return {"id": record[api.ID_KEYS[entity]], "inputs": jsonb(inputs), "bd": business_date,
            "now": now, **{"t_" + c: v for c, v in typed.items()}}


def seed_sql(admin, workspace: str, records: dict[str, list[dict]], now) -> None:
    with admin.begin() as conn:
        for entity in ("clients", "leads", "works", "payments", "time", "expenses", "tasks"):
            typed = TYPED_COLUMNS[entity]
            sql = text(
                f"INSERT INTO assaf_os.os_{entity} (workspace_id, record_id, revision, inputs, "
                "business_date, source, created_at, updated_at"
                + "".join(", " + c for c in typed) + ") VALUES (:ws, :id, 1, CAST(:inputs AS "
                "jsonb), :bd, 'OS', :now, :now" + "".join(", :t_" + c for c in typed) + ")")
            rows = [row_of(entity, r, now) | {"ws": workspace} for r in records[entity]]
            if entity == "payments":  # an original before its refunds (the self FK)
                rows.sort(key=lambda r: r["t_original_payment_id"] is not None)
            for start in range(0, len(rows), 2000):
                conn.execute(sql, rows[start:start + 2000])
        summary = build_summaries(records)
        months: dict[str, list] = {}
        for row in summary["monthly"]:
            months.setdefault(row["month"], []).append(row)
        if months:
            conn.execute(text(
                "INSERT INTO assaf_os.os_summary_monthly (workspace_id, month_start, version, "
                "summary, calculated_at) VALUES (:ws, :m, 1, CAST(:s AS jsonb), :now)"),
                [{"ws": workspace, "m": date.fromisoformat(m + "-01"),
                  "s": jsonb({"rows": rows}), "now": now} for m, rows in months.items()])
        conn.execute(text(
            "UPDATE assaf_os.os_workspaces SET version = 1, summaries = CAST(:h AS jsonb) "
            "WHERE workspace_id = :ws"),
            {"ws": workspace, "h": jsonb({"coverage": "complete", "calculated_version": 1})})
        conn.execute(text(
            "UPDATE assaf_os.os_tasks SET deleted_at = :now, deleted_by_request_id = "
            "CAST(:rid AS uuid) WHERE workspace_id = :ws AND record_id IN "
            "(SELECT record_id FROM assaf_os.os_tasks WHERE workspace_id = :ws "
            "ORDER BY record_id LIMIT 60)"),
            {"ws": workspace, "now": now, "rid": "00000000-0000-4000-8000-000000000001"})
        conn.execute(text("ANALYZE"))


# --- the run ----------------------------------------------------------------------------------


class DumpLoop(threading.Thread):
    def __init__(self, container: str, database: str) -> None:
        super().__init__(daemon=True)
        self.command = ["docker", "exec", container, "pg_dump", "-U", "postgres", database]
        self.stop, self.completed, self.failed = threading.Event(), 0, 0

    def run(self) -> None:
        while not self.stop.is_set():
            done = subprocess.run(self.command, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, check=False)
            if done.returncode == 0:
                self.completed += 1
            else:
                self.failed += 1


def container_limits(container: str) -> str:
    out = subprocess.run(["docker", "inspect", container, "--format",
                          "{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}"],
                         capture_output=True, text=True, check=False).stdout.split()
    if len(out) != 2:
        return "unknown"
    return f"{int(out[0]) / 1e9:g} cpu / {int(out[1]) / 2**30:g} GB"


def run(args: argparse.Namespace) -> dict:
    from tests.fakes.os_ledger_builders import ctx, direct_draft
    from tests.services.os_ledger.conftest import _prepare, build_pg_env

    server = make_url(args.server)
    database = f"v218_bench_{os.getpid()}"
    assert re.fullmatch(r"[a-z0-9_]+", database)
    root = create_engine(server.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with root.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {database}"))
    url = server.set(database=database).render_as_string(hide_password=False)
    env = None
    try:
        env = _prepare(build_pg_env(url))
        ledger = env.transaction.__self__
        workspace = "test"
        admin = create_engine(url)
        records = synthesize(args.records, args.seed)
        validate_state(records)  # the seed is a valid state
        started = perf_counter()
        seed_sql(admin, workspace, records, env.now())
        seed_ms = (perf_counter() - started) * 1000
        report = timed([], lambda: verifier.verify(ledger.settings))
        assert report.ok, report
        verify_ms = []
        timed(verify_ms, lambda: verifier.verify(ledger.settings))
        context = ctx()
        rng = random.Random(args.seed + 1)
        works = records["works"]

        # pending proposals to list and to approve (task and payment creates)
        approvals = []
        with ledger.transaction(context, domain="core", kind="proposal") as tx:
            for i in range(args.samples * 2 + 40):
                if i % 2:
                    work = rng.choice(works)
                    m = api.Mutation("payments", f"PAY_q{i:05d}", "upsert", None, canonical_input(
                        "payments", {"person_id": work["person_id"], "work_id": work["work_id"],
                                     "path": work["path"], "date": "2025-06-15",
                                     "actual_amount_ils": 1234, "currency": work["currency"],
                                     "kind": P_RECEIVED}) | {"payment_id": f"PAY_q{i:05d}",
                                                             "revision": 1})
                else:
                    m = api.Mutation("tasks", f"TAS_q{i:05d}", "upsert", None, {
                        "title": f"Queued {i}", "status": ENUMS["task_status"][0],
                        "task_id": f"TAS_q{i:05d}", "revision": 1})
                draft = api.ProposalDraft(
                    "proposal", "OS", str(UUID(int=rng.getrandbits(128), version=4)),
                    api.digest({"bench": i}), m.entity, m.record_id, 0, (m,),
                    dependency_digest(records, (m,)), tx.now + timedelta(minutes=9))
                approvals.append(tx.insert_proposal(draft))

        breakdown: dict[str, list[float]] = {"snapshot_records": [], "validate_state": []}
        for _ in range(5):  # the O(N) part of every apply that V2-16's tx.py still runs
            with ledger.transaction(context, domain="core", kind="owner_direct") as tx:
                loaded = timed(breakdown["snapshot_records"], tx.snapshot_records)
            timed(breakdown["validate_state"], lambda r=loaded: validate_state(r))
        dumps = DumpLoop(args.container, database)
        if args.dump:
            dumps.start()
        pages: dict[str, list[float]] = {}
        try:
            for _ in range(args.samples):
                for entity in api.ENTITIES:
                    samples = pages.setdefault(entity, [])
                    page = timed(samples, lambda e=entity: ledger.page(
                        context, e, after=None, limit=50))
                    for _ in range(3):  # cursor pages deeper into the listing
                        if page.next_cursor is None:
                            break
                        page = timed(samples, lambda e=entity, c=page.next_cursor: ledger.page(
                            context, e, after=c, limit=50))
                timed(pages.setdefault("deleted", []), lambda: ledger.deleted(
                    context, "tasks", after=None, limit=50))
                timed(pages.setdefault("pending", []), lambda: ledger.pending(
                    context, after=None, limit=50))
                timed(pages.setdefault("head", []), lambda: ledger.head(context))
                timed(pages.setdefault("monthly_summary", []),
                      lambda: ledger.monthly_summary(context))
            writes: dict[str, list[float]] = {"commit_task": [], "commit_payment": [],
                                              "approve": []}

            def commit(target, mutation, samples):
                request_id = str(UUID(int=rng.getrandbits(128), version=4))

                def call():
                    with target.transaction(context, domain="core", kind="owner_direct") as tx:
                        proposal = tx.insert_proposal(direct_draft(tx, [mutation], request_id,
                                                                   30))
                        tx.apply([mutation], source="OS_DIRECT", request_id=request_id)
                        return tx.finish(proposal, status="committed",
                                         receipt_request_id=request_id, applied=[mutation])

                receipt = timed(samples, call)
                assert receipt.status == "committed"

            def payment(i):
                work = rng.choice(works)
                return api.Mutation("payments", f"PAY_b{i:05d}", "upsert", None, canonical_input(
                    "payments", {"person_id": work["person_id"], "work_id": work["work_id"],
                                 "path": work["path"], "date": "2025-03-10",
                                 "actual_amount_ils": 500 + i, "currency": work["currency"],
                                 "kind": P_RECEIVED}) | {"payment_id": f"PAY_b{i:05d}",
                                                         "revision": 1})

            for i in range(args.samples):
                commit(ledger, api.Mutation("tasks", f"TAS_b{i:05d}", "upsert", None, {
                    "title": f"Bench {i}", "status": ENUMS["task_status"][0],
                    "task_id": f"TAS_b{i:05d}", "revision": 1}), writes["commit_task"])
                commit(ledger, payment(i), writes["commit_payment"])
                proposal = approvals[i]
                result = timed(writes["approve"], lambda p=proposal: ledger.decide(
                    context, p.proposal_id, p.approval_id, "approve"))
                assert result.receipt.status == "committed", result.receipt.status
            compare: dict[str, list[float]] = {"propose_v2_16": [], "commit_payment_full": []}
            full = core.build_ledger(ledger.settings, clock=ledger.clock,
                                     validator=FullStateValidator(), summaries=FullSummaries())
            for i in range(args.compare_samples):
                timed(compare["propose_v2_16"], lambda n=i: ledger.propose(
                    context, api.ProposeInput(str(UUID(int=rng.getrandbits(128), version=4)),
                                              "tasks", None, 0, {
                                                  "title": f"Proposed {n}",
                                                  "status": ENUMS["task_status"][0]})))
                commit(full, payment(10_000 + i), compare["commit_payment_full"])
        finally:
            dumps.stop.set()
            if args.dump:
                dumps.join(timeout=600)
        after = verifier.verify(ledger.settings)
        page_all = [v for k, values in pages.items() if k in api.ENTITIES for v in values]
        write_all = [v for values in writes.values() for v in values]
        result = {
            "records": sum(len(v) for v in records.values()),
            "mix": {entity: len(records[entity]) for entity in api.ENTITIES},
            "pg_limits": container_limits(args.container),
            "concurrent_pg_dump": {"enabled": args.dump, "completed": dumps.completed,
                                   "failed": dumps.failed},
            "write_breakdown": {name: distribution(v) for name, v in breakdown.items()},
            "seed_sql_ms": round(seed_ms, 1),
            "verifier_full_recompute": distribution(verify_ms),
            "verifier_after_run_ok": after.ok,
            "page": {name: distribution(values) for name, values in pages.items()},
            "page_p95_ms": distribution(page_all)["p95_ms"],
            "write": {name: distribution(values) for name, values in writes.items()},
            "write_p95_ms": distribution(write_all)["p95_ms"],
            "compare": {name: distribution(values) for name, values in compare.items() if values},
            "budgets_ms": BUDGETS_MS, "raised_caps_ms": RAISED_CAPS_MS,
        }
        result["within_caps"] = (result["page_p95_ms"] <= RAISED_CAPS_MS["page_p95"]
                                 and result["write_p95_ms"] <= RAISED_CAPS_MS["write_p95"])
        admin.dispose()
        return result
    finally:
        if env is not None:
            from tests.services.os_ledger.conftest import build_pg_env as fixture

            setup = fixture.__dict__.get("state", {}).get(url)
            if setup is not None:
                setup["finish"]()
        core.dispose_engines()
        with root.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {database} WITH (FORCE)"))
        root.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", required=True,
                        help="a superuser URL of a DISPOSABLE loopback server")
    parser.add_argument("--container", required=True, help="the server's docker container")
    parser.add_argument("--records", type=int, default=20_000)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--compare-samples", type=int, default=3)
    parser.add_argument("--seed", type=int, default=18)
    parser.add_argument("--out", default="")
    parser.add_argument("--no-dump", dest="dump", action="store_false",
                        help="attribution only: skip the concurrent pg_dump (not a budget run)")
    args = parser.parse_args(argv)
    result = run(args)
    text_out = json.dumps(result, indent=2, sort_keys=True)
    print(text_out)
    if args.out:
        Path(args.out).write_text(text_out + "\n", encoding="utf-8")
    return 0 if result["within_caps"] else 1


if __name__ == "__main__":
    sys.exit(main())
