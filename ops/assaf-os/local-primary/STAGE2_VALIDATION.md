# Adapter and synthetic import-shadow validation, 2026-10-14

Outcome: reviewed local milestone complete. Default-off primary adapter and synthetic
seven-entity staging/reconciliation are implemented, tested and independently reviewed.
No runtime activation or external effects occurred.

## Checkout and ownership

- Branch: `codex/os-primary-shadow-adapter-local`
- Checkout: `C:/Users/lenovo/Documents/Codex/2026-10-14/task/os-primary-shadow`
- Reviewed base: `5fcce60abb44c1c770f0650dda3a134b2ea38cc0`
- Plan written before implementation: `STAGE2_MILESTONE_PLAN.md`
- Root: integration fixtures/tests, benchmark, environment, documentation and checkpoint.
- Separate MID authors: adapter/contract tests and importer/reconciliation tests.
- Independent HEAVY reviewer: read-only security/acceptance probes and rereview.
- Model routing followed `AGENTS.md`; no platform usage-limit response was returned.
  No model/environment change was used to evade a limit.
- Prior seven-entity and original task worktrees remain intact and clean.

## Final checks

| Check | Result | Evidence |
|---|---|---|
| Frozen full Python suite | 3,349 passed, 111 skipped, 1,245 warnings, 158.70 seconds | `.cache/stage2-repo-final.log` |
| All prototype tests | 142 passed; included in final full suite | `.cache/stage2-prototypes-final.log` and full suite |
| Adapter focus | 26 passed | author focused run |
| Importer focus | 17 passed | author focused run |
| Combined Stage 2 flows | 7 passed | `.cache/stage2-combined-repaired.log` |
| Ruff, including benchmark | passed | `.cache/stage2-ruff-final.log` |
| Widget behavior | passed | `.cache/stage2-widget-final.log` |
| Existing runtime/Apps Script Node tests | 42 passed, no skips | `.cache/stage2-node-final.log` |
| Independent final review | PASS; no remaining blocking findings | findings/probe summary below |
| Final synthetic benchmark | 15 measured samples per operation, 3 warm-ups; zero failures | `STAGE2_BENCHMARK.json` |
| Git whitespace check | passed | terminal check |

The full suite's 111 separate opt-in/skipped tests are not passing evidence for those
integrations. Existing asyncio and Starlette/httpx deprecation warnings remain; no
dependency upgrade was made. Prototype coverage includes actual disposable PostgreSQL
transactions, not SQLite as concurrency evidence. The 142 prototype tests comprise
the 92 reviewed baseline tests plus 26 adapter, 17 importer and 7 combined tests.
Real PostgreSQL was available for every opt-in prototype integration case; none
was skipped in the final run. Pure domain, summary and guard checks are also part
of that 142-test group.

## Independent review and repairs

The initial review returned FAIL and withheld acceptance. Independent PostgreSQL
probes found persisted row/provenance/checkpoint integrity gaps, stale-batch epochs,
required-reference acceptance, malformed typed metadata, inherited business transaction
APIs, incomplete coverage presentation and insufficient benchmark success criteria.
All findings were repaired, covered by regressions and rerun.

Final independent verdict: PASS. The reviewer separately verified:

- changed/missing rows, provenance, headers and checkpoint data fail closed;
- stale epochs are refused and chunk/reconciliation writes hold workspace locks;
- a concurrent stage completion does not mix read-only shadow snapshots;
- promoted leads require clients while unpromoted identities can precede clients;
- invalid representable rows remain quarantined and inspectable;
- unknown source coverage stays unknown instead of becoming a complete empty summary;
- huge epochs, non-JSON/NUL/tuple inputs and excessive depth/nodes fail before DB checkout;
- business transaction methods are absent from the staging facade;
- owner/channel/scopes, context expiry, browser verification and exact decision IDs;
- lost acknowledgement recovery with the same receipt after mutation policy closes;
- exact proposal replay after an unchanged outer approval deadline expires;
- benchmark samples require accepted, issue-free manifests, correct counts and known totals.

Frozen SHA256 values match the reviewed implementation:

