# Phase 1.5 clean-room HEAVY fifth outcome review

Date: 2026-08-28
Mode: independent outcome review; only this evidence file was edited
Verdict: **FAIL**

The full mechanical gate is green and every requested inventory/complexity number
reconciles. The outcome gate is not green: two current P1 Sheets authorization defects
remain outside the maintained regression suite. No additional P0/P1/P2 was found in
notification upgrade/retry semantics, empty-session binding, HANDOFF, Gmail callbacks,
principal isolation, voice, owner provider wiring, minimality, or migration packaging.

## Review ledger and four passes

- [x] R1: Required contracts, four historical reviews, synthesis, repair evidence,
  current diff, relevant code/tests/migration, and all three audit matrices were read.
  EVIDENCE: AGENTS, PRODUCT, ARCHITECTURE, DECISIONS, PLAN, root/1.5/1.5.4f gates,
  four earlier HEAVY reviews, synthesis/repair evidence, 23 + 73 + 68 audit rows, and
  current production/test/migration/diff surfaces were independently read.
- [x] R2: Mandatory Sheets, notification, migration-upgrade, empty-session, HANDOFF,
  Gmail, principal, voice, provider-wiring, and minimality probes were executed.
  EVIDENCE: direct Sheet probes found the two blockers below; 18 selected adversarial
  cases covering the other transition families passed; the 20-path suite passed 272.
- [x] R3: Complete pytest, Ruff, origin binding, evals, strict C901, inventory/matrix
  reconciliation, migration packaging/application, and diff-check were remeasured.
  EVIDENCE: exact results appear below.
- [x] R4: Final adversarial reread was able to fail and recorded exact reproducers.
  EVIDENCE: both failures reached port construction, operation claim, and fake provider
  mutation. Phase 1.5 parent gates remain open.

No `.env` or secret value was inspected. No live provider/network call, AWS mutation,
deployment, production/test/migration/application edit, or destructive command occurred.

## Blocking findings

### P1 - two complete Sheet targets let the model choose one

`app/tools/registries/owner_tools.py:843-861` proves only that the model-selected
spreadsheet id and A1 range each occur as complete tokens. It does not prove that they
are the unique requested target pair or reject a second complete target. An ambiguous
owner instruction can therefore authorize the model's document/range choice.

Real owner registry + real `LeadStore` + `FakeSheetsPort`, with both ids allowlisted and
counters around `_owner_sheets_port` and `claim_operation`:

```text
owner: Please append "x" to sheet-allowed at KPI!A1 or sheet-other at KPI!B1 in the Sheet
tool:  sheets_append(sheet-allowed, KPI!A1, [["x"]])

{'ok': True, 'delta_port': 1, 'delta_claim': 1,
 'delta_ops': 1, 'delta_rows': 1}
provider operation:
('append', 'sheet-allowed', 'KPI!A1', [['x']])
```

The private binding result independently reconciled the boundary:

```text
{'quoted_target': False, 'exact_one_target': True,
 'two_targets_one_payload': True}
```

Quoted id/range syntax fails closed, but two unquoted complete target pairs do not. This
violates exact id/range binding and ambiguous-target rejection before port/claim.

Required repair: bind exactly one spreadsheet-id/range pair from the authenticated
current turn. Any second complete candidate target must fail before adapter construction
and claim. Add the exact regression above plus same-id/two-range, two-id/same-range, and
quoted-target cases while retaining complete-token matching.

### P1 - whitespace-only literal normalizes to an empty Sheet write

`app/tools/registries/owner_tools.py:864-877` rejects only a raw falsey cell, so a
whitespace-only string is considered bound. `app/integrations/sheets.py:183-199` then
strips the cell and appends the resulting empty string without rejecting it. The
mandatory empty-value boundary is bypassed before port/claim.

Same real-registry/real-store probe:

```text
owner: Please append "   " to sheet-allowed at KPI!A1 in the Sheet
tool:  sheets_append(sheet-allowed, KPI!A1, [["   "]])

{'ok': True, 'delta_port': 1, 'delta_claim': 1,
 'delta_ops': 1, 'delta_rows': 1}
provider operation:
('append', 'sheet-allowed', 'KPI!A1', [['']])
```

Required repair: reject any cell whose normalized value is empty in shared pure
prevalidation, proving port, claim, idempotency rows, and provider operations untouched.
Preserve literal spaces inside non-empty values.

## Objective-by-objective outcome

