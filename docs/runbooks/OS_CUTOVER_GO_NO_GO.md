# Assaf OS v2: GO / NO-GO record

Fill before advancing each live stage. The copy rehearsal is NOT_INCLUDED by
contract §22 E36 (2026-10-24, Assaf); its rows below are retained for reference.
Use [CUTOVER_RUNBOOK.md step 6](../../ops/assaf-os/CUTOVER_RUNBOOK.md#step-6-e36-readiness-before-wave-6)
for the four E36 conditions and G09 statement. No percentages or inferred
approval. Run/window ___; UTC ___; operator ___; Assaf present ___; reviewer ___.
Decision: **GO / NO-GO** ___; exact next authorized action ___; reason code ___.
Backend SHA ___; frontend SHA/bundle ___; Script revision/digest ___; epoch ___.

States describe the evidence: `NOT_RUN`, `CODE_CHECKED`, `LOCAL_TESTED`,
`REHEARSAL_VERIFIED`, `PRODUCTION_VERIFIED`, `OWNER_ACCEPTED`, `BLOCKED`,
`NOT_INCLUDED`. Record failures as `BLOCKED` plus a bounded reason code. Local
checks never establish production/device acceptance. Store private identifiers,
exports and hashes in the restricted evidence store; no credentials or payloads.
`REHEARSAL_VERIFIED` is retained for reference only (E36).

| Required evidence | State | Evidence reference / UTC / reason code |
|---|---|---|
| Exact SHAs, independent HEAVY PASS, exact-head CI green | NOT_RUN | |
| Frozen contract / V2-31 stage mapping resolved | NOT_RUN | |
| TEST copy, production pins, PG16, fresh HMAC / isolated harness | NOT_INCLUDED (E36) | Reference only |
| §19 order, main/ledger roles and validators | NOT_RUN | |
| Backup verified/nonempty, <1 h; drill <7 days | NOT_RUN | |
| Full V2-35 rehearsal passed, including broken-precondition abort | NOT_INCLUDED (E36) | Reference only |
| E36: fixes merged | NOT_RUN | WP1/E35 (#199) and WP2 (#197) merged PR SHAs, both ancestors of `S`; each PR `state == MERGED`, merge timestamp and 40-hex `mergeCommit.oid`; both ancestry checks exit 0 (step 6) |
| E36: CI green | NOT_RUN | run ID/URL and JSON: `headSha == S`, run `conclusion == success`, job `os-cutover-postgres` with `conclusion == success`; runs `tests/workers/test_os_cutover_sequence.py`, proving T6→T7 with mirror verified under the freeze; WP2's drift tests prove the workbook-level drift check |
| E36: PC restore drill | NOT_RUN | On Assaf's PC, never the VM: `ops/gcp/backup/restore_drill.py` report `restore-drill.json`; `started_utc` < 7 days old at activation, `passed: true`, `rto_met: true`; recheck age at activation (`restore_drill_too_old` blocks); record Assaf's RTO ≤ 2 h and RPO ≤ 15 min limits |
| E36: live window and rollback | NOT_RUN | Assaf's quiet window: explicit statement in chat naming `S` and the UTC window, with section 5 rollback ready before the first non-import receipt; every G10-G17 gate keeps its explicit go |
| Restore entrypoint / roles / sessions / retention verified | NOT_RUN | |
| Every §16 preflight and activation precondition | NOT_RUN | |
| Drain: zero pending / unknown receipts / open approval cards | NOT_RUN | |
| Fence, every-tab warning protection and archive name | NOT_RUN | |
| Post-fence xlsx #1 vs #2 diff zero; signed manifest equal | NOT_RUN | |
| T6 settings, health exact SHA + epoch, readiness | NOT_RUN | |
| T7 xlsx #3 vs #1 diff zero; mirror seed/readback verified | NOT_RUN | |
| Zero non-import receipt boundary; rehearsal rollback + forward | NOT_INCLUDED (E36) | Copy-run evidence; reference only |
| Live zero non-import receipt boundary and section 5 rollback readiness | NOT_RUN | Required before the first non-import PG receipt |
| Owner smoke, exact receipt identity and visible effects | NOT_RUN | |
| ≥20 phone actions with original V2-03 copies; p95 <4000 ms | NOT_RUN | |
| Measured restore RTO/RPO accepted against prior limits | NOT_RUN | |
| Separate Assaf go for each step in OS_PHONE_ACCEPTANCE.md | NOT_RUN | |
| Phone acceptance, then separately approved onEdit removal | NOT_RUN | |
| TEST deployment archived, copy deleted, data disposition recorded | NOT_INCLUDED (E36) | Reference only |
| V2-34 island | NOT_INCLUDED | Revisit only if Dec 20 cycle missed |

Phone count ___; p95 ms ___; timing artifact ___; RTO seconds ___ / limit ___;
RPO seconds ___ / limit ___; lost receipt interval/count ___; restore artifact ___.
Pending dependencies/reason codes ___; first non-import receipt exists ___;
eligible rollback/recovery path ___; next action's exact owner go reference ___.

Record all four E36 evidence items and Assaf's G09 statement ___. Missing, stale
or mismatched evidence stops wave 6. A release SHA or window change requires fresh
matching evidence and an explicit G09 statement.

**Decision rule:** `NOT_INCLUDED (E36)` reference rows are excluded. Required
evidence for the next action must be current and in
the appropriate environment, with no `NOT_RUN` or `BLOCKED` gate. An earlier
stage may be GO without authorizing a later one. Missing approval means NO-GO.
After a non-import receipt, never perform a simple Sheets rollback. Resolve
unknowns by receipt; obtain a new go for forward recovery. Subsequent releases
remain separate: one-tap → fast tasks → statements → assistant, each its own go.
