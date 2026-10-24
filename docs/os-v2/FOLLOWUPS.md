# OS v2 follow-up findings (verbatim from the Claude reviews)

## V2-10 (merged #167, review r2 at a9a1e1f) -> V2-10b

### 1. [minor] ops/gcp/vm/pg-hba-harden.sh:55-62 (dsn_sources / fixed_roles); app/core/config.py:224-225; app/services/owner_os_reads.py:322; app/api/owner_os_actions.py:55
**Scenario:** The app reads two more loopback-capable DSNs from settings.env (mia-env): MIA_OWNER_OS_READ_DATABASE_URL and MIA_OWNER_OS_ACTION_DATABASE_URL. They are not in dsn_sources, and their roles are not in fixed_roles. Suppose either is set to postgresql+psycopg://<role>:<pw>@127.0.0.1:5432/mia, and the role has no SCRAM verifier or the password has drifted (both hidden while the line is trust). Then --apply passes the precondition, never logs in with that DSN after the reload, and prints applied=true. Owner OS reads or actions then fail closed with 503, and nothing rolls back. /health/ready stays 200, so runbook step 1a's readiness check does not catch it. The header's claim that every readable live DSN logs in after the reload does not hold for these two keys. This depends on production config: LIVE_STATUS shows OS reads going through the Apps Script binding today, so it is probably not live.

**Fix:** In scan_dsns, scan every key matching ^[A-Z0-9_]*DATABASE_URL= in runtime.env, settings.env and os-ledger-migrate.env, or at least add the READ and ACTION keys to dsn_sources. Add a disposable-cluster test with a wrong-password READ DSN that must roll back. Do this before the step 1a go; it need not block the merge.

### 2. [minor] ops/gcp/vm/mia:355-364 and 487-488 (post_restore_reapply, then start_services); ops/gcp/backup/README.md:153-155 and 220
**Scenario:** Services stay down after an abort only until another mia command calls start_services: mia deploy, mia settings, mia https enable or mia go-live. The README's own cause for ledger_reapply_failed, an image with no 40-hex MIA_BUILD_SHA, is fixed by a redeploy, and mia deploy runs start_services. The app and caddy then come up on a database where step 1 succeeded and step 2 did not. The ledger is not re-owned, and owner sessions issued before the restore point are still valid, because step 4 never ran. That lasts until the operator runs mia restore again. wait_healthy only checks /health/live, so a failed step 1 does not stop this either.

**Fix:** Write a marker such as /etc/mia/restore-incomplete before DROP DATABASE. Remove it only after post_restore_reapply succeeds. Make start_services (or deploy, settings, https and go-live) refuse with a reason code while it exists. At minimum, warn in the README that the deploy fix starts the services, and that mia restore must run again right away.

### 3. [minor] ops/gcp/backup/README.md:220 (ledger_reapply_failed row); restore post-step 2 via provision_ledger.py --mode reapply
**Scenario:** Once a later ledger migration ships, take a dump made before it and restore it with the current image. Step 2 issues GRANTs on tables that do not exist yet, and V2-15's exact validator expects the newer catalog. So it always fails with ledger_reapply_failed. The README's advice to fix the cause and run mia restore again then loops. mia os-ledger migrate also refuses on the unrepaired posture. Today V2-14 and 14b migrate together and production has no assaf_os, so nothing is exposed yet.

**Fix:** Document the cause and the way out in the ledger_reapply_failed row: restore with the image whose ledger schema matches the dump (mia deploy <old tag>, then mia restore), then forward-migrate and re-provision. Better, have the cutover or ledger runbook record which image matches which dump.

### 4. [minor] ops/gcp/vm/compose.yaml (postgres environment), ops/gcp/vm/bootstrap.sh:32, ops/gcp/vm/pg-hba-harden.sh:13-19; contract section 22 E21
**Scenario:** E21's binding text says --auth-host=scram-sha-256 --auth-local=scram-sha-256 and 'rewrites every trust line'. The PR sets --auth-host only and rewrites host* lines only. I checked that this deviation is correct. mia backup, mia restore (restore_sql), app_role_demote.py, backup_offsite.py and the drill all use docker compose exec psql -U mia over the in-container socket with no password. --auth-local=scram would break all of them, and the socket is reachable only through root-equivalent docker exec. The code is right, but it departs from a binding contract with no erratum recorded.

**Fix:** The coordinator records an E21 erratum (host lines and --auth-host only, the socket stays trust, with this reason) before merge. No code change.

### 5. [minor] ops/gcp/backup/README.md:268-272 (Open gap)
**Scenario:** mia has no command for the V2-15 --mode provision step that the new-VM recovery order needs. Until V2-31 adds one, a lost VM cannot be rebuilt from these backups once assaf_os holds data. The gap is documented and the drill reproduces it. The author could not file the tracking issue.