| Objective | Verdict | Current-tree evidence |
| --- | --- | --- |
| Sheets operation/id/range/value multiplicity and ambiguity | **FAIL** | Required EN negations (`do not`, `don't`, `don’t`, `dont`) and eight natural HE forms rejected with zero effects. Conflicting operations, subset/superset/duplicate multiplicity, quoted targets, token collisions, and corrected replay passed. Direct two-target and whitespace-empty probes still mutate. |
| Sheets allowlist, A1, caps, shape, formula/empty, kill, principal ordering | **FAIL** | Allowlist/reversed-A1/caps/shape/formula/empty-string/kill/client-principal tests reject pre-port/pre-claim; RAW/idempotency pass. Whitespace-normalized empty bypasses the boundary. |
| Finalization/due recipient state | PASS (local) | No-config creates no recipient claim; accepted/ambiguous remain; explicit rejection releases only that owner; same-session/day retry sends only missing owner; keys are conversation id/local day. |
| Notification legacy upgrade | PASS (local) | Exact legacy finalization claim blocks resend/new recipient claim; a new conversation sends; legacy due claim blocks same local day but not later; no fabricated recipient backfill. |
| Empty returning website session | PASS (local) | Message predicate and inactivity aggregation bind lead plus conversation. Empty B is absent and creates no claim/transport; B with its own message remains eligible. |
| Hot HANDOFF | PASS (local) | Kill precedes all effects. Per-owner partial retry, ambiguity retention, success dedupe, and one-card ClientGraph return passed. |
| Gmail callback | PASS (local) | Numeric auth precedes Gmail construction; binding/expiry/risk/write/demo/kill remain; deferred/known-failed sends recover once and completed sends dedupe. |
| Unauthorized owner/principal isolation | PASS | Empty/unauthorized batches return before settings/builders; owner mint requires numeric membership; ClientGraph rejects owner principal; AST guard passes. |
| Telegram voice/runtime shape | PASS (local) | Numeric auth precedes download/STT; transcript enters the same OwnerGraph text path; no TTS implementation found; one bounded `run_owner_agent` loop remains. |
| GA4/GSC/LinkedIn/Sheets wiring | PASS except blockers | Owner-only named capabilities, typed adapters, and normalized KPI/profile projections are wired. GA4/GSC/LinkedIn remain read/profile-only; Sheets remains RAW/explicit-id scoped. |
| Minimality/removal/architecture | PASS | No definitions remain for dead symbols, rejected Sheet discovery helpers, duplicate count method, knowledge lookup, plaintext deploy helper, or legacy service scan. WhatsApp Composio sender remains; two graphs and one runtime owner agent remain. |
| Migration packaging/order/application/conflict | PASS (local) | Docker copies migrations; `mia-migrate`/ECS command are pinned; 37 SQL files sorted; target index 36/last, portable/recorded; SQLite creates five columns; PostgreSQL compiles one `ON CONFLICT DO NOTHING`. |
| 164 matrix and metrics | PASS | 164 files = 164 unique rows, 23/73/68, no missing/extra/duplicate; 139 KEEP / 24 SIMPLIFY / 1 MERGE. |
| Mechanical regression | PASS | Exact results below. |

## Exact commands and measurements

All Python commands used `uv --offline --cache-dir .uv-cache`; pytest used local
`--basetemp`, disabled cache provider, and `MIA_DATABASE_URL=sqlite:///:memory:`.

1. Enumerated 20-path focused suite (Sheets, Gmail/callback, Telegram,
   finalization/due/HANDOFF, principals/voice, GA4/GSC/LinkedIn, scripts/migration):

   ```powershell
   uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider --basetemp .pytest-heavy-fifth-focused-20260828 tests/unit/test_owner_live_tools.py tests/unit/test_owner_sheets.py tests/unit/test_sheets.py tests/unit/test_gmail.py tests/unit/test_owner_gmail_console.py tests/unit/test_telegram.py tests/unit/test_telegram_owner_graph.py tests/unit/test_vnext_finalization.py tests/unit/test_hot_handoff.py tests/unit/test_website_handoff_owner_notify.py tests/unit/test_due_scan_worker.py tests/unit/test_vnext_principal.py tests/unit/test_vnext_owner_voice.py tests/unit/test_vnext_client_voice.py tests/unit/test_ga4.py tests/unit/test_search_console.py tests/unit/test_linkedin.py tests/unit/test_deploy_secret_box.py tests/unit/test_scripts.py tests/unit/test_migrate.py
   ```

   Result: **272 passed, 119 warnings in 23.03s**. Retained evidence did not preserve
   an exact 19-path command for the parent-reported 287 count, so this review does not
   relabel a different enumerated command as 287.

2. Selected mandatory transitions: **18 case invocations passed** including
   parametrization; command wall time was **6.8s**.

3. Complete tree: `uv ... run pytest -p no:cacheprovider --basetemp
   .pytest-heavy-fifth-full-20260828` -> **2,429 passed, 1,856 warnings in 80.16s**.

4. `uv ... run ruff check app tests scripts` -> **All checks passed**.

5. `uv ... run python scripts/assert_origin_bind.py` -> **origin-bind: ok**.

6. `uv ... run python scripts/eval_diff.py` -> **273/273**: sales 51, buyer 43,
   calendar 20, website_handoff 15, safety 20, objection 20, routing 20, extract 30,
   writing 33, gold 21.

7. `uv ... run ruff check app scripts --select C901 --output-format concise` ->
   expected measurement exit 1, **37 findings**.

8. Independent AST/physical/matrix reconciliation:

   ```text
   function-bearing files  164
   definition lines        1,630
   physical lines          42,144
   non-blank lines         37,453
   matrix rows/unique      164/164
   missing/extra/duplicate 0/0/0
   partition               23 API / 73 domain / 68 infra
   dispositions            139 KEEP / 24 SIMPLIFY / 1 MERGE
   ```

9. Migration: 37 sorted files; target index 36/last; portable; failed empty; recorded;
   columns `kind, lead_id, notification_key, recipient_id, claimed_at`; PostgreSQL
   compiles `INSERT ... ON CONFLICT DO NOTHING`.

10. `git diff --check`: exit **0** after this evidence edit; line-ending warnings only.

## Decision, non-claims, and files edited

**FAIL - leave final-review G4, node 1.5 G4/G5, and root G7 open.** Passing
2,429 tests and every mechanical gate does not supersede two direct provider-boundary
mutations absent from those tests.

This review does not claim live Telegram/Gmail/Sheets/GA4/GSC/LinkedIn behavior, live
PostgreSQL concurrency, an applied production migration, AWS/deployment state,
real-device voice latency/barge-in, or a pre-cleanup physical/nonblank reduction.

Exact files edited: `gates/evidence/function-cleanup-heavy-fifth-review.md` only.
