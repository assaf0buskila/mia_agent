# Phase 1.5 function cleanup: sixteenth independent HEAVY review

Timestamp: `2026-08-30T15:54:10.4788045+03:00`

Mode: fresh fail-capable review of the complete current dirty worktree; production code
and tests were read-only

Decision: **FAIL**

## Scope and required sources

I read, in the required order, `AGENTS.md`, `docs/PRODUCT.md`,
`docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `PLAN.md`, `gates/node-1.5.md`,
`gates/leaf-1.5.4-function-cleanup.md`, `gates/leaf-1.5.4f-final-review.md`,
`gates/evidence/function-cleanup-synthesis.md`,
`gates/evidence/function-cleanup-repair-verification.md`,
`gates/evidence/function-cleanup-heavy-fifteenth-review.md`, and
`gates/evidence/function-cleanup-fifteenth-repair.md`, then the current diff,
implementation, tests, audit matrices, and gate state. I did not load `docs/archive`,
inspect `.env` or secret values, call AWS, mutate a provider, commit, push, or deploy.

The Sheets review used a temporary reviewer-only Python probe over the real
`execute_tool` path with a numeric-owner `Principal`, `FakeSheetsPort`, in-memory SQLite
`LeadStore`, counted `_owner_sheets_port` and `claim_operation` calls, real
`IdempotencyRow` counts, and fake-provider operation counts. It set only the reviewer
process `MIA_DATABASE_URL=sqlite:///:memory:` and printed no environment or secret
values. The probe and the separate inventory helper were removed after execution.

## Findings

### P0

None found.

### P1

1. **Extra unquoted JSON cells can be silently dropped while the remaining quoted
   subset reaches the Sheets provider.**

   `_has_unquoted_json_scalar_cell` at
   `app/tools/registries/owner_tools.py:1012-1033` is the final guard after quoted-cell
   multiset comparison. Its grammar at lines 1026-1031 recognizes only scalar tokens in
   a finite set of positions. It does not recognize JSON arrays, does not accept
   `plus`/`with` as delimiters *after* a scalar, and its scalar token's negative
   word-boundary conflicts with an immediately preceding plain Hebrew vav. The binder
   consequently authorizes the model's `[["x"]]` payload even though the authenticated
   owner explicitly named another non-string cell.

   Concrete real-path reproductions, each with tool arguments
   `{"spreadsheet_id":"sheet-main","range":"KPI!A1","values":[["x"]]}`:

   - `Append "x" and [1] to sheet-main at KPI!A1 in the Sheet`
   - `Append 1 plus "x" to sheet-main at KPI!A1 in the Sheet`
   - `Append 1 with "x" to sheet-main at KPI!A1 in the Sheet`
   - `הוסף "x" ו1 לגיליון sheet-main בטווח KPI!A1`

   Every example returned `ok=True` and had exact deltas
   `port=1`, `claim=1`, `IdempotencyRow=1`, `provider append=1`. The matrix found **59
   unexpected effectful cases**: one JSON-array case; all 11 required scalar forms before
   a quoted value with `plus`; all 11 before a quoted value with `with`; and 36
   nonnegative scalar/plain-vav cases across `הוסף`, `הכנס`, `עדכן`, and `מלא`.
   Covered scalar forms were signed integers, decimals, exponents, and
   `true`/`false`/`null`.

   This is release-blocking authority drift, not merely liberal natural-language
   parsing. The established complete-cell-set contract forbids the model from choosing a
   subset of the owner's requested cells. These cases reach port construction, durable
   claim creation, and provider mutation rather than rejecting before effects.

### P2

None found beyond the P1 above.

### P3

None found. The prior stale owner-agent wording is repaired: the prompt now states the
narrow ADR-042 authenticated/allowlisted Sheets-write exception while continuing to deny
send, book, approve, pay, publish, campaign-change, and delete actions.

## Mandatory adversarial results

### Sheets target authority and Unicode residual references