**Fix:** The coordinator files the issue against V2-31: the new-VM order goes into the cutover runbook, and the mia provision command must exist before ledger activation.

### 6. [nit] ops/gcp/backup/backup_offsite.py:417 and 303-304 (state_lock_busy is checked once, then fetch_key and pg_dump run without holding the lock)
**Scenario:** Check-then-act. A mia restore can take the lock after the check, while gcloud is fetching the key, and DROP and recreate mia before pg_dump starts. The slot then dumps a partially restored database and uploads it as the newest 15min/ object, and possibly as the day's daily/ copy. The same happens on every slot after an aborted restore, because the lock is released. Older good objects remain (objectCreator cannot delete), so nothing is lost, but restore_drill --latest would pick the bad one.

**Fix:** Hold a shared flock on the state lock across the dump phase, or skip while a restore-incomplete marker exists (see the services-down finding).

### 7. [nit] ops/gcp/backup/mia-backup-offsite.service (TimeoutStartSec=25min); ops/gcp/backup/backup_offsite.py run_backup finally
**Scenario:** At the timeout systemd sends SIGTERM. Python has no handler, so the finally with rmtree does not run. The plaintext dump.bin and the key file stay in /var/lib/mia/backup-offsite/runs/run-*, which is root-only and 0700, until a later run prunes them after 2 h.

**Fix:** Install a SIGTERM handler that raises SystemExit so the finally runs, or prune stale runs sooner.

### 8. [nit] ops/gcp/vm/mia:23 (usage)
**Scenario:** The 'backup' usage line lost a space, so its description no longer lines up with the other commands.

**Fix:** Restore the original padding (16 spaces).

### 9. [nit] PR #167 on GitHub
**Scenario:** GitHub still shows head b157b13 and the old body: stacked on #169, the drill uses a marked stand-in, held for the GPT cross-check. That contradicts E15 and this SHA. a9a1e1f is not pushed, so CI has not run on the reviewed head.

**Fix:** Push a9a1e1f with a normal push. Replace the body with .cache/pr167-body-draft.md and add the attribution trailer. Merge only on green checks, postgres and container at exactly a9a1e1f.

## V2-31 (#185, review r2 at 38aac76) -> V2-31b

### 1. [minor] disposable PG 127.0.0.1:56242, environment only (not code); log C:\Users\lenovo\Documents\Codex\2026-10-14\task-2\backend-v2-31\.cache\lane-review-V2-31-r2-pg.log
**Scenario:** I could not confirm the focused PostgreSQL gate myself. My run used database owner_os_test, but the author ran on mia_ledger_test: last_autoanalyze of assaf_os.os_authority there is 2026-10-19 12:49 UTC, which matches the author's 15:48 local run. The runtime and retention roles already held grants in mia_ledger_test. The fixture build_pg_env (tests/services/os_ledger/conftest.py:225) applied ledger_grant_statements again in owner_os_test. validate_ledger_role then correctly refused a runtime role that has footprints in two databases (role_policy._footprints). Result: 503 passed, 5 failed, 34 errors, 2 skipped. That is 544 collected, the same as the author's 544. There were 0 PG skips; the 2 skips are POSIX-only. All 39 non-passes are ValueError ledger_role_denied raised inside that shared fixture. My run LEFT those grants in owner_os_test, so the next PG run on mia_ledger_test will also fail until they are removed. The permission classifier blocked both the cleanup and a re-run.

**Fix:** Before merge, the coordinator or Assaf runs, against the V2-31 container only: docker exec assaf-os-v2-31-pg psql -U postgres -d owner_os_test -c "DROP OWNED BY assaf_os_ledger, assaf_os_retention;". Then re-run the focused suites with MIA_OS_LEDGER_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:56242/mia_ledger_test (tests/workers, tests/ops/test_os_jobs.py, test_provision_retention.py, test_cutover_runbook.py, tests/services/test_os_backend_factory.py) and confirm 542 passed, 2 skipped (POSIX-only). CI does not run these PG cases, so this is the only PG proof.

### 2. [minor] app/workers/os_authority.py:737-767 (_check_pending); ops/assaf-os/CUTOVER_RUNBOOK.md:278 and :290 (T1/T2 rollback)
**Scenario:** T1 freeze, then T2 drain records sheets_pending 0 at epoch E and SHA S. The window is then aborted before T3. The T2 rollback unfreezes, and the runbook says 'Nothing else changed'. Over the next days the owner creates OS-screen proposals; these have no Telegram card, so approval_cards_open never sees them. In the next window the operator runs T1 and then goes straight to T3, because drain.json still shows 0 at the same epoch and SHA. No fence had ever been recorded, so fence_broken_at is absent. The old drain satisfies recorded < fence_probe_at, activate passes sheets_pending_proposals, and those pending proposals are stranded in the archive. The round-1 fix covers only a lifted fence. test_a_live_preflight_before_any_fence_does_not_void_the_drain assumes the freeze held throughout.

