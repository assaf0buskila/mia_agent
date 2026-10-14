# Claude Code handoff — Assaf OS

Checkpoint: 2026-10-14 16:46 UTC. The owner approved the architecture direction and is considering Claude Code because Codex allowance is low. Claude Code has not been launched or contacted. Stop substantial implementation and broad test runs until the owner starts the next executor.

## Scope and safety

Approved direction: retain the Cloudflare OS and MIA server; make a dedicated PostgreSQL OS ledger the single business authority, with Sheets as an asynchronous, separately verified mirror. Preserve numeric-owner authorization, immutable expiring per-write human approval, and exact-once primary decision semantics. The model may propose, never approve.

No production cutover until migration, reconciliation, writer-fencing, backup/isolated-restore, authorization, independent-review and CI gates pass, followed by final explicit owner approval. Deployments, real export/import, data migration, credentials/grants, private-repository CI-access changes, external uploads and authority activation remain paused. Do not clear live pending markers, replay uncertain writes, mint replacement request IDs, or create dual authority. A missing receipt after restore does not prove the original write never committed. Do not read .env or expose tokens, cookies, passwords or OTPs.

Read AGENTS.md, HANDOFF.md, MIA_V2.md, TASKS.md, docs/ASSAF_OS_DATABASE_FIRST_PLAN.md and ops/assaf-os/local-primary/README.md. The canonical plan's older “planning only” and “first task prototype proposed” wording is superseded by the owner's architecture approval and completed checkpoints below; its production gates still apply. Follow repository ownership and independent-review requirements. Preserve every existing worktree; no reset, clean, stash or discard.

## Repositories and exact checkpoints

Backend repository: https://github.com/assaf0buskila/mia_agent (now private).
Shared original checkout: C:\Users\lenovo\Desktop\assaf project\assaf_agent. Its current branch is unrelated; do not develop there.

Frontend repository: https://github.com/assaf0buskila/assaf-landingPage.

Exact commit keys:
- E: ef161ae278d93bcf2f70f2f3d18371f11805a7a1 — GET-only export/provenance preparation.
- S: f36a2fbccb03da583304ad8a8b0e07bd65711c7d — adapter and synthetic import shadow.
- L: 5fcce60abb44c1c770f0650dda3a134b2ea38cc0 — seven-entity ledger.
- B: 72b7468da1e38865630448853fdcd4c969e487f9 — separate Sheets/API creation-request recovery patch, not deployed.
- F: 6e897fcfab12081f8bb6c3c04ab4540c4f59221d — frontend relief/recovery release.

Paths below are exact: T2 = C:\Users\lenovo\Documents\Codex\2026-10-14\task-2; T1 = C:\Users\lenovo\Documents\Codex\2026-10-14\task.

| Worktree | Branch | HEAD |
|---|---|---|
| T2\backend-creation-recovery | codex/os-primary-creation-recovery-local | E |
| T1\os-readonly-export | codex/os-readonly-export-local | E |
| T1\os-primary-shadow | codex/os-primary-shadow-adapter-local | S |
| T1\os-primary-seven | codex/os-primary-seven-entities-local | L |
| T2\backend-recovery | codex/os-pending-recovery-api | B |
| T2\backend-personal-tasks | codex/personal-tasks-api | B |
| T2\frontend-recovery | codex/os-pending-recovery-ui | F |
| T2\frontend-personal-tasks | codex/personal-tasks-ui | F |
| T2\frontend-relief-release | detached | F |

All nine worktrees were verified clean at 16:46 UTC before writing this handoff. The only new uncommitted file in the new worktree is docs/CLAUDE_CODE_HANDOFF.md, this document. No implementation agent was launched, no lookup code edited, and no tests run for that new slice. T2 itself is an evidence directory outside Git; preserve its existing artifacts, including the newly written phone-login-triage.json. Ignored .cache validation logs in prior worktrees must also be preserved.

## Reconciliation: reuse completed work

E exists and adds nine export/provenance files after S. Adapter, ledger and task-primary files are unchanged between S and E.

Already implemented locally: seven entity semantics, atomic row/receipt/audit/outbox decisions, numeric owner/workspace/epoch binding, request-fingerprint refusal, exact decision receipt recovery, default-off synthetic facade, resumable synthetic staging and reconciliation, bounded synthetic benchmarks.

