# V2-36: phone acceptance, written before the window

Status: **NOT_RUN**. Assaf must be present throughout the approved window.
This checklist is prepared before waves 4-5 land. It authorizes no deployment,
migration, role grant, Script change, import, authority switch or external write.
Use frozen `V2_PG_CONTRACTS.md` v1 §§0, 16, 18, 19 and V2-31's integrated
[CUTOVER_RUNBOOK.md](../../ops/assaf-os/CUTOVER_RUNBOOK.md) for commands.

## Before the window and the first production write

- [ ] [V2-35](OS_REHEARSAL_PLAN.md) passed end to end at the candidate release,
  including a real `mia restore`, broken-precondition abort, pre-write rollback,
  forward again, receipts, mirror readback and teardown evidence. A relevant
  release change invalidates that rehearsal until rerun.
- [ ] Exact CI-green backend/FE/Script revisions, independent HEAVY PASS,
  Cloudflare version/bundle hash, backup/restore evidence and rollback operator
  are recorded. Phone checklist and action plan are frozen before the window.
- [ ] Record iPhone model, OS/browser version, home-screen/Safari mode, network,
  timezone, backend SHA/epoch and frontend bundle. Assaf selects five genuine
  needed task changes below; no fabricated payments, clients or financial data.
- [ ] Agree RTO limit ___ seconds and RPO limit ___ seconds with Assaf **before**
  the window. Record restore drill date ___, measured RTO ___, measured RPO ___,
  backup recovery point ___ and lost receipt interval/count ___. RTO includes
  readiness, role checks and usable owner access; RPO is failure time minus the
  dump's consistent recovery point, with actual lost-write evidence. The contract
  requires a 15-minute backup cadence; it supplies no RTO acceptance number.
- [ ] In §19 production order: default-off integrated deploy including restore
  post-step; V2-14a demote; ledger migrations; provision + role validator; authority
  init; passed rehearsal; then wave-6 T1-T7. Pause jobs and take a verified,
  nonempty whole-DB backup before each production step. Verify checksum/catalog,
  excluded statement detail, backup age <1 h and drill age <7 days at activation.
- [ ] Cutover is **authority only**. Existing proposal/approve UX stays in use.
  One-tap, fast tasks, statements and assistant remain disabled for this window.
  Follow T1-T7, including warning-only protection on every archive tab, two
  post-fence xlsx captures and T7's third xlsx diff against #1, signed equal
  manifest, mirror readback and exact receipts. T6 is `mia settings` followed by
  `/health` SHA + epoch and readiness; there is no GCP VM "task" replacement.

**Pre-write rollback criteria:** stop before admitting the first non-import PG
receipt if any required preflight/machine check fails, captures drift, quarantine
or manifest mismatch remains, the mirror is unknown/conflicted, health SHA/epoch
disagrees, backup/drill is stale, a separate owner go is missing, or Assaf calls
stop. Retain freeze, prove zero non-import receipts and execute V2-31's rollback
procedure with Assaf's go. Core must be fenced before Sheets resumes; verify
the resulting authority and receipts. A retry requires fresh drain/fence/import
at the expected next epoch, not direct `fenced→active`.

**Point of no simple rollback:** a non-import PG receipt, including a rejected
decision, closes this boundary. Before the first phone write, check the boundary
and record Assaf's go to proceed. After it, a bad timing result or uncertain
write means pause/fence and resolve under V2-31's forward recovery/restore plan.
Never switch back to Sheets, delete receipts, or replay uncertain actions as a
performance remedy. Each restore is a separately approved recovery operation.

## Twenty real, sequential phone actions

Use the existing UI; opening/typing in a form is preparation, not a scored action.
Each table row is one measured action. Do not pool HTTP calls or `click_feedback`
into the action count. Wait for the authoritative outcome and visible state,
then export timings **after every row**. Before each new row, close the timings
sheet. This preserves evidence even with the 50-entry memory buffer.

