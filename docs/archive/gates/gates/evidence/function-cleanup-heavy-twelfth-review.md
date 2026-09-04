# Phase 1.5 function cleanup: twelfth independent HEAVY review

Timestamp: `2026-08-28T09:25:50.2931881+03:00`

Mode: fresh adversarial review of the current dirty worktree; no production code or tests changed

Decision: **FAIL**

## Scope and method

I loaded `AGENTS.md`, `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, and
`docs/DECISIONS.md` in that order, then the plan, four requested gate files (using the
corrected `gates/root.md` path), repair evidence, synthesis, current diff, task-specific
code, and tests. I did not inspect `.env`, secret values, `docs/archive`, live providers,
or AWS. Historical author/reviewer claims were treated as hypotheses and rerun locally.

## Findings

### P0

None found.

### P1

1. **Punctuation or parentheses between target introducers bypass the one-introducer
   Sheets authority boundary.**

   Evidence: `app/tools/registries/owner_tools.py:905-908` defines a tail detector that
   recognizes only an introducer followed by whitespace at the end of the prefix, and
   `app/tools/registries/owner_tools.py:931-939` applies that detector only immediately
   before the selected introducer. Consequently `at, range Sheet One!A1`,
   `at (range Sheet One!A1)`, and the corresponding mixed Hebrew/English punctuation
   chain are accepted. The current production write path then proceeds from the binder
   through port construction, idempotency claim, and capability execution at
   `app/tools/registries/owner_tools.py:713-785`.

   Direct result on the current tree:

   ```text
   at, range ...        -> True
   at (range ...)       -> True
   Hebrew-intro, range  -> True
   ```

   A second direct probe used a real `FakeSheetsPort`; the result was `ok=True` and the
   provider operation list contained
   `('update', 'sheet-main', 'Sheet One!A1', [['x']])`. This violates the explicit
   contract that exactly one authorized target introducer binds before any effect.

2. **Lowercase secondary A1 targets are invisible to the remaining-target rejection and
   let the model select one of multiple owner-stated targets.**

   Evidence: `app/tools/registries/owner_tools.py:909-914` matches only uppercase A1
   endpoints in both bare and bang-qualified secondary-target scans. After the selected
   target is blanked, `app/tools/registries/owner_tools.py:940-951` therefore does not see
   `b2` or `Other!b2`. Both messages below returned `True` from the current binder and
   both completed real `FakeSheetsPort` mutations of `Sheet One!A1`:

   ```text
   Update Google Sheets sheet-main at Sheet One!A1 plus b2 with "x"
   Update Google Sheets sheet-main at Sheet One!A1 plus Other!b2 with "x"
   ```

   The uppercase control `B2` was correctly rejected. A target-binding guard cannot
   allow target selection merely because the second A1 token used lowercase letters.

### P2

1. **English target introducers are case-sensitive, rejecting ordinary owner casing.**

   Evidence: `_SHEETS_TARGET_INTRO_RE` at
   `app/tools/registries/owner_tools.py:905` is compiled into `selected_target` at
   `app/tools/registries/owner_tools.py:931-934` without `re.IGNORECASE`. Clean authorized
   requests using `RANGE Sheet One!A1` or `RaNgE Sheet One!A1` returned `False`, although
   the operation and Sheet reference are normalized to lowercase earlier. This makes the
   owner-facing natural-language write contract depend on magic casing.

### P3

None found.

## Adversarial probe results

- Raw JSON codepoint binding: exact precomposed text passed; precomposed/decomposed,
  fullwidth/ASCII, variation-selector removal, and `Cf` control removal mismatches all
  failed closed. Whitespace-bearing quoted cells failed rather than being provider-trimmed
  into a different write. Exact duplicate multisets passed; duplicate-count mismatch,
  invalid JSON, and non-string model cells failed closed.
- Unicode negation: all 2,671 runtime Unicode `M*`/`Cf` codepoints inserted inside Hebrew
  `לא` were recognized (zero failures). U+034F outside quoted values denied the write;
  an exact quoted `"לא"` value remained inert and authorized when the surrounding request
  was otherwise valid.
- Target binding: uppercase secondary targets were rejected, but the P1 lowercase bare
  and bang-qualified variants and punctuation/parenthesis introducer chains mutated the
  fake provider. Adjacent/newline chains covered by the existing detector failed closed.
- Telegram/Gmail/notifications: non-audio MIME, empty/oversize/malformed alternate media,
  canonical voice preclaim/dedupe, same-process Gmail recovery, per-recipient notification
  retry, finalization, hot handoff, and kill-switch regression tests passed. The three
  Telegram duplicate/Gmail recovery tests passed twice via two `pytest.main` calls in one
  Python process.

## Exact commands and results

### Direct Sheets binder and provider-effect probes

```powershell
uv --offline --cache-dir .uv-cache run python -c 'from app.tools.registries.owner_tools import _has_bound_sheets_write_request as f; a={"spreadsheet_id":"sheet-main","range":"Sheet One!A1","values":[["x"]]}; cases=["Update Google Sheets sheet-main at, range Sheet One!A1 with \"x\"","Update Google Sheets sheet-main at (range Sheet One!A1) with \"x\"","Update Google Sheets sheet-main AT range Sheet One!A1 with \"x\"","Update Google Sheets sheet-main at\nrange Sheet One!A1 with \"x\"","עדכן שיטס sheet-main בטווח, range Sheet One!A1 with \"x\""]; print([(c, f(c,a,append=False,allowed_spreadsheet_ids=frozenset({"sheet-main"}))) for c in cases])'
```

Result: `True, True, False, False, True` in case order.

```powershell
uv --offline --cache-dir .uv-cache run python -c 'from app.tools.registries.owner_tools import _has_bound_sheets_write_request as f; a={"spreadsheet_id":"sheet-main","range":"Sheet One!A1","values":[["x"]]}; cases=["Update Google Sheets sheet-main at Sheet One!A1 plus b2 with \"x\"","Update Google Sheets sheet-main at Sheet One!A1 plus Other!b2 with \"x\"","Update Google Sheets sheet-main at Sheet One!A1 plus B2 with \"x\"","Update Google Sheets sheet-main RANGE Sheet One!A1 with \"x\"","Update Google Sheets sheet-main RaNgE Sheet One!A1 with \"x\""]; print([(c, f(c,a,append=False,allowed_spreadsheet_ids=frozenset({"sheet-main"}))) for c in cases])'
```

Result: `True, True, False, False, False` in case order.

```powershell
uv --offline --cache-dir .uv-cache run python -c 'import os,runpy; os.environ["MIA_DATABASE_URL"]="sqlite://"; ns=runpy.run_path("tests/unit/test_owner_live_tools.py"); s=ns["_session"](); c=ns["_ctx"](s); from app.integrations.sheets import FakeSheetsPort; from app.tools.registries.owner_tools import execute_tool; c.sheets=FakeSheetsPort(); c.settings=c.settings.model_copy(update={"sheets_allowed_spreadsheet_ids":"sheet-main"}); a={"spreadsheet_id":"sheet-main","range":"Sheet One!A1","values":[["x"]]}; cases=["Update Google Sheets sheet-main at, range Sheet One!A1 with \"x\"","Update Google Sheets sheet-main at Sheet One!A1 plus Other!b2 with \"x\""]; out=[]; [(setattr(c,"source_ref",f"telegram:probe-{i}"), setattr(c,"owner_text",t), out.append(execute_tool("sheets_update",a,c))) for i,t in enumerate(cases)]; print([(r.ok,r.error) for r in out]); print(c.sheets.owner_operations); s.close()'
```

Result: both tool results were `(True, '')`; two update operations reached the fake
provider.

The raw-literal/multiset and exhaustive `M*`/`Cf` probes were also run with
`uv --offline --cache-dir .uv-cache run python -c ...`; results are recorded in the
adversarial section above. No probe called a live provider.

### Focused and full regression

```powershell
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_vnext_finalization.py tests/unit/test_website_handoff_owner_notify.py tests/unit/test_hot_handoff.py tests/unit/test_due_scan_worker.py tests/unit/test_comm_operating_model.py tests/unit/test_owner_notify.py tests/unit/test_website_client_graph.py tests/unit/test_vnext_graph_functions.py tests/unit/test_migrate.py tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py tests/unit/test_sheets.py tests/unit/test_vnext_principal.py tests/unit/test_vnext_owner_voice.py tests/unit/test_telegram.py tests/unit/test_transcribe.py tests/unit/test_telegram_owner_outbound.py tests/unit/test_telegram_owner_graph.py tests/unit/test_telegram_format.py --basetemp .pytest-heavy-twelfth-review-combined -p no:cacheprovider -q
```

Result: **323 passed**, exit 0.

```powershell
uv --offline --cache-dir .uv-cache run python -c 'import pytest; nodes=["tests/unit/test_telegram.py::test_retried_voice_transcription_failure_sends_one_visible_reply","tests/unit/test_telegram.py::test_retried_voice_success_claims_before_download_stt_graph_and_reply","tests/unit/test_telegram_owner_outbound.py::test_gmail_callback_recovers_deferred_and_failed_send_once"]; a=pytest.main([*nodes,"--basetemp=.pytest-heavy-twelfth-review-sameproc-a","-p","no:cacheprovider","-q"]); b=pytest.main([*nodes,"--basetemp=.pytest-heavy-twelfth-review-sameproc-b","-p","no:cacheprovider","-q"]); print({"first":a,"second":b}); raise SystemExit(int(a or b))'
```

Result: **3 passed, then 3 passed in the same process**, both exit codes 0.

```powershell
uv --offline --cache-dir .uv-cache run pytest --basetemp .pytest-heavy-twelfth-review-full -p no:cacheprovider -q
```

Result: **2,465 passed**, exit 0 (the repository `addopts=-q` plus command `-q`
suppressed the final one-line count; progress reached 100% with 2,465 collected items).

### Lint, evals, origin, inventory, and diff

```powershell
uv --offline --cache-dir .uv-cache run ruff check app tests scripts
```

Result: `All checks passed!`

```powershell
uv --offline --cache-dir .uv-cache run python scripts/assert_origin_bind.py
```

Result: `origin-bind: ok`.

```powershell
uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py
```

Result: **273/273**: sales 51, buyer 43, calendar 20, website_handoff 15, safety 20,
objection 20, routing 20, extract 30, writing 33, gold 21.

```powershell
$current = @(rg -l '^[[:space:]]*(async[[:space:]]+)?def[[:space:]]' app scripts -g '*.py' | ForEach-Object { $_ -replace '\\','/' } | Sort-Object -Unique); $rows = @(); foreach ($matrix in @('gates/evidence/function-audit-api.md','gates/evidence/function-audit-domain.md','gates/evidence/function-audit-infra.md')) { Get-Content $matrix | ForEach-Object { if ($_ -match '^\|\s*\d+\s*\|\s*`((?:app|scripts)/[^`]+\.py)`') { $rows += $Matches[1] } } }; $uniqueRows = @($rows | Sort-Object -Unique); $missing = @($current | Where-Object { $_ -notin $uniqueRows }); $extra = @($uniqueRows | Where-Object { $_ -notin $current }); $definitions = (rg -n '^[[:space:]]*(async[[:space:]]+)?def[[:space:]]' app scripts -g '*.py' | Measure-Object).Count; $physical = 0; $nonblank = 0; foreach ($file in $current) { $content = @(Get-Content -LiteralPath $file); $physical += $content.Count; $nonblank += @($content | Where-Object { $_.Trim().Length -gt 0 }).Count }; [pscustomobject]@{ current_files=$current.Count; audit_rows=$rows.Count; unique_audit_rows=$uniqueRows.Count; duplicate_rows=($rows.Count-$uniqueRows.Count); missing=$missing; extra=$extra; definition_lines=$definitions; physical_lines=$physical; nonblank_lines=$nonblank } | ConvertTo-Json -Compress
```

Result: **164 current files, 164 rows, 164 unique rows, zero duplicates/missing/extra,
1,635 definition lines, 42,281 physical lines, 37,578 nonblank lines**.

```powershell
$out = uv --offline --cache-dir .uv-cache run ruff check --select C901 app scripts --output-format concise --exit-zero; $count = @($out | Where-Object { $_ -match ' C901 ' }).Count; $out; "C901_COUNT=$count"
```

Result: **36 C901 findings**, matching the current matrix/gate evidence.

```powershell
git diff --check
```

Result before this evidence file: exit 0; only LF-to-CRLF working-copy warnings.

## Architecture verdict

The broad architecture remains intact: one production OwnerGraph owner agent under
ADR-031/032, one ClientGraph, shared brain/core, thin Telegram/website boundaries,
request-derived principals, named capability to policy to typed-adapter flow, Postgres as
system of record, text-only output with shared STT input, API-backed GA4/GSC, profile-only
LinkedIn, and allowlisted bounded Sheets operations. Repository searches and runtime wiring
inspection found no resurrected swarm, TTS, paid-Meta, campaign-analysis, pacing, prelaunch,
or LinkedIn-member-analytics execution path; historical capability identifiers remain
`SPECIFIED` with empty ports and campaign wording is refusal-only.

That architectural preservation is not sufficient for release because the current Sheets
write authority boundary still lets the model choose one target from malformed/multiple
owner text. With two P1 and one P2 findings, the clean-room acceptance condition is unmet.

## Final verdict

**FAIL — do not approve Phase 1.5.4, commit, or deploy this tree.**

No completion item was checked in `gates/leaf-1.5.4f-final-review.md`,
`gates/leaf-1.5.4-function-cleanup.md`, `gates/node-1.5.md`, or `gates/root.md`.
