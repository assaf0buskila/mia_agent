# Mia v2 release plan

Updated 2026-09-11. Resumed from HANDOFF.md. User requested fast execution with
Luna and completion of the already-authorized implementation, cleanup, review and
deployment. HEAVY retains planning, security decisions and independent acceptance.
Preserve the existing dirty tree and immutable `.cache/mia-v2-baseline/` inventory.

## Ordered gates

- [x] Read operating contract, handoff, product context and current branch.
- [x] Verify local Docker PostgreSQL, AWS account and GitHub authentication.
- [x] Luna: repair kill-switch CRM callback, delivery fairness, whole-input contact
  consent and unsupported widget response promise; add behavioral regressions.
- [x] Luna: port remaining obsolete tests to v2 contracts and repair fixtures;
  document retired behavior and retained coverage without weakening invariants.
- [x] Run full pytest, isolated PostgreSQL migration/concurrency, Ruff, widget
  behavior/origin, browser appearance and real API integration, startup/container.
- [x] Obtain fresh independent HEAVY review of repairs and final release state.
- [x] Replace stale documentation and reconcile cleanup against the initial baseline.
- [ ] Commit reviewed release, push branch/PR and pass CI for the exact commit SHA.
- [ ] Build, scan and push image; verify immutable digest and matching SHA provenance.
- [ ] Pause the two schedulers before registering a candidate: both currently
  select the latest family revision. Keep reconcile disabled; migrate, deploy,
  then enable owner due scan pinned to the accepted revision.
- [ ] Verify serving task/digest/SHA, schema, health, routes, providers and workers.
- [ ] Record deployment evidence and give Assaf the live acceptance checklist.

## Evidence and boundaries

Final local gates: **1,991 passed / zero failures, errors or skips**, 104.6s.
Fresh independent HEAVY review passed with no unresolved findings. Ruff, widget
behavior/origin, four viewport visual checks, real API/widget session/message/
capture/reload checks, and settled-source nonroot container startup passed.
All 45 migrations are present in isolated PostgreSQL; repeat applies nothing.
Evidence: `.cache/mia-v2-release/final.xml`, `final.txt`, `postgres.xml`,
`local-migration.json`, `widget-*`, `container-final.json`, `cleanup-inventory.json`.

Cleanup: 179 deletions = 66 preexisting user deletions + 113 implementation
cleanup deletions. The immutable 93-path initial baseline remains intact.
Docker `mia-v2-release-pg` is running on localhost:51403 with isolated database
`mia_v2`. All local tests use `MIA_ENV=test`, fake external adapters and distinct
workspace pytest basetemp directories. Never read `.env` or production secrets.

AWS account 535252061205 and GitHub authentication were verified on this resumed
run. Fresh production serving/rollback evidence is task mia:57, digest
`sha256:78f25971a5a891f05ef745d5b0055cb8d723cc2fc14a5561cbcfad153894ef54`,
commit `58b40b341c992508349e23cdc55d3cc5f456c41b`. The due scan runs every 15 minutes
and reconcile hourly; both currently target unrevisioned family `mia`.

The old `.pytest_tmp_migrate_crm/` ACL issue remains excluded from Git and Docker;
do not alter ACLs or delete it. Preserve baseline/evidence caches. No v2 commit or
deployment has occurred yet. Live Telegram/browser/Sheets acceptance follows
deployment and belongs to the user.

## User live acceptance

Telegram conversation; Hebrew voice/image and captions; exact approve/reject,
expired/changed target and repeated callbacks; explicit memories only. Website
exploration/pricing/value, volunteered follow-up contact, continued conversation
and reload history. Confirm Telegram lead brief and Contacts/Activity records,
then owner edits and conflict behavior. Real model and device quality cannot be
established by local fake-provider checks.