E adds assaf_os_export_reader.py, assaf_os_export_provenance.py, frozen schema, offline native oracle, tests and export-preparation documents. Reader is default-off, GET-only and sealed to synthetic transport. Provenance maps raw/formula/header/history evidence into a dry validation manifest; no business import batch or live Google adapter exists. Its invented guard/evidence tables are not known to exist in the real workbook. Matching two-pass fixture reads do not prove a transactional live snapshot or eliminate ABA changes.

Do not rebuild this export preparation. Read EXPORT_PREPARATION_PLAN.md and EXPORT_PREPARATION_VALIDATION.md under ops/assaf-os/local-primary. The committed validation report predates Docker repair; newer ignored logs provide the repaired results.

## Tests actually completed versus pending

Existing E evidence, read and verified in this handoff session:
- .cache/export-pgverify-prototypes.log: 197 passed, no skips, 13.49s; all 76 PostgreSQL cases executed.
- .cache/export-pgverify-full.log: 3404 passed, 111 existing opt-in skips, 177.20s.
- Export work: 55 tests; independent adversarial review: 25 tests; existing offline native OS checks: 68. Ruff, widget and syntax checks recorded passing.
- Prior synthetic benchmark: 15 samples plus three warmups, sequential local PG16 loopback. Propose/approve/fresh verification p50 65.348ms, p95/p99/max 500.923ms; first snapshot page p50 15.344ms, p95/p99/max 96.265ms. This excludes browser/WAN/auth/mirror and proves no production SLO.

Other existing release evidence:
- F: 182 frontend tests, two unchanged-backend cross-contract tests, 21 independent focused tests; exact-head CI and Unified OS integration succeeded.
- B: 33 core Python, 32 related Python, 86 JS tests, Ruff/widget checks and independent HEAVY review passed. This patch is for the existing Sheets path, not a completed PostgreSQL lookup.

None of those suites were rerun in this checkpoint session. The new PostgreSQL creation-request lookup, its regressions, independent review and any updated full-suite run remain pending. Use existing cached dependencies and a new disposable loopback PostgreSQL fixture with the required database prefix/marker. No package installation or production database reuse.

## First next action and corrected remaining backlog

Start in C:\Users\lenovo\Documents\Codex\2026-10-14\task-2\backend-creation-recovery.

1. Implement only a read-only creation-request UUID lookup in the default-off local ledger/facade. LocalPrimaryAdapter currently has contract/snapshot/propose/decide/receipt, but no creation UUID lookup; proposal rows already store request_id. Freshly verify read scope, numeric owner, workspace and authority epoch. Return the original immutable proposal and exact decision IDs; a missing result stays unknown. Reads must not expire/consume proposals, change domain rows/receipts/audit/outbox/epoch, replay POSTs or activate routes. Recovery must work while mutation policy/browser-mutation verification is closed, without bypassing authentication or authority checks.
2. Add synthetic regressions for lost creation acknowledgement, pending and terminal proposals, unknown UUID, malformed UUID, read-scope/session expiry, wrong owner/workspace/epoch, and zero read-side mutations. Use actual PostgreSQL for transactional/concurrency claims; obtain independent review before completion.
3. Then prepare reviewed server-auth/session/CSRF and existing owner-approval integration plus the honest PostgreSQL browser/MIA transport contract, still disabled. Bind recovery markers/cards to authority epoch and preserve exact receipt truth.
4. Prepare the missing real bounded read-only source transport, protected staging and provenance/coverage treatment. Actual export/import requires separate authorization and identified source access. The current signed Sheet snapshot reconciles and can write; do not call it a read-only export.
5. Rehearse resumable full seven-entity migration and semantic/history reconciliation with synthetic data; inventory/fence all old writers and design a workspace-wide switch. No live per-family canary or synchronous dual write.
6. Rehearse bounded asynchronous mirror delivery, unknown acknowledgement, stale leased writes, periodic anti-entropy even with an empty queue, capacity, encrypted off-host backups and isolated restore. Fence old hosts after recovery, use a new epoch, and reconcile the potential loss window. Proposed RPO15m/RTO2h and action p95<4s remain targets requiring proof.
7. Only after all gates and final owner approval: coordinated exact CI-green backend/frontend release, approved migration/final frozen export/import, single authority switch, then bounded real-device acceptance. After primary writes, prefer a compatible DB-primary rollback/forward fix; reverting authority to a stale Sheet requires a separately approved reverse migration.