**Fix:** Machine-check it: have `freeze-plan --phase drain` move an existing drain.json aside (each new freeze starts a new drain), and/or bound the drain's age against fence_probe_at (T3 follows T2 immediately), e.g. refuse with drain_stale beyond a few hours. At minimum, the T1/T2 rollback text must say that a retry redoes T2 and that drain.json is set aside.

### 3. [minor] app/workers/os_authority.py:635-660 (_fence_record, the 'unknown' branch); ops/assaf-os/CUTOVER_RUNBOOK.md:367-370 (T6 preflight)
**Scenario:** At T6 the runbook requires a preflight. If the signed schema probe times out once (20 s budget) or returns a transport error, the state is 'unknown' and preflight.json is rewritten without fence_probe_at. The next preflight sets fence_probe_at to now, which is after captures #1 and #2. capture_before_fence then fails with capture_not_after_fence, and the operator must redo T4 captures, staging (G24) and the sign-off and promote (G12). This adds no safety: an unprobed gap between two 'unavailable' probes is already treated as continuous, so an 'unknown' probe carries no more information than no probe. The runbook never says that an 'unknown' preflight voids the captures.

**Fix:** Carry fence_probe_at through 'unknown' (it is still no break), or leave preflight.json untouched on 'unknown'. If the strict behaviour is intended, document in T3/T6 and in the section 7 row that a 'sheets: unknown' preflight voids the captures.

### 4. [minor] app/workers/os_authority.py:1176-1195 (cmd_rollback)
**Scenario:** Rollback is allowed after the 'open' phase and until the first non-import receipt, which is exactly when Assaf is doing the G16/G17 phone actions. probes.non_import_receipts and admin.transition(core, fenced) are separate transactions, and the ledger does not check receipts on active->fenced. A commit can therefore land between the count and the fence. The CLI then reports success, prints 'The Sheet is exactly as captured', and gives the revert-to-Sheets steps, stranding a business write in a ledger the runbook abandons.

**Fix:** Re-count non_import_receipts after the transition. If it is nonzero, refuse rollback_after_first_write (core stays fenced, which is the section 6 path). Alternatively, count inside the same FOR UPDATE transaction.

### 5. [nit] pyproject.toml:24-26
**Scenario:** Contract section 3.1 lists os-import in [project.scripts]. The PR adds only os-authority, os-verifier and os-retention. The author's reason is sound: scripts/ is not in the wheel, and compose runs python /app/scripts/os_import.py from the read-only tag mount. It is still an unrecorded contract deviation.

**Fix:** The coordinator records an erratum: os-import runs from the mounted tag scripts and is not a console script.

### 6. [nit] tests/services/test_os_backend_factory.py:1089 (comment) and PR body
**Scenario:** The new factory site is called a 'read-only Sheets probe', but LiveProbes.sheets_snapshot_pending is the disclosed MUTATING T2 snapshot (it accepts pending native edits), as the runbook itself says.

**Fix:** Reword it to 'the T2 drain snapshot (disclosed mutating call), actor MIA'.

### 7. [nit] ops/assaf-os/CUTOVER_RUNBOOK.md:391-397 (T7 precondition)
**Scenario:** E11.4 says V2-31's runbook must create the eight mirror tabs. The runbook only requires that they exist 'with their exact names and headers'. It lists the names but says neither where the headers come from nor how the tabs are created.

**Fix:** Add the creation step, with the header source (the V2-29 mirror schema), or point to the V2-35a guide section that does it.

### 8. [nit] app/workers/os_authority.py:1098-1135 (cmd_activate) and 1247-1281 (freeze-plan open)
**Scenario:** Two checks are left to people. First, activate does not check that the signed manifest digest was actually promoted at this epoch: if the operator skips the T5 promote, activation passes on an empty ledger, and only the T6 phone check catches it, with no data loss since rollback is still allowed. Second, freeze-plan --phase open does not check 'mirror verified' or 'non_import_receipts == 0'; runbook section 4.1 leaves that to the operator. Neither is required by section 16.

**Fix:** Optional hardening: an import receipt at this epoch that binds manifest_digest as an extra activate check, and mirror verified as an extra check before open.

## Extra V2-31b items (coordinator)

- **CI coverage.** Add the V2-31 PostgreSQL test files to CI, in a NEW job with its own fresh cluster: they leave cluster roles behind, so they cannot share the os-ledger-migration-postgres cluster. The files are tests/workers/test_os_authority*.py, test_os_freeze_paths.py, tests/ops/test_os_jobs.py, test_provision_retention.py, test_cutover_runbook.py and tests/services/test_os_backend_factory.py.
  - Better still, make those tests drop the roles they create, so they can also share a cluster.