```text
assaf_os_adapter.py
0C48863F46A8D77BD380016269CCFEA9C105D619A639FFF2CF0A0D91906F3CCD
assaf_os_shadow_import.py
2285E6C089D9A0E3F18C2057BA4D84D5BAB684E78546DB7E6F288A90D0EC96FD
benchmark_assaf_os_local.py
6B1D153A94BAAD63569085B602E7557B2A0152E7504E89DE795B8C34C0C267AB
```

## Bounded benchmark

The final run started after test/review processes finished. A preliminary overlapping
smoke measurement is retained only in the ignored cache; the table below uses the
final run. All timings are wall-clock milliseconds. Write timings include proposal
creation, explicit synthetic approval, PostgreSQL commit and fresh receipt verification;
human approval think time is excluded. Shadow reconciliation checks acceptance, exact
counts and known ILS/EUR/refund/missing-time totals before recording success.

| Operation | n | p50 ms | p95 ms | p99 ms | max ms |
|---|---:|---:|---:|---:|---:|
| clients_propose_approve_verify | 15 | 67.581 | 401.682 | 401.682 | 401.682 |
| expenses_propose_approve_verify | 15 | 64.827 | 298.58 | 298.58 | 298.58 |
| leads_propose_approve_verify | 15 | 73.841 | 243.333 | 243.333 | 243.333 |
| payments_received_propose_approve_verify | 15 | 70.717 | 415.872 | 415.872 | 415.872 |
| payments_refund_propose_approve_verify | 15 | 66.608 | 289.895 | 289.895 | 289.895 |
| shadow_reconcile_12_rows | 15 | 17.427 | 56.883 | 56.883 | 56.883 |
| shadow_stage_12_rows | 15 | 50.958 | 485.556 | 485.556 | 485.556 |
| snapshot_first_page | 15 | 15.344 | 96.265 | 96.265 | 96.265 |
| tasks_propose_approve_verify | 15 | 65.348 | 500.923 | 500.923 | 500.923 |
| time_propose_approve_verify | 15 | 69.211 | 257.449 | 257.449 | 257.449 |
| works_propose_approve_verify | 15 | 65.995 | 152.334 | 152.334 | 152.334 |

Nearest-rank percentiles are used. With only 15 observations, p95 and p99 both equal
the observed maximum; these are small sample descriptions, not population guarantees.
The workload was sequential, with no injected concurrent load, a four-connection
pool, three warm-ups, and an already warm cached PostgreSQL 16.15 Docker instance
limited to one CPU and 512 MiB. Each iteration added eight synthetic ledger records
and staged one twelve-row shadow batch. Schema/environment setup and fixture
construction were excluded. Other Windows host activity was not controlled.

The authorization callback was synthetic and in-process. No browser, WAN, actual
session/Telegram/CSRF authentication, real source import, external mirror, production
VM contention or production SLO was measured. No four-second guarantee follows from
these numbers. The script requires explicit `--run-synthetic`, 5..30 samples,
`MIA_ENV=test`, and a disposable local PostgreSQL URL.

## Environment and cleanup

Only cached dependencies were used, with an ignored offline `.venv`. PostgreSQL
16.15 came from cached `postgres:16` (image ID `ca0bd484cb98`). The uniquely labelled
container was `assaf-os-stage2-local-20261014`, loopback port 56198, database
`assaf_os_local_stage2`, marker `disposable_only`. Fsync and synchronous commit
were observed on. No existing database or other container was modified. Cleanup
verifies this exact container ID/label and stops only its disposable synthetic storage.

Executor access remained on the selected Windows computer. One harmless read hit a
transient transport disconnect; a later same-computer check recovered. No alternative
environment or access bypass was used.

## Meaning and next decision gate

The adapter context is a synthetic server-contract input, not live authentication
proof. An accepted shadow manifest means a synthetic batch passed local validation;
it creates no business authority, human approval, receipt or outbox. No loader of real
source files is provided. The synthetic fixture preserves input provenance and IDs,
but does not constitute a complete production export of historical approval/receipt/
replay journals or raw Sheet/formula/header evidence.

This checkpoint is ready for later approved integration work. The next real-data gate
requires a bounded authorized source export/import preparation and resolution of its
provenance/coverage exceptions. Real owner session/Telegram/CSRF and policy wiring,
production schema/recovery/writer fencing and authority cutover remain separately
gated. Actual mirror delivery requires a concrete destination and permissions.
No push, merge, deploy, Google call, real export/import, authority switch, credential
change or paid resource occurred.
