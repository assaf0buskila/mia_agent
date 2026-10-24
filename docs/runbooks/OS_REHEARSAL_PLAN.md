# V2-35: isolated cutover rehearsal plan

Status: NOT_INCLUDED by contract §22 E36 (2026-10-24, Assaf); kept for reference

Status: checklist written before execution; **NOT_RUN**. This does not constitute
a passed rehearsal or approval for production. Follow frozen `V2_PG_CONTRACTS.md`
v1 §§0, 16, 18, 19 and the pasted V2-35 issue. Save a private evidence directory
per UTC run ID; report reason codes/booleans, never business rows or credentials.
Use the evidence states defined in [the owner guide](OS_REHEARSAL_ENV.md).

## Integration gate, before booking a run

- [ ] [V2-35a](OS_REHEARSAL_ENV.md) evidence is complete: isolated TEST copy,
  fresh HMAC, new deployment, confirmed production pins and signed binding probe.
- [ ] Exact reviewed/CI-green backend, frontend and Script revisions are recorded.
  The frontend includes `91afca6` and V2-07/V2-09/V2-42-fe; V2-21's v2 reader is
  deployed in the rehearsal before any backend emits v2. Pin bundle hashes.
- [ ] V2-31's [CUTOVER_RUNBOOK.md](../../ops/assaf-os/CUTOVER_RUNBOOK.md) has landed
  at that release. Resolve its named **T1**, **T2**, **T3**, **T4**, **T5**, **T6**,
  **T7**, rollback, preflight and restore references before execution. It is absent
  at this preparation's `5c01704` base. Missing sections/commands are `BLOCKED`,
  never guessed commands. V2-31 owns executable commands and stage boundaries;
  the checkpoints below are acceptance obligations, not a second command source.
- [ ] Disposable PG16 and a clean detached MIA tree match the release SHA. A
  rehearsal-only Linux VM/container harness can execute that release's **actual**
  `mia settings` and `mia restore` paths, including its compose layout, VM-owned
  runtime env and job-only env files. The local launcher provides PG + uvicorn
  bootstrap only. Do not run the production VM's `mia` binary/paths for this drill.
- [ ] Harness uses TEST-only settings/secret store, mirror service account and a
  **separate new rehearsal mirror workbook**, shared only with that account.
  Production cloud credentials, webhook registrations, scheduled jobs and
  production Sheet allowlists are absent. Local-only owner auth/phone access and
  any TEST bot route have been reviewed; no production bot is repointed.
- [ ] Replay §19 **Production, waves 5-6** setup order on the isolated environment:
  default-off integrated backend with V2-10 restore post-step; V2-14a demote
  (`stage`, `activate`, recreate, smoke, `enforce`); ledger migrations V2-14+14b;
  V2-15 provision + validator; authority init (epoch 1, workspace and pepper pin).
  Pause jobs and take a verified backup before each step. This list gives order,
  not production authorization. No provision before schema creation.
- [ ] Main runtime is `mia_app`, without superuser, membership or `assaf_os`
  access; ledger runtime is `assaf_os_ledger`; migration/admin and retention roles
  are distinct. Run both §18 validators. Retention DSN is job-only, never in
  runtime env/Settings. Bootstrap `postgres` is not the runtime acceptance role.
- [ ] Finish the restore drill below, leaving backup <1 h old and drill <7 days
  old at activation. Both timestamps are machine checked, not inferred from files.

## Tests first: required machine evidence

Record the check, exit status, bounded reason, exact SHA, UTC and private artifact
reference. Empty cells are `NOT_RUN`. Every required check must pass for GO.

| Check | Pass criterion | State | Evidence |
|---|---|---|---|
| Identity/isolation | TEST binding and pins unequal; loopback PG16; exact detached SHA | NOT_RUN | |
| Preflight | All §16 rows below pass on fresh evidence | NOT_RUN | |
| Drain/reconcile | Zero pending Sheets proposals, unknown receipts and open approval cards; complete reconciled coverage | NOT_RUN | |
| Fence | Direct signed Script schema probe returns `unavailable`; trigger/reconcile/validation paths fenced | NOT_RUN | |
| Post-fence capture drift | Capture #1 versus #2 semantic workbook diff = 0; capture times > recorded fence probe | NOT_RUN | |
| Import | Zero quarantined/unresolved rows; manifest verifies and canonical import equals signed manifest | NOT_RUN | |
| Activation | Approved SHA; correct authority and epoch; main readiness passes | NOT_RUN | |
| Mirror | Seed and readback equal canonical PG, `verified_version` equals target version; no conflict/unknown | NOT_RUN | |
| T7 drift | Capture #3 versus #1 semantic workbook diff = 0 | NOT_RUN | |
| Receipt identity | Proposal/decision/request/version/epoch agree; replay/readback gives same receipt, no duplicate effect | NOT_RUN | |
| Deliberate failure | Exactly one broken precondition aborts nonzero with expected reason and no state change | NOT_RUN | |
| Pre-write rollback + forward | No non-import receipt; rollback fences core; second activation succeeds at expected epoch | NOT_RUN | |
| Restore | Actual `mia restore` drill; both role validators and readiness pass; RTO/RPO recorded | NOT_RUN | |
| Owner smoke | Copy-only accepted actions have exact receipts, fresh read and mirror readback | NOT_RUN | |