- **os-import job.** Add an os-import entry point (V2-30 obligation). It must call `authorize.require(ctx, 'import_promote')`, and it must treat a same-epoch re-stage, which raises `ledger_integrity_failed` on import_batch_pk, as already staged.
- **Freeze census nits** from the V2-31 focused review:
  - in drive_pg_telegram, compare full ledger row counts and version, not only receipts;
  - in the frozen `_create_final_ledger_proposal` case, assert `provider_claims == []`;
  - optionally, add an early `os_frozen` check before the local claim.

## V2-29 mirror nits (optional, lowest priority)

- N3: if two publishers ever run at once, a stale writer can leave the state falsely `synced`.
- N5: a burst of events rewrites the mirror once per 100 events.
- N6: a deferred anti-entropy pass reloads the snapshot every 5 s. Floor that outcome.

## Docs follow-up (#101 carry-over, from the #183 review)

- docs/ASSAF_OS_RELEASE_STATUS.md line 3 says Script version 9; it is version 10.
- The Worker IDs and rollback IDs in LIVE_STATUS.md and ASSAF_OS_FINISH_PLAN.md need marking as historical, pointing to docs/CLAUDE_CODE_HANDOFF.md:105.
- Point to the newer MIA health record.
- The "draft PRs remain unmerged" lines are stale: #101 landed via #183.
- Add a superseded-by pointer on the Sheets-first finish plan, linking docs/ASSAF_OS_DATABASE_FIRST_PLAN.md.
- Document the Apps Script bundle-hash rule:
  - files schema.js, core.js, runtime.js, UnifiedSheets.gs and UnifiedRuntime.gs, in that order;
  - CRLF converted to LF, trailing whitespace trimmed, exactly one trailing newline per file;
  - files joined with a newline, then sha256;
  - appsscript.json is excluded.
  - Fix docs/runbooks/OS_REHEARSAL_ENV.md, which lists 6 files.

## Added 2026-10-24 (coordinator)

| # | Severity | Finding | Where | Owner / state |
|---|---|---|---|---|
| F24-1 | minor | Order-dependent test failure: `test_each_mode_builds_the_import_worker_and_requires_import_promote_first[fake-sheets]` fails after other suites in `pytest tests/workers tests/services/os_ledger -x`, and passes alone. Codex traces it to authorization-module identity across tests. It is pre-existing on master (proved on master + WP2). | `tests/services/os_ledger/test_os_import_write_cli.py` | builders lane, Codex build |
| F24-2 | low | Outbox mixes clocks. Claim eligibility compares DB-derived `next_attempt_at` with an app-clock `:now`. `GREATEST(now(), updated_at)` runs over app-stamped `updated_at`. Production shares one VM clock, so the risk is low. Use DB time for both. | `app/services/os_ledger/outbox.py:148,188` | builders lane, Codex build, opus review |
| F24-3 | minor | iOS: while a field holds unsaved typed text, an owner tap on filter, toggle, refresh or show-more looks ignored until blur. Force a repaint for owner taps. | landing `os-boot-v2.mjs:361` | FE lane; PHONE_VERIFIED needed |
| F24-4 | minor | The keyboard may close after a quick-add (V2-33). | landing FE | FE lane |
| F24-5 | minor | Cold-path bundle is 9.6 KB brotli. Lazy-split the non-cold modules without changing the frozen V2-42 boot. | landing FE | FE lane |
| F24-6 | minor | The `ops/gcp/backup` tools spawn `gcloud`, which is `gcloud.cmd` on the Windows PC (exit 127). This blocks the PC restore drill that E36 requires. | `ops/gcp/backup/backup_lib.py:335-366` | coordinator, Codex build in progress |
| F24-7 | info | F-P1 (cached paint waits for the schema) is skipped. It would change the frozen V2-42 boot, and post-cutover schema calls take about 100 ms. F-P2 (duplicate schema request) is fixed in the FE chain. | landing FE | closed |
| F24-8 | info | A date time bomb in the V2-26 PG retention test (frozen `NOW` against DB `now()`) turned master CI red. It was fixed in #198 (0bcf77d). A Codex sweep found no other bombs (13 safe). | `tests/services/os_ledger/statements/test_retention_worker.py` | closed |
| F24-9 | info | Workbook drift check blind spot (date1904, sheet state, sheet set and order). | `scripts/os_workbook_diff.py` | WP2 PR #197 |
| F24-10 | info | Mirror vs freeze deadlock at T7 (E35). | `app/workers/os_mirror.py` | WP1 PR #199 |
