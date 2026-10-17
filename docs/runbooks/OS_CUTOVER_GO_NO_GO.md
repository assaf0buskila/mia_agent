# Assaf OS v2: GO / NO-GO record

Fill before advancing each rehearsal/live stage. No percentages or inferred
approval. Run/window ___; UTC ___; operator ___; Assaf present ___; reviewer ___.
Decision: **GO / NO-GO** ___; exact next authorized action ___; reason code ___.
Backend SHA ___; frontend SHA/bundle ___; Script revision/digest ___; epoch ___.

States describe the evidence: `NOT_RUN`, `CODE_CHECKED`, `LOCAL_TESTED`,
`REHEARSAL_VERIFIED`, `PRODUCTION_VERIFIED`, `OWNER_ACCEPTED`, `BLOCKED`,
`NOT_INCLUDED`. Record failures as `BLOCKED` plus a bounded reason code. Local
checks never establish production/device acceptance. Store private identifiers,
exports and hashes in the restricted evidence store; no credentials or payloads.

| Required evidence | State | Evidence reference / UTC / reason code |
|---|---|---|
| Exact SHAs, independent HEAVY PASS, exact-head CI green | NOT_RUN | |
| Frozen contract / V2-31 stage mapping resolved | NOT_RUN | |
| TEST copy, production pins, PG16, fresh HMAC / isolated harness | NOT_RUN | |
| §19 order, main/ledger roles and validators | NOT_RUN | |
| Backup verified/nonempty, <1 h; drill <7 days | NOT_RUN | |
| Full V2-35 rehearsal passed, including broken-precondition abort | NOT_RUN | |
| Restore entrypoint / roles / sessions / retention verified | NOT_RUN | |
| Every §16 preflight and activation precondition | NOT_RUN | |
| Drain: zero pending / unknown receipts / open approval cards | NOT_RUN | |
| Fence, every-tab warning protection and archive name | NOT_RUN | |
| Post-fence xlsx #1 vs #2 diff zero; signed manifest equal | NOT_RUN | |
| T6 settings, health exact SHA + epoch, readiness | NOT_RUN | |
| T7 xlsx #3 vs #1 diff zero; mirror seed/readback verified | NOT_RUN | |
| Zero non-import receipt boundary; rehearsal rollback + forward | NOT_RUN | |
| Owner smoke, exact receipt identity and visible effects | NOT_RUN | |
| ≥20 phone actions with original V2-03 copies; p95 <4000 ms | NOT_RUN | |
| Measured restore RTO/RPO accepted against prior limits | NOT_RUN | |
| Separate Assaf go for each step in OS_PHONE_ACCEPTANCE.md | NOT_RUN | |
| Phone acceptance, then separately approved onEdit removal | NOT_RUN | |
| TEST deployment archived, copy deleted, data disposition recorded | NOT_RUN | |
| V2-34 island | NOT_INCLUDED | Revisit only if Dec 20 cycle missed |

Phone count ___; p95 ms ___; timing artifact ___; RTO seconds ___ / limit ___;
RPO seconds ___ / limit ___; lost receipt interval/count ___; restore artifact ___.
Pending dependencies/reason codes ___; first non-import receipt exists ___;
eligible rollback/recovery path ___; next action's exact owner go reference ___.

**Decision rule:** required evidence for the next action must be current and in
the appropriate environment, with no `NOT_RUN` or `BLOCKED` gate. An earlier
stage may be GO without authorizing a later one. Missing approval means NO-GO.
After a non-import receipt, never perform a simple Sheets rollback. Resolve
unknowns by receipt; obtain a new go for forward recovery. Subsequent releases
remain separate: one-tap → fast tasks → statements → assistant, each its own go.
