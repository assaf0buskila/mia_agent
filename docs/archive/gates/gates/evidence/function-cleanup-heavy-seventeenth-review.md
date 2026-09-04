# Function cleanup HEAVY seventeenth review

- Date: 2026-08-30T16:26:13.8657367+03:00
- Reviewer role: fresh independent HEAVY verifier after review 16 and its repair/follow-up; this reviewer did not implement the tree.
- Verdict: **FAIL**
- Release decision: Phase 1.5 cannot close because two independently reproduced P1 defects remain at the real Sheets `execute_tool` mutation boundary. No P0 or P2 finding was found.

## Scope and sources

The complete current dirty tree was reviewed read-only except for temporary reviewer probes and this evidence file. Existing dirty work was preserved. Production code and tests were not edited.

Mandatory sources were read fully and in order:

1. `AGENTS.md`
2. `docs/PRODUCT.md`
3. `docs/ARCHITECTURE.md`
4. `docs/DECISIONS.md`
5. `PLAN.md`
6. `gates/node-1.5.md`
7. `gates/leaf-1.5.4-function-cleanup.md`
8. `gates/leaf-1.5.4f-final-review.md`
9. `gates/evidence/function-cleanup-synthesis.md`
10. `gates/evidence/function-cleanup-repair-verification.md`
11. `gates/evidence/function-cleanup-heavy-sixteenth-review.md`
12. `gates/evidence/function-cleanup-sixteenth-repair.md`
13. Current code, tests, dirty-tree diff, and all three matrices: `function-audit-api.md`, `function-audit-domain.md`, and `function-audit-infra.md`.

`docs/archive/` and `.env` were not opened. No secret value was inspected or emitted.

## Findings

### P0

None found.

### P1-1: punctuation between an introducer and an extra cell bypasses complete-cell authority

- Location: `app/tools/registries/owner_tools.py:1013-1058`, especially `_SHEETS_CELL_PREFIX_RE` and `_has_unquoted_json_cell`.
- Cause: the finite prefix grammar requires the connector or Hebrew vav to be adjacent to the JSON-looking candidate. Punctuation, brackets, or spaces between them can prevent the extra candidate from being recognized. The authoritative owner text therefore contains an additional requested non-string or malformed cell, but the model-provided quoted subset is accepted and mutated.
- Boundary under test: real `execute_tool`, numeric Telegram owner principal, real `LeadStore`, in-memory SQLite `IdempotencyRow`, counted Sheets-port construction, counted operation claim, and a fake provider.
- Representative reproductions:
  - Owner: `Append "x" and: 1 to sheet-main at KPI!A1 in the Sheet`; tool values: `[["x"]]`.
  - Owner: `Append "x" plus (1) to sheet-main at KPI!A1 in the Sheet`; tool values: `[["x"]]`.
  - Owner: `Append "x" with / 1 to sheet-main at KPI!A1 in the Sheet`; tool values: `[["x"]]`.
  - Owner: `Append "x" or — 1 to sheet-main at KPI!A1 in the Sheet`; tool values: `[["x"]]`.
  - Owner: `Append "x" and: [1] to sheet-main at KPI!A1 in the Sheet`; tool values: `[["x"]]`.
  - Malformed signed candidates `+true`, `--1`, and `-null` likewise bypassed when placed after these punctuation variants.
  - Hebrew examples: `הוסף "x" ו - 1 ...`, `הוסף "x" ו ־ 1 ...`, `הוסף "x" ו:1 ...`, and `הוסף "x" ו([1]) ...`.
  - The `and: [1]` form reproduced for every tested English write verb (`Append`, `add`, `Update`, `fill`, `enter`) and every tested Hebrew write verb (`הוסף`, `הכנס`, `עדכן`, `מלא`).
- Effect evidence: 24/24 bypass cases returned success. Every case had the exact effect delta `(port construction, operation claim, idempotency row, provider mutation) = (1, 1, 1, 1)`. Required behavior was rejection before every effect.
- Impact: the model can silently discard an owner-requested cell and still perform a narrower Sheets mutation. This violates ADR-042's narrow-write authority binding and the complete-cell contract.

### P1-2: quoted-cell Counter comparison ignores positional order and two-dimensional layout

- Location: `app/tools/registries/owner_tools.py:884-891`, especially `return quoted_cells == Counter(cells)` at line 890.
- Cause: `Counter` proves only value multiplicity. It does not bind the positional order of cells or the row/column layout chosen by the model.
- Boundary under test: the same real `execute_tool` path, counted port and claim, real idempotency row, and fake-provider mutation.
- Reproductions:
  - Owner: `Append "first" and "second" to sheet-main at KPI!A1:B1 in the Sheet`; tool values: `[["second", "first"]]`.
  - Owner: `Update "a", "b", "c", "d" to sheet-main at KPI!A1:B2 in the Sheet`; tool values: `[["a", "c"], ["b", "d"]]`.
