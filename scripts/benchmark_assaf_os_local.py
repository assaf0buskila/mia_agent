"""Explicit bounded synthetic benchmark. No production or browser SLO claim."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from uuid import uuid4

# Direct execution from scripts must resolve this checkout, not a different install.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.prototypes.assaf_os_adapter import LocalPrimaryAdapter, TrustedOwnerContext  # noqa: E402
from app.prototypes.assaf_os_domain import ENTITIES, ENUMS, ID_KEYS  # noqa: E402
from app.prototypes.assaf_os_ledger import LocalLedgerPrimary  # noqa: E402
from app.prototypes.assaf_os_primary import LocalBinding, LocalPrincipal  # noqa: E402
from app.prototypes.assaf_os_shadow_import import LocalShadowImporter  # noqa: E402
from app.prototypes.assaf_os_synthetic import make_synthetic_batch  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402


def distribution(values):
    ordered = sorted(values)

    def percentile(p):
        return round(ordered[max(0, math.ceil(len(ordered) * p) - 1)], 3)

    return {
        "n": len(ordered),
        "p50_ms": percentile(0.5),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": round(ordered[-1], 3),
        "method": "nearest_rank",
    }


def run(samples):
    url = os.getenv("MIA_OS_LOCAL_PROTOTYPE_URL", "")
    if not url or os.getenv("MIA_ENV") != "test":
        raise ValueError("explicit_synthetic_test_environment_required")
    engine = create_engine(url, pool_size=4, max_overflow=0)
    binding = LocalBinding("benchmark_" + uuid4().hex, "12345")
    ledger = LocalLedgerPrimary(engine, binding, enabled=True)
    ledger.initialize_local_schema()
    importer = LocalShadowImporter(engine, binding, enabled=True)
    importer.initialize_local_schema()
    context = TrustedOwnerContext(
        numeric_owner=12345,
        workspace=binding.workspace_id,
        authority_epoch=1,
        channel="OS",
        scopes=frozenset({"assaf-os:read", "assaf-os:propose", "assaf-os:approve"}),
        auth_expires_at=datetime.now(UTC) + timedelta(hours=1),
        mutation_allowed=True,
        browser_mutation_verified=True,
    )
    adapter = LocalPrimaryAdapter(ledger, lambda: context, enabled=True)
    owner = LocalPrincipal("12345", "OS")
    durations, failures = defaultdict(list), []
    path = ENUMS["path"][0]
    warmup = 3

    def measured(name, operation, record):
        start = perf_counter()
        try:
            result = operation()
        except Exception as exc:
            failures.append({"operation": name, "code": getattr(exc, "code", "benchmark_failed")})
            raise
        if record:
            durations[name].append((perf_counter() - start) * 1000)
        return result

    def write(entity, inputs, label, record):
        def operation():
            proposal = adapter.propose(str(uuid4()), entity, inputs)
            reply = adapter.decide(
                proposal["proposal_id"],
                proposal["approval_id"],
                explicit_owner_decision=True,
            )
            if reply["receipt"]["status"] != "committed" or not reply["verified_primary"]:
                raise ValueError("synthetic_commit_not_verified")
            return reply["records"][0]["record"]

        return measured(label, operation, record)

    try:
        for index in range(warmup + samples):
            record = index >= warmup
            client = write(
                "clients",
                {
                    "name": "Synthetic benchmark client",
                    "status": ENUMS["client_status"][0],
                },
                "clients_propose_approve_verify",
                record,
            )
            write(
                "leads",
                {
                    "name": "Synthetic benchmark lead",
                    "path": path,
                    "status": ENUMS["lead_status"][0],
                },
                "leads_propose_approve_verify",
                record,
            )
            work = write(
                "works",
                {
                    "person_id": client["person_id"],
                    "title": "Synthetic benchmark work",
                    "path": path,
                    "status": ENUMS["work_status"][1],
                    "currency": "ILS",
                    "agreed_amount_ils": 99999,
                },
                "works_propose_approve_verify",
                record,
            )
            refs = {"person_id": client["person_id"], "work_id": work["work_id"], "path": path}
            payment = write(
                "payments",
                refs
                | {
                    "date": "2026-10-02",
                    "actual_amount_ils": 12345,
                    "currency": "ILS",
                    "kind": ENUMS["payment_kind"][0],
                },
                "payments_received_propose_approve_verify",
                record,
            )
            write(
                "payments",
                refs
                | {
                    "date": "2026-10-03",
                    "actual_amount_ils": 2345,
                    "currency": "ILS",
                    "kind": ENUMS["payment_kind"][1],
                    "original_payment_id": payment["payment_id"],
                },
                "payments_refund_propose_approve_verify",
                record,
            )
            write(
                "time",
                refs
                | {
                    "date": "2026-10-02",
                    "minutes": 90,
                    "status": ENUMS["time_status"][0],
                },
                "time_propose_approve_verify",
                record,
            )
            write(
                "expenses",
                {
                    "date": "2026-10-02",
                    "category": "Synthetic benchmark expense",
                    "platform": "Meta",
                    "path": path,
                    "amount_ils": 1500,
                    "currency": "ILS",
                    "payment_status": ENUMS["payment_status"][0],
                    "source_type": ENUMS["expense_source"][0],
                    "source_key": "synthetic:benchmark:" + str(uuid4()),
                },
                "expenses_propose_approve_verify",
                record,
            )
            write(
                "tasks",
                refs
                | {
                    "title": "Synthetic benchmark task",
                    "status": ENUMS["task_status"][0],
                    "due_date": "2026-10-14",
                },
                "tasks_propose_approve_verify",
                record,
            )
            measured("snapshot_first_page", adapter.snapshot, record)
            batch = make_synthetic_batch(binding.workspace_id)
            measured("shadow_stage_12_rows", lambda: importer.stage(owner, batch), record)

            def reconcile_verified():
                manifest = importer.reconcile(owner, batch["batch_id"])
                if (
                    manifest["status"] != "accepted"
                    or manifest["issue_count"] != 0
                    or manifest["counts"] != {e: len(batch["records"][e]) for e in ENTITIES}
                ):
                    raise ValueError("synthetic_reconciliation_not_verified")
                totals = {r["currency"]: r for r in manifest["summaries"]["totals"]}
                if (
                    totals["ILS"]["paid_income_ils"] != 10000
                    or totals["ILS"]["paid_expenses_ils"] != 1500
                    or totals["ILS"]["net_cash_ils"] != 8500
                    or totals["EUR"]["paid_income_ils"] != 5000
                    or totals["EUR"]["effective_hourly_ils"] is not None
                ):
                    raise ValueError("synthetic_reconciliation_money_mismatch")
                return manifest

            measured(
                "shadow_reconcile_12_rows",
                reconcile_verified,
                record,
            )
            restored = importer.read_shadow(owner, batch["batch_id"])
            for entity in ENTITIES:
                key = ID_KEYS[entity]
                if sorted(restored[entity], key=lambda r: r[key]) != sorted(
                    batch["records"][entity], key=lambda r: r[key]
                ):
                    raise ValueError("synthetic_shadow_mismatch")
        return {
            "mode": "synthetic_local_only",
            "samples_per_operation": samples,
            "warmup_per_operation": warmup,
            "timing": "wall-clock; propose+approve+freshverify; human think time excluded",
            "operations": {
                name: distribution(values) for name, values in sorted(durations.items())
            },
            "failures": failures,
            "failure_count": len(failures),
            "limits": [
                "loopback PostgreSQL 16.15 in cached Docker; one CPU and 512 MiB cap",
                "sequential synthetic operations; 4 connection pool; no concurrent load",
                "synthetic context callback; no browser, WAN or real authentication latency",
                "schema/environment setup and fixture construction excluded; database already warm",
                "small samples; p95/p99 often equal max; no production SLO inference",
                "ledger grows by 8 records per iteration; each shadow batch 12 rows; no mirror",
            ],
        }
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-synthetic", action="store_true")
    parser.add_argument("--samples", type=int, default=15)
    args = parser.parse_args()
    if not args.run_synthetic or not 5 <= args.samples <= 30:
        parser.error("--run-synthetic and 5..30 samples required")
    result = run(args.samples)
    target = ROOT / ".cache" / "stage2-benchmark.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