Native mirror edit policy, mirror destination, lag/retention, backup budget and actual VM capacity remain concrete decisions; plan approval does not grant new credentials or provider changes. The local ledger still loads all seven collections for several reads and has a 2000-record synthetic bound; address real-volume queries and pagination consistency before production claims.

## Claude Code progress — 2026-10-14

**Backlog step 1–2: done locally (`LOCAL_TESTED`).** The read-only creation-request lookup is in place: `LocalLedgerPrimary.creation_request` and `LocalPrimaryAdapter.creation_request`, on this branch.
- `cf64cbc`: implementation and regressions.
- `ecec72e`: independent-review round-1 fixes (PASS_WITH_NITS, no blockers).
- Contract and limits are in `ops/assaf-os/local-primary/README.md` → "Creation-request lookup".

Evidence. The disposable loopback PG16 is `assaf_os_local_creation_lookup` on port 56211, in its own container `assaf-os-creation-lookup`. Logs are under `.cache/creation-lookup-*`.
- Focused ledger and adapter tests: 20 were red before implementation. After the review fixes, 88 pass.
- Prototype suite: 233 passed, all PostgreSQL cases executed.
- Full suite at `ecec72e`: 3440 passed, 111 existing opt-in skips, 0 failed. The E baseline was 3404/111.
- Ruff is clean and the widget checks pass.
- Mutation check: 6 of 6 deliberate breakages were caught (decision-ID map, receipt version, receipt epoch, removed READ ONLY, adapter ID map, read-side expiry write).

Still open: backlog steps 3–7 above, unchanged. The lookup has no route, no transport and no activation. Reviewer note, left as a product decision: decision IDs are visible to every owner channel holding read scope (OS/MIA/TELEGRAM_APPROVAL). Authority is unchanged.

Live OS check (read-only, unauthenticated, 2026-10-14). The page renders at 375px with no horizontal scroll. There is no apple-touch-icon or manifest, so iPhone "Add to Home Screen" gets no brand icon: only an SVG data-URI favicon is served, and the icon/manifest paths return a JSON 404. The fix is planned as a separate frontend chunk.

## Last verified production and access

OS URL: https://assaf-os.assaf-buskila10.workers.dev/

Cloudflare frontend: F, version10 at100%, version ID 15aa208e-f094-4488-923c-2d579e76f6ac, deployment ID d20d2e5c-2a35-46c8-8692-63774f0cf79d. Promoted 2026-10-14 15:36:56 UTC; verified 15:39:07 UTC. Existing rollback is version9, ID 8a53459d-51d3-40f9-8158-5573c831060b. Do not redeploy merely because the desktop connection flaps.

MIA last verified live commit: 459b945a2cb947b066fd3fab8f0fcf8425db40b3, source-check artifact dated16:22:26 UTC. Live creation-request route is absent. B is not uploaded/deployed; installed Apps Script source/version is unverified. No PostgreSQL prototype is activated. Existing Sheet-backed business authority remains in place. The original Save's exact request UUID, cause and outcome remain unresolved; the frontend relief did not settle it or clear its marker.

Chrome on the user's Windows PC is blocked before tab discovery: HRESULT0x80070003, “cannot locate required Windows platform directory.” Reset/retry failed. Shell/Git reads succeeded at this checkpoint; catalog “connected” is not proof browser automation works. No cloud-browser fallback. Telegram session/user/private bot handle remain unverified; zero live test messages sent. Four explicitly synthetic conversation-only tests are prepared in phone-login-triage.json. Verify the numeric owner and exact private bot before sending.

Cloudflare MCP persisted auth_status=o_auth was confirmed in normal Windows; this does not verify Chrome or Wrangler login. Earlier Wrangler device code expired, current Wrangler auth is unverified, and no new five-minute flow was started. Coordinate any retry with the owner/parent and disclose verification URL/code immediately; add no scopes. No Apps Script project/editor/deployment ID is identified; do not create permissions/triggers.

Backend repo privacy was verified; future frontend cross-repository CI may need explicit private-backend access. That access change is paused. No backend source upload is authorized by this handoff.

## Supporting local evidence

In T2: cloudflare-relief-production-verification.json; recovery-verification.json; os-live-mia-source-verification.json; os-architecture-independent-review.json; os-live-schema-checkpoint.json; apps-script-access-audit.json; mia-repository-private-verification.json; phone-login-triage.json.

These are checkpoints, not permission to mutate production. No credentials or private conversation transcript are included here.