- Effect evidence: both cases returned success; each had exact effect delta `(1, 1, 1, 1)`. A duplicate-multiplicity mismatch control rejected, confirming that multiplicity is checked while order/layout is not.
- Impact: the model may reorder values or select an owner-unauthorized grid layout while satisfying the same multiset. Exact owner-approved cell placement is not preserved.

### P2

None found beyond the two P1 release blockers above.

### P3

No separate P3 finding recorded.

## Adversarial Sheets matrix

The main real-path probe executed 320 cases. Its final output was:

```text
cases=320 failures=24 unexpected_effects=24
final_counts=port:33 claim:33 rows:33 provider:33
```

The 24 failures are the effectful P1-1/P1-2 cases described above; nine positive controls account for the other nine successful effects.

Coverage independently reproduced review-16 cases and broadened them across:

- signed integers, decimals, exponents, `true`, `false`, and `null`;
- valid and malformed arrays/objects, including nested and quoted members;
- candidates before and after quoted cells;
- English `and`, `or`, `plus`, `with`, comma, and semicolon separators;
- every enumerated English and Hebrew update/append verb;
- Hebrew plain vav, hyphen, and maqaf;
- whitespace, newline, and punctuation variants;
- malformed quoted candidates, invalid escapes, raw newline, unmatched/escaped quotes, and backslashes;
- exact value multiset, duplicates, and order;
- numeric-looking spreadsheet ID, A1-like target, quoted numeric text, allowlisted ID equal to selected range, a second ID outside the target, and duplicate/A1-like opaque IDs;
- repeated/mixed English/Hebrew introducers and separator classes.

Literal scalar/container-looking text remained inert inside valid quoted strings. Positive controls preserved exact raw values for quoted `true`, `[1]`, `{"a":1}`, `123`, escaped backslashes, and escaped quotes. Positive controls also preserved an overlapping allowlisted ID equal to the selected range (`KPI!A1`), numeric-looking ID `sheet-2026`, and A1-like opaque ID `opaque-A1` without normalization.

A separate exhaustive Unicode/A1 probe enumerated every current Unicode `M*` and `Cf` code point at every boundary of 12 secondary A1 forms, including bare/tab-qualified relative, absolute, mixed, range, whole-row, and whole-column forms:

```text
mark_count=2671 cases=192312 unexpected=0 port=0 claim=0 rows=0 provider=0
```

All 192,312 such secondary-target cases rejected before every effect. Raw selected IDs, valid targets, and quoted values otherwise remained exact and unnormalized in the positive controls.

## Telegram voice, Gmail, notification, and finalization assessment

Current code and focused behavioral tests support these conclusions, with no additional P0-P2 finding:

- Telegram verifies the webhook secret and numeric owner ID before media access. Canonical dedupe/claim occurs before download, transcription, graph invocation, and reply.
- MIME support, nonempty content, and the 16,000,000-byte limit are enforced at both the Telegram adapter and transcription boundary.
- Voice and text use the same `OwnerGraph`; successful voice handling sends a text reply only and has no TTS path.
- Retried voice success and transcription failure preserve one visible reply/effect ordering.
- Gmail callback and console recovery work in both tested orders. Completed sends dedupe; deferred/failed sends remain retryable, and failed sends release the operation.
- Handoff/finalization applies policy before state, persists recipient delivery state, preserves ambiguous recipients for retry, releases only confirmed rejection, and keeps inbox notification independent.

Focused order probes passed in both order A and reverse order B for:

```text
tests/unit/test_telegram.py::test_retried_voice_success_claims_before_download_stt_graph_and_reply
tests/unit/test_telegram.py::test_retried_voice_transcription_failure_sends_one_visible_reply
tests/unit/test_telegram_owner_outbound.py::test_gmail_callback_recovers_deferred_and_failed_send_once
tests/unit/test_owner_gmail_console.py::test_approved_gmail_send_deferrals_remain_retryable
```

Each ordering run reported `4 passed`.

## Architecture and disposition assessment