The §16 preflight/activation evidence must cover **every** frozen precondition:
image SHA equals approved SHA (`release_sha_mismatch`); direct Script fence
(`sheets_not_fenced`); freeze on (`freeze_off`); signed capture after fence probe
(`capture_before_fence`); main/ledger posture (`main_role_unsafe`,
`ledger_role_unsafe`); zero pending proposals (`sheets_pending_proposals`), unknown
receipts (`sheets_unknown_receipts`) and open cards (`approval_cards_open`);
backup <1 h (`backup_too_old`); restore drill <7 days (`restore_drill_too_old`);
mirror empty or verified (`mirror_not_verified`); signed manifest
(`manifest_not_signed`); frontend version + bundle hash evidence
(`fe_v2_reader_not_live`); journal measure below cap (`journal_capacity`).
Record actual activation as well as preflight: preflight success alone is not GO.

## Execute V2-31, retaining its commands as the source of truth

1. Open this run's checklist and timings table **before** changes. Run V2-31's
   preflight and record the baseline authority epoch, receipt boundary, backup,
   role checks and job state. Execute its **T1** drain and **T2** final reconcile
   sections. Resolve pending/unknown items through their exact receipts; never
   replay an uncertain write. Do not delete copied journals to make checks pass.
2. Execute **T3**. Set the copy's Script fence through the integrated procedure;
   confirm all writer entrypoints refuse. Apply **warning-only protection to
   every tab**, including hidden tabs, and rename the copy per the frozen contract
   to `ARCHIVE – edits ignored`. Record every tab's protection verification.
   Warning protection is not a lock against owner edits. **Source conflict:** the
   pasted issue asks for `ARCHIVE - read only`; frozen §16 has the former literal.
   V2-31/coordinator must confirm this naming conflict before the run. Do not
   silently claim both names or modify V2-31 here.
3. Execute the capture/import/sign-off checkpoints in **T4** and **T5**, in the
   order confirmed by the integrated runbook. After the successful fence probe,
   take two **xlsx** captures, #1 and #2, including hidden journals. Record UTC,
   byte size and SHA-256 for each. Run V2-30's workbook diff: **zero semantic
   changes** is required; ZIP byte hashes alone are not a drift check. Import
   from the approved capture, resolve quarantine without bypass, compare the
   signed manifest with canonical imported IDs, revisions, amounts/currencies,
   dependencies and receipt/cancellation mappings. Owner signs the exact manifest
   and digest, then authorize promotion on this copy. Never invent a signature
   format: use V2-30/V2-31's implementation.
4. **Deliberately broken precondition, before real activation:** retain a green
   candidate set, record authority/epoch and non-import receipt count. Use the
   isolated harness to turn only `assaf_os_freeze` off, leaving every other
   precondition valid. Invoke V2-31's activation check. Require nonzero exit and
   `freeze_off`, unchanged authority/epoch/receipt count and no outbox effect.
   An unrelated failure is not this test passing. Restore freeze, regenerate
   affected evidence, and require green checks again. Save both runs; never inject
   this failure in production.
5. Execute **T6** activation and backend switch using the integrated procedure.
   **T6 uses `mia settings`, then `/health` verifying the exact build SHA and
   authority epoch**; also check `/health/ready`. There is no ECS task or GCP VM
   "task" to replace. `activate` does not bump epoch. Keep freeze on, prevent all
   non-import writes, and execute **T7** read-only/mirror checks as ordered by
   V2-31: separate mirror seed, canonical readback and capture #3 diffed against
   #1. The third capture detects owner edits after the first two captures.
6. **Rollback drill before the first non-import receipt:** do read-only owner
   smoke now, not a business commit. Prove non-import receipt count remains zero,
   then execute V2-31's rollback section. Core becomes fenced; restore Sheets
   authority **on the TEST copy**. V2-31 prints production `ASSAF_OS_MODE=LIVE`
   instructions: do not apply those literally to rehearsal; require its reviewed
   TEST adaptation and keep MIA's rehearsal mode TEST. Re-probe signed binding
   and record the expected epoch. If any non-import receipt exists, this rollback
   is prohibited; stop and record NO-GO.