| # | Real action Assaf performs | Scored V2-03 phase | Required outcome |
|---|---|---|---|
| 1 | Cold-open the signed-in OS from the home screen | `cold_initial_load` | Correct PG authority and visible data |
| 2 | Tap refresh on the main page | `subsequent_refresh` | Fresh complete snapshot |
| 3 | Submit a proposal for a genuinely needed task A | `proposal` | Exact intended proposal |
| 4 | Approve task A | `definitive_commit` | Definitive receipt matches intent |
| 5 | Refresh tasks | `subsequent_refresh` | Task A visible once |
| 6 | Submit a proposal for another needed task B | `proposal` | Exact intended proposal |
| 7 | Approve task B | `definitive_commit` | Receipt matches intent |
| 8 | Refresh tasks | `subsequent_refresh` | Task B visible once |
| 9 | Propose a genuine title correction on task C | `proposal` | Correct target/revision/diff |
| 10 | Approve that correction | `definitive_commit` | Matching receipt |
| 11 | Refresh tasks | `subsequent_refresh` | Corrected title visible |
| 12 | Propose a needed due-date change on task D | `proposal` | Correct target/date/revision |
| 13 | Approve that change | `definitive_commit` | Matching receipt |
| 14 | Refresh tasks | `subsequent_refresh` | Date change visible |
| 15 | Propose completing a task E actually finished | `proposal` | Correct target and status |
| 16 | Approve completion | `definitive_commit` | Matching receipt |
| 17 | Refresh tasks | `subsequent_refresh` | Correct final status |
| 18 | Open clients and tap refresh | `subsequent_refresh` | Existing client data correct |
| 19 | Open payments and tap refresh | `subsequent_refresh` | Existing receipts/currencies correct |
| 20 | Export row 19, close and cold-open OS again | `cold_initial_load` | Correct state survives restart |

If one of these task changes is not genuinely needed, choose another legitimate
task change **before** the window and record the substitution. Do not create
throwaway production data to meet a quota. For each write compare request,
decision, receipt, version and epoch with the accepted intent and fresh read;
verify the mirror catches up. Keep full IDs in a private evidence file, with
equality booleans/reason codes in the report. Stop on duplicate, conflicting,
unknown or missing effects. Do not auto-resend. More rows are allowed; retain all
attempts and the initial failed run rather than replacing slow/error samples.

## Export the actual V2-03 timing copy

1. After each action finishes, tap **זמני תגובה** in the refresh bar, then
   **העתקה**. Paste into a private local file named
   `phone-<run-id>-action-<NN>.txt`. If clipboard access is blocked, select/copy
   the read-only text field manually. Close the sheet before the next action.
2. The UI exports phase, milliseconds and bounded code only. Keep the original
   copy untouched. In a separate local action ledger record action number, export
   filename, phase line occurrence, `ms`, `code`, receipt-match boolean and visible
   outcome boolean. Mark the **new** phase occurrence since the previous export;
   copied windows overlap and must not be counted twice. Exports have no stable
   record IDs/timestamps; identical lines cannot be deduplicated by text alone.
   Sequential per-action export and occurrence mapping are mandatory.
3. The buffer holds at most 50 entries and is cleared on sign-out, owner-state
   clear or a 401. Export **before** closing/signing out and do not count stale
   entries from an earlier session. A missing/ambiguous phase is `BLOCKED`, not
   zero milliseconds. Repeat the full planned run after resolving capture issues.
4. `click_feedback` and `form_paint` are diagnostics, not completion latency.
   Request/server phase timings also are diagnostics: backend
   `auth/db/script/ledger/verify`, Worker `edge`. Do not sum those nested timings
   or substitute them for phone observations. The V2-03 `definitive_commit` phase
   ends at the decision/receipt outcome paint; the separately scored subsequent
   refresh proves visibility. No claim of approve-to-final-list latency is made
   by adding overlapping phases.

Checked against the local V2-03 release source:
`prototypes/assafweb-os-mobile/os-app.mjs` (`refreshBar`, `timingsSheet`,
`copyTimings`, `phaseOp`, `timedLoad`) and `V2_WAVE1_TIMING_CONTRACT.md`.
Recheck these entry points at the integrated frontend SHA before the window;
missing instrumentation blocks acceptance, not an excuse for a stopwatch-only
performance claim.

## Compute p95 and record acceptance

