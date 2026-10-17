# Bounded adapter and synthetic import-shadow milestone

Base: reviewed local `5fcce60abb44c1c770f0650dda3a134b2ea38cc0`.
Branch: `codex/os-primary-shadow-adapter-local`. Prior worktrees stay intact.
This is the next local milestone under the existing database-first architecture;
it does not authorize its production/import/mirror/authority activation gates.

## Objective and limits

Exercise the seven-entity primary transaction contract through a default-off,
server-context adapter, and exercise import staging/reconciliation with synthetic
source/accepted provenance. Commit success requires a fresh primary verification;
an inconclusive acknowledgement is recovered by the exact receipt. Mirror state
remains separate and pending. No Google or external exporter is implemented.

Use only cached dependencies and disposable, marked, loopback PostgreSQL. Do not
read `.env`, production data, real exports, credentials, or provider settings.
No live routes, app-startup registration, production migration, Google call,
paid resource, push, merge, deployment, authority switch, or external mirror delivery.
If the platform returns a usage limit, preserve work and report it; do not change
models or environments to evade it.

## Ownership and sequence

1. Root records this plan, checks the reviewed baseline and context contracts, and
   owns disposable test infrastructure, integration acceptance, benchmark, evidence,
   and checkpoint. Root resolves shared API choices before authors edit.
2. MID adapter author owns `app/prototypes/assaf_os_adapter.py` and its tests.
   Build an explicit disabled-by-default facade for snapshot/propose/decision/receipt,
   translating trusted server context into bound principals. No routes or real
   authentication are registered. Validate owner/workspace/epoch/scopes/policy at
   each entry, reject model approval, and return honest PostgreSQL/version/receipt
   metadata plus separate mirror state.
3. MID importer author owns `app/prototypes/assaf_os_shadow_import.py` and its tests.
   Stage bounded synthetic batches with immutable source/accepted provenance,
   checksums, ID/revision preservation, quarantine issues, duplicate-batch replay,
   and resumable atomic checkpoints. Reconcile all seven collections, references,
   per-currency money/refunds, input hashes, coverage, and deterministic summaries.
   Keep shadow data separate from approved business transactions and their receipts.
4. Root combines synthetic adapter/import flows, injects crash/lost-response/conflict
   and duplicate scenarios, and benchmarks a bounded synthetic workload if tools
   remain available. Do not claim a production/browser SLO.
5. Fresh HEAVY reviewer independently challenges boundaries, atomicity, provenance,
   idempotency, parity and evidence. Repair findings and rerun affected checks.
6. Record exact tests, skip limits, benchmark sample/workload bounds and commit;
   remove only this milestone's identity-verified disposable container.

## Acceptance gates

- Default off and no runtime imports/routes/migrations/provider effects.
- Numeric owner, workspace and authority epoch are server bound on every adapter
  operation; scopes and policy refuse prohibited decisions; only explicit owner
  approve/reject consumes immutable proposals.
- Both synthetic browser and Telegram paths return one exact receipt; lost commit
  acknowledgement stays inconclusive until recovery by that same decision ID.
- Synthetic staging has no live reads and creates no approval/financial receipt.
  IDs/revisions and original source/accepted metadata survive; invalid, duplicate,
  changed or uncertain provenance is visible and blocks acceptance.
- Duplicate batch replay is harmless; same batch ID with changed payload is refused;
  crash/rollback preserves checkpoints without half a batch; retry resumes safely.
- Seven-family counts, canonical hashes, references, refunds, dates and separated
  currency summaries reconcile, including missing/partial coverage and conflicts.
- Real disposable PostgreSQL proves transaction/race behavior; Node oracles retain
  financial parity. Required Ruff, widget, Node and Python checks pass with skips
  clearly identified. Independent reviewer returns PASS.

## Next decision gate

Stop this milestone at a reviewed local checkpoint. Real source export/import,
runtime session/Telegram/CSRF wiring, production schema and authority cutover,
backup/restore policy, mirror destination/permissions and external delivery require
their concrete later approvals and operational evidence. No destination, budget,
credential, native-edit policy or production authority choice is inferred here.