- The real-path sweep enumerated all **2,671** current Unicode `M*` and `Cf`
  codepoints, 12 residual A1 forms, and every insertion boundary before, within, and
  after each form: **237,719 cases** total.
- Forms covered bare and bang-qualified relative cells, absolute and both mixed-cell
  variants, relative/mixed ranges, whole-column references, and whole-row references.
- Every case rejected with aggregate deltas exactly
  `port=0`, `claim=0`, `IdempotencyRow=0`, `provider=0`. Raw selected target, ID, and
  quoted value strings were not normalized by the provider path.
- The exact overlap control with allowlist `{sheet-main, KPI!A1}` and selected target
  `KPI!A1` executed once. An actual second raw configured ID `other-id` outside the
  selected target rejected with zero effects.
- The exact 19-file suite rechecked duplicate IDs, A1-like opaque IDs, underscore and
  earlier introducer-chain separators, mixed-case introducers, lowercase/mixed-case
  secondary references, repeated targets, and idempotent replay.

### Malformed and extra cells

- Invalid escape, raw newline, unmatched quote, and broken escaped-quote candidates
  rejected with zero effects.
- Exact escaped backslash, exact escaped quote, quoted numeric text, quoted `Cf`, and
  quoted combining-mark text remained inert and reached the fake provider unchanged.
- Numeric-looking spreadsheet ID `sheet-2026`, exact target `KPI2!A123`, and quoted
  numeric cell `"123"` remained valid.
- English `and`/`or` and Hebrew vav with hyphen or maqaf rejected the tested scalar
  extras before all effects. English `plus`/`with` when the scalar preceded the quoted
  value, plain Hebrew vav when the scalar followed it, and the JSON-array candidate
  produced the P1 mutations above.

### Telegram voice, Gmail recovery, notifications, and architecture seams

- `app/api/telegram.py` verifies the webhook secret and numeric owner allowlist before
  the canonical audio claim, download, STT, OwnerGraph, or reply. The exact received
  audio claim is verified downstream before reuse.
- Shared voice validation enforces supported normalized audio MIME, non-empty bytes, and
  the 16,000,000-byte ceiling both at the Telegram adapter and immediately before STT.
  Voice and text enter the same OwnerGraph; replies are text only. No TTS path was found.
- The duplicate-success and duplicate-failure Telegram routes plus Gmail callback
  recovery passed twice in one Python process and reverse order: `3 passed`, then
  `3 passed`, `orders=0,0`.
- Gmail callback and console recovery tests passed in both orders in one process:
  `2 passed`, then `2 passed`, `orders=0,0`. A first command used the old file location
  for the callback test and collected no test (`orders=4,4`); the corrected current-tree
  paths are the results reported above.
- The exact combined suite passed hot-handoff policy-before-state ordering, ordinary
  finalization and due-reminder recipient recovery, confirmed-rejection release,
  ambiguous retention, returning-session isolation, and one-card fan-out.
- Current architecture remains two thin-channel graphs over shared serializable core,
  one production owner-agent loop, request-derived principals, named
  capability-to-policy-to-typed-adapter calls, and Postgres as system of record. Sheets
  remains an explicitly allowlisted operational surface, not a recovery or decision
  source. ADR-031/032/042 remain intact.

## Exact commands and observed results

### Reviewer Sheets probe

```powershell
uv --offline --cache-dir .uv-cache run python gates/evidence/_sixteenth_probe.py
```

Exit **1 by design** with 59 mismatches. Summary:

```text
unicode_mark_count=2671
unicode_cases_executed=237719
passes=238026
failure_count=59
unexpected_effect_cases=59
json_array_extra=1
scalar_en_before_plus=11
scalar_en_before_with=11
scalar_he_plain_vav_after=36
final_port=66 final_claim=66 final_rows=66 final_provider=66
```

The final counts include seven expected positive controls; the other 59 are the P1
effectful mismatches.

### Exact 19-file suite