- The production topology remains one owner-agent loop with two thin graphs (`OwnerGraph` for Telegram and `ClientGraph` for website), consistent with ADR-031/032.
- The path remains capability -> policy -> typed adapter; principals are minted from request/channel facts rather than model claims.
- Postgres remains the system of record. Sheets is a narrow operational write surface, not a system of record. The two P1 findings are precisely failures inside the ADR-042 narrow-write binding, not an alternate SoR.
- Removed campaigns, pacing, prelaunch, Meta-paid, and LinkedIn-analytics runtime modules remain absent. Searches found only historical ADR/status/dataset or test references, not a live removed runtime path.
- Explicitly checked absent production files: `app/integrations/meta_ads.py`, `app/integrations/linkedin_analytics.py`, `app/domain/campaigns.py`, `app/domain/pacing.py`, and `app/domain/prelaunch.py`.
- The `.gitignore` additions `.pytest-*/` and `.heavy-review-*/` are mechanical reviewer-artifact cleanup. A repository-only `check-ignore` probe matched those temporary-directory shapes and did not match `app/tools/registries/owner_tools.py` or `gates/evidence/function-cleanup-repair-verification.md`; the additions do not hide source or evidence.

All three current audit matrices reconcile exactly:

```text
function_files=164
audit_rows=164 unique_audit_rows=164
definitions=1638 physical=42392 nonblank=37679
duplicates=[]
missing=[]
extra=[]
```

The 164-file disposition is exhaustive and mechanically one-to-one. Whole-tree C901 independently reports 36 findings, matching the stated inventory. The disposition ledger is therefore reconciled, but reconciliation does not override the two live P1 boundary failures.

## Exact mechanical commands and results

### Exact 19-file cleanup suite

```powershell
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_vnext_finalization.py tests/unit/test_website_handoff_owner_notify.py tests/unit/test_hot_handoff.py tests/unit/test_due_scan_worker.py tests/unit/test_comm_operating_model.py tests/unit/test_owner_notify.py tests/unit/test_website_client_graph.py tests/unit/test_vnext_graph_functions.py tests/unit/test_migrate.py tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py tests/unit/test_sheets.py tests/unit/test_vnext_principal.py tests/unit/test_vnext_owner_voice.py tests/unit/test_telegram.py tests/unit/test_transcribe.py tests/unit/test_telegram_owner_outbound.py tests/unit/test_telegram_owner_graph.py tests/unit/test_telegram_format.py --basetemp .pytest-heavy-seventeenth-review-combined -p no:cacheprovider -q
```

Result: exit 0; 329 tests passed.

### Full pytest

```powershell
uv --offline --cache-dir .uv-cache run pytest --basetemp .pytest-heavy-seventeenth-review-full -p no:cacheprovider -q --disable-warnings
```

Result: exit 0; progress reached 100%. Independent collection count:

```powershell
uv --offline --cache-dir .uv-cache run pytest --collect-only -q -o addopts= | Select-Object -Last 5
```

Result: `2471 tests collected in 2.71s`.

### Ruff

```powershell
uv --offline --cache-dir .uv-cache run ruff check app tests scripts
```

Result: exit 0; `All checks passed!`

```powershell
uv --offline --cache-dir .uv-cache run ruff check --select C901 app scripts --output-format concise --exit-zero
```

Result: `Found 36 errors.` This is the expected inventory count, not a clean-complexity claim.

### Origin binding

```powershell
uv --offline --cache-dir .uv-cache run python scripts/assert_origin_bind.py
```

Result: exit 0; `origin-bind: ok`.

### Evaluation diff

```powershell
uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py
```

Result: exit 0; 273/273 total across sales 51, buyer 43, calendar 20, website handoff 15, safety 20, objection 20, routing 20, extract 30, writing 33, and gold 21.

### Inventory reconciliation

```powershell
uv --offline --cache-dir .uv-cache run python gates/evidence/_seventeenth_inventory.py
```

Result: 164 files/164 unique rows; 1,638 definitions; 42,392 physical lines; 37,679 nonblank lines; no duplicate, missing, or extra audit row. The temporary reviewer script was removed after recording its output.

### Diff hygiene

```powershell
git diff --check
```

Result: exit 0; only working-tree LF-to-CRLF conversion warnings were emitted.

## Gate changes and non-claims

- Gate changes: **none**. Because the verdict is FAIL, no independent-review or completion item was checked in `gates/leaf-1.5.4f-final-review.md`, `gates/leaf-1.5.4-function-cleanup.md`, `gates/node-1.5.md`, or `gates/root.md`.
- No live G2/G3, deployment, AWS, or live-provider gate was marked or claimed.
- No AWS API, external provider, deployment, commit, push, or live mutation was performed. Provider effects in the adversarial review used a local fake only.
- Passing tests, lint, evaluation, origin binding, and ledger reconciliation do not compensate for the two reproduced P1 mutation-boundary defects; this review remains FAIL.