Create a private UTF-8 CSV `phone-actions.csv` with header
`action,phase,ms,code,receipt_match,visible_match` and exactly one mapped phase per
performed action. Use integer milliseconds; keep every planned row and every
extra attempt. First require at least 20 complete, genuine actions and all
expected outcomes. Unknown/error/missing samples fail acceptance even if a
success-only percentile would be fast.

Sort the action durations ascending as `x[0] ... x[n-1]` and use **nearest rank**:
`p95_ms = x[ceil(0.95 * n) - 1]`. For 20 actions this is the **19th** sorted
duration. Pass only if **p95_ms < 4000** (4000 itself fails). A minimal local
calculation, after validating the action ledger, is:

```python
import csv
import math
from pathlib import Path

rows = list(csv.DictReader(Path("phone-actions.csv").open(encoding="utf-8")))
assert len(rows) >= 20 and len({r["action"] for r in rows}) == len(rows)
assert all(r["code"] == "ok" and r["visible_match"] == "true" for r in rows)
assert all(r["receipt_match"] in {"true", "not_applicable"} for r in rows)
assert all(r["receipt_match"] == "true" for r in rows if r["phase"] == "definitive_commit")
values = sorted(int(r["ms"]) for r in rows)
assert all(ms >= 0 for ms in values)
p95_ms = values[math.ceil(0.95 * len(values)) - 1]
print({"n": len(values), "p95_ms": p95_ms, "latency_pass": p95_ms < 4000})
```

Report all-action p95 and, separately, phase counts/p95 for cold load, proposal,
definitive commit and refresh. Review slow critical phases with Assaf; do not
hide a slow commit with cheap UI feedback entries. Record measured restore
RTO/RPO versus the pre-agreed limits. GO requires truthful receipt/readback
results, the overall latency gate, accepted recovery measurements and explicit
phone acceptance. A local test run or desktop browser is not phone acceptance.

## Separate Assaf go for each production step

For **every** checkbox record Assaf's explicit statement, UTC, exact target,
release SHA/evidence reference and operator. Blank means no authorization.
Past approvals, the prepared checklist and a rehearsal PASS are not reusable go.

- [ ] GO: exact backend deployment (including default-off features).
- [ ] GO: exact frontend deployment (reader compatibility; no new UX enabled).
- [ ] GO: exact Apps Script deployment, if this release requires it.
- [ ] GO: V2-14a main-role demotion and runtime DSN/posture change.
- [ ] GO: V2-14/14b schema migration.
- [ ] GO: V2-15 role grants/provisioning and validator.
- [ ] GO: authority initialization and workspace/pepper pin.
- [ ] GO: drain/freeze and final reconcile in the approved window.
- [ ] GO: **Script Properties fence**, archive naming and tab warnings.
- [ ] GO: **import promotion** of the exact signed manifest.
- [ ] GO: **authority activation** at the recorded epoch.
- [ ] GO: **backend switch** to postgres via settings, then health SHA/epoch check.
- [ ] GO: **enable mirror** to the pinned new mirror workbook.
- [ ] GO: admit the first non-import PG write and perform the planned phone actions.
- [ ] GO: phone acceptance including p95 and restore RTO/RPO.
- [ ] GO: **remove the old workbook's `reconcileAssafOsEdit` onEdit trigger**, only
  after acceptance. Record exact trigger ID, project and readback of its absence;
  keep the old workbook fenced/archived. Do not delete unrelated triggers.

## Subsequent feature releases (one per deployment and go)

After authority-only acceptance, each exact CI-green release needs its own
previewed change, Assaf go, deploy evidence and acceptance before the next:

1. V2-24 + V2-32: one-tap and undo. Separate GO ___ / deployment ___ / acceptance ___.
2. V2-33: fast tasks. Separate GO ___ / deployment ___ / acceptance ___.
3. V2-26 + V2-27: statements. Separate GO ___ / deployment ___ / acceptance ___.
4. V2-37/38/39: assistant access and connections. Separate GO ___ / deployment ___ / acceptance ___.

No island feature: V2-34 remains `NOT_INCLUDED`. If the December 20 cycle is
missed, bring FORK-ISLAND back to Assaf's go/no-go rather than enabling it here.