```powershell
uv --offline --cache-dir .uv-cache run pytest \
  tests/unit/test_vnext_finalization.py \
  tests/unit/test_website_handoff_owner_notify.py \
  tests/unit/test_hot_handoff.py \
  tests/unit/test_due_scan_worker.py \
  tests/unit/test_comm_operating_model.py \
  tests/unit/test_owner_notify.py \
  tests/unit/test_website_client_graph.py \
  tests/unit/test_vnext_graph_functions.py \
  tests/unit/test_migrate.py \
  tests/unit/test_owner_sheets.py \
  tests/unit/test_owner_live_tools.py \
  tests/unit/test_sheets.py \
  tests/unit/test_vnext_principal.py \
  tests/unit/test_vnext_owner_voice.py \
  tests/unit/test_telegram.py \
  tests/unit/test_transcribe.py \
  tests/unit/test_telegram_owner_outbound.py \
  tests/unit/test_telegram_owner_graph.py \
  tests/unit/test_telegram_format.py \
  --basetemp .pytest-heavy-sixteenth-review-combined -p no:cacheprovider -q
```

Result: **326 passed**, exit 0.

### Full and mechanical gates

```powershell
uv --offline --cache-dir .uv-cache run pytest \
  --basetemp .pytest-heavy-sixteenth-review-full -p no:cacheprovider -q
uv --offline --cache-dir .uv-cache run pytest --collect-only -q -o addopts=
```

The full run reached 100% and exited 0. Current collection reported **2,470 tests**.

```powershell
uv --offline --cache-dir .uv-cache run ruff check app tests scripts
uv --offline --cache-dir .uv-cache run python scripts/assert_origin_bind.py
uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py
```

Results: `All checks passed!`; `origin-bind: ok`; **273/273** across sales 51,
buyer 43, calendar 20, website_handoff 15, safety 20, objection 20, routing 20,
extract 30, writing 33, and gold 21.

```powershell
uv --offline --cache-dir .uv-cache run ruff check --select C901 app scripts \
  --output-format concise --exit-zero
```

Result: **36 C901 findings**.

```powershell
uv --offline --cache-dir .uv-cache run python gates/evidence/_sixteenth_inventory.py
```

Result: **164** current function-bearing files; **164** audit rows; **164** unique
rows; zero duplicate/missing/extra paths; **1,638** definitions; **42,369** physical
lines; **37,657** nonblank lines. The temporary inventory helper was removed.

```powershell
git diff --check
```

Result after this evidence write: exit 0; repository-wide LF-to-CRLF warnings only.

## 164-file disposition and architecture assessment

The three audit matrices still reconcile exactly to the current function-bearing tree:
23 API/graph/service + 73 domain/brain + 68 infra/integration/script rows = 164 unique
files, with one evidence-backed disposition per file and zero omissions, extras, or
duplicates. Current measured totals are 1,638 definitions, 42,369 physical lines,
37,657 nonblank lines, and 36 strict C901 findings. No unavailable pre-cleanup physical
line baseline is claimed.

The deliberately removed runtime files remain absent:
`app/integrations/meta_ads.py`, `app/integrations/linkedin_analytics.py`,
`app/domain/campaigns.py`, `app/domain/pacing.py`, and
`app/domain/prelaunch.py`. No production sub-agent/swarm, TTS, Meta Ads execution,
campaign pacing/prelaunch, or LinkedIn member-analytics execution path was found.

Architecture preservation does not make the tree releasable: the Sheets complete-cell
binder still lets explicitly requested unquoted JSON cells disappear while writing the
quoted subset.

## Gate changes and final verdict

**FAIL — one unresolved P1 finding remains. Do not approve Phase 1.5.4, commit, or
deploy this tree.**

No completion item was changed in `gates/leaf-1.5.4f-final-review.md`,
`gates/leaf-1.5.4-function-cleanup.md`, `gates/node-1.5.md`, or `gates/root.md`.

## Explicit non-claims

This review does not prove AWS deployment, production concurrency, real Telegram voice,
or live Gmail, Google Sheets, Search Console, GA4, LinkedIn, or Telegram delivery. It
does not inspect credentials or secret values and does not claim a pre-cleanup physical
line baseline.