7. **Forward again:** follow the runbook from drain/fence through fresh captures,
   signed manifest and T6/T7. Do not reuse stale evidence or a prior approval.
   The `fenced→shadow` transition increments epoch exactly once via expected-epoch
   CAS; promotion is idempotent within an epoch and may run again at the new epoch.
   Record both epochs; activation itself must not increment. Repeat all green
   checks, including the third capture, before admitting the first owner write.
8. Only after this rollback drill, unfreeze via the runbook and perform copy-only
   owner write smoke: propose, approve, read back, recover the same decision's
   receipt and verify mirror equality. Record authoritative IDs privately and
   equality booleans in the report. Never auto-resend an `unknown`. If V2-31's
   section layout places write smoke earlier, split it into read-only and write
   portions; get that ordering resolved before execution.
9. Record timings and GO/NO-GO, then complete V2-35a teardown. A passed rehearsal
   authorizes no production action; V2-36 has separate owner gates.

## One actual `mia restore` drill (contract §19 step 6)

Run in the isolated Linux harness before activation preflight; never on the GCP
production VM. Use V2-10's integrated restore procedure and actual `mia restore`
entrypoint, not a direct `pg_restore` substituted for it.

1. Pause jobs; record receipt/version boundary, authority/epoch and sanitized
   logical checksums on the rehearsal database. Take a consistent whole-DB dump,
   verify it is nonempty, hash it and validate its catalog. Dump and pre-restore
   safety dump exclude `assaf_os.os_statement_batch_lines` data. Mark a known
   copy-only receipt before backup and another after backup to measure loss.
2. Record recovery start UTC, the dump's consistent recovery-point UTC, and the
   last committed copy-only receipt before the simulated failure. Execute
   `mia restore` against the rehearsal harness. This intentionally replaces only
   its disposable DB. Require role reapply before service start: V2-14a app
   ownership/grants, V2-15 ledger reapply and validator. Existing role passwords
   remain unchanged. Failure must leave services down.
3. Require both validators green, main role SELECT/INSERT on its own tables,
   no main-role ledger access, correct runtime ledger grants, restored logical
   data/receipts at the backup boundary and readiness. Verify old owner sessions
   revoked, line-less pending batches finished as `restored_without_detail` by
   retention, and the restore authority transition/expected epoch. Reauthenticate
   the owner; mark usability only after a successful owner read and receipt check.
4. Record **RTO = usable-and-verified UTC minus recovery-start UTC**. Record
   **RPO = simulated-failure UTC minus consistent backup recovery-point UTC**,
   plus the exact interval/count of committed receipts absent after recovery
   (post-backup sentinel absent; pre-backup sentinel present). Do not report zero
   RPO merely because no real data was used. Record backup cadence (contract:
   15 minutes), measured values and Assaf's agreed limits before the window;
   no RTO target is specified in the binding issue.
5. Restore/recreate the isolated baseline using the integrated procedure before
   the pre-write rollback drill: the deliberate restore sentinel must not leave
   a non-import receipt in that drill's active epoch. Rerun roles/init and record
   fresh baseline, backup and zero-boundary proof. Keep the original restore
   evidence intact; do not delete receipts in place to fake eligibility.

## Timings and decision

| Stage | Start UTC | End UTC | Seconds | State | Evidence / reason |
|---|---|---|---|---|---|
| Environment and backup | | | | NOT_RUN | |
| Restore outage / usable again | | | | NOT_RUN | |
| T1 drain | | | | NOT_RUN | |
| T2 final reconcile | | | | NOT_RUN | |
| T3 fence | | | | NOT_RUN | |
| T4 captures / import | | | | NOT_RUN | |
| T5 manifest sign-off | | | | NOT_RUN | |
| Broken precondition / recovery | | | | NOT_RUN | |
| T6 settings / health | | | | NOT_RUN | |
| T7 mirror / third capture | | | | NOT_RUN | |
| Pre-write rollback | | | | NOT_RUN | |
| Forward again | | | | NOT_RUN | |
| Owner write smoke / receipts | | | | NOT_RUN | |
| Teardown | | | | NOT_RUN | |

GO/NO-GO note: run ID ___; backend/FE/Script SHAs ___; independent reviewer ___;
every required machine check ___; deliberately broken check ___; timings ___;
restore RTO/RPO ___; evidence references ___; owner acceptance ___; unresolved
reason codes ___; decision ___; decision UTC ___; operator ___; Assaf ___;
next eligible action ___. Use [the one-page template](OS_CUTOVER_GO_NO_GO.md).

If any required check is missing, failed or stale: **NO-GO**. FORK-ISLAND remains
**NOT_INCLUDED**, with no island flag or split authority. If cutover misses the
December 20 cycle, reopen that fork with Assaf at this go/no-go; do not enable an
island workaround in this branch.
