# Phase 1.5 function cleanup: thirteenth independent HEAVY review

Timestamp: `2026-08-28T09:49:56.4061872+03:00`

Mode: fresh adversarial review of the frozen dirty worktree; production code and tests were read-only

Decision: **FAIL**

## Scope and method

I loaded `AGENTS.md`, `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, and
`docs/DECISIONS.md` in that order, followed by the requested plan, gate files, twelfth-review
evidence, repair evidence, verification evidence, synthesis, current diff, task-specific code,
and tests. I did not inspect `.env`, secret values, `docs/archive`, AWS, or live providers.
Historical claims were treated as hypotheses and rerun locally.

## Findings

### P0

None found.

### P1

1. **Colon, exclamation, and ASCII-hyphen separated target-introducer chains still reach
   Sheets claims and provider mutation.**

   `_SHEETS_TARGET_INTRO_TAIL_RE` at
   `app/tools/registries/owner_tools.py:908-910` permits only characters outside
   `[\w!:-]` between the earlier introducer and the selected introducer. As a result,
   `at: range`, `at! range`, and `at- range` are not recognized as repeated introducer
   chains by the check at `app/tools/registries/owner_tools.py:940`, even though the later
   `range` binds the selected target. The same result reproduced for mixed EN/HE chains.

   Direct binder results for those three cases were `True, True, True`. A real
   `FakeSheetsPort` effects probe returned `ok=True` for all three, constructed the port,
   created three `owner_sheets_write` idempotency rows, and recorded three update
   operations. The effect path is port construction at line 746, claim at line 756, and
   capability/provider execution at line 761. This is the same authority-boundary class
   as the twelfth review's comma/parenthesis finding, but under punctuation excluded by
   the repaired tail pattern.

2. **Secondary whole-column, whole-row, and absolute A1 references are invisible to the
   remaining-target scan and let the model select one target from several owner-stated
   targets.**

   `_A1_CELL_OR_RANGE_RE` and `_BANG_QUALIFIED_A1_RE` at
   `app/tools/registries/owner_tools.py:911-916` recognize only relative bounded cell
   endpoints such as `B2` or `B2:C3`. They do not recognize valid A1 forms such as `B:B`,
   `Other!2:2`, `'Other Tab'!B:B`, `$B$2`, or `Other!$B$2`. After the selected target is
   blanked at lines 942-948, the final rejection at lines 949-953 therefore accepts these
   secondary targets.

   The direct binder accepted every form above. A real `FakeSheetsPort` effects probe for
   `B:B`, `Other!2:2`, `'Other Tab'!B:B`, and `$B$2` returned `ok=True` four times and
   added four port constructions, four claims/idempotency rows, and four provider
   mutations of the model-selected `Sheet One!A1`. No secondary actual A1 target may reach
   any of those effects.

### P2

None additional. The two P1 findings already fail the release gate.

### P3

1. `app/graph/owner_agent.py:104` correctly advertises bounded `sheets_update` and
   `sheets_append`, and the registry exposes them at
   `app/tools/registries/owner_tools.py:1427` and `:1441`; however, the same system prompt
   says "You have read tools only" at `app/graph/owner_agent.py:148`. The more specific
   tool wording can still guide the model, so I did not promote this wording contradiction
   to a release-blocking defect, but the prompt should describe the bounded Sheets-write
   exception accurately.

## Adversarial probe results

- Repeated-introducer separator sweep: space, comma, period, semicolon, question mark,
  slash, backslash, parentheses, brackets, braces, straight/curly quotes, tab, newline,
  CRLF, controls, ZWJ/ZWNJ, LRM/RLM, bidi embedding controls, Unicode marks, emoji, maqaf,
  and em dash denied. Colon, exclamation, ASCII hyphen, and underscore were accepted in
  both English and mixed Hebrew/English chains. Colon/exclamation/hyphen reached the fake
  provider as described in P1-1.
- Secondary targets: uppercase/lowercase/mixed bounded cells and ranges, bare and
  bang-qualified targets, spaced and quoted tab names, before/after placement, prefix and
  suffix punctuation, and row/column endpoint bounds were denied. Unicode lookalikes were
  correctly inert. Whole-column, whole-row, and absolute A1 forms reached mutation as
  described in P1-2.
- Valid exact binding: `at`, `AT`/`RANGE`, `RaNgE`, and Hebrew introducers bound; exact
  lowercase/mixed-case spaced tab names bound without normalizing the raw selected target.
  Existing validator-legal provider tests passed.
- Raw value binding: exact precomposed and escaped literals passed; decomposed/precomposed,
  fullwidth/ASCII, variation-selector, and format-control substitutions failed closed.
  Exact duplicate multisets passed; missing/extra duplicates and invalid JSON failed.
  Existing 324-test coverage also passed non-string and trim-empty denial plus preservation
  of internal spaces.
- Exhaustive Unicode negation: all runtime Unicode `M*` and `Cf` codepoints inserted into
  standalone Hebrew negation produced zero authorization failures. A quoted negation value
  remained inert and valid.
- Every non-P1 denial inspected or exercised occurred before port construction, claim,
  idempotency persistence, and provider effect. Both P1 classes violated all four ordering
  requirements.
- Telegram voice focused coverage passed numeric auth-before-media, non-audio MIME,
  empty/oversize bytes, alternate-port validation, pre-media dedupe, one visible text reply,
  and no OwnerGraph call on failure. No TTS path was found.
- Gmail callback and console deferral/recovery tests passed in both orders twice in one
  Python process. Finalization, due-reminder, hot-handoff, kill-switch, recipient retry,
  returning-session, and migration-focused tests passed.

## Exact commands and results

### New direct Sheets probes

```powershell
uv --offline --cache-dir .uv-cache run python gates\evidence\_thirteenth_probe.py
```

Temporary reviewer probe result: chain acceptances were `bang, colon, hyphen, underscore`
in both English and mixed EN/HE; secondary acceptances were `B:B`, `Other!B:B`,
`'Other Tab'!B:B`, `2:2`, `Other!2:2`, `$B$2`, and `Other!$B$2`; all 2,671 runtime
`M*`/`Cf` negation insertions denied; raw-codepoint and multiset controls behaved as
described above. The temporary probe file was removed after evidence capture.

```powershell
uv --offline --cache-dir .uv-cache run python -c 'import os,runpy; os.environ["MIA_DATABASE_URL"]="sqlite://"; ns=runpy.run_path("tests/unit/test_owner_live_tools.py"); s=ns["_session"](); c=ns["_ctx"](s); from app.db.models import IdempotencyRow; from app.integrations.sheets import FakeSheetsPort; from app.tools.registries.owner_tools import execute_tool; c.sheets=FakeSheetsPort(); c.settings=c.settings.model_copy(update={"sheets_allowed_spreadsheet_ids":"sheet-main"}); q=chr(34); sq=chr(39); a={"spreadsheet_id":"sheet-main","range":"Sheet One!A1","values":[["x"]]}; cases=[f"Update Google Sheets sheet-main at: range Sheet One!A1 with {q}x{q}",f"Update Google Sheets sheet-main at! range Sheet One!A1 with {q}x{q}",f"Update Google Sheets sheet-main at- range Sheet One!A1 with {q}x{q}",f"Update Google Sheets sheet-main at Sheet One!A1 plus B:B with {q}x{q}",f"Update Google Sheets sheet-main at Sheet One!A1 plus Other!2:2 with {q}x{q}",f"Update Google Sheets sheet-main at Sheet One!A1 plus {sq}Other Tab{sq}!B:B with {q}x{q}",f"Update Google Sheets sheet-main at Sheet One!A1 plus $B$2 with {q}x{q}"]; out=[]; [(setattr(c,"source_ref",f"telegram:thirteenth-{i}"),setattr(c,"owner_text",t),out.append(execute_tool("sheets_update",a,c))) for i,t in enumerate(cases)]; print([(r.ok,r.error) for r in out]); print(c.sheets.owner_operations); print(s.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count()); s.close()'
```

Result: seven `(True, '')` tool results, seven fake-provider updates, and seven
`owner_sheets_write` idempotency rows; exit 0.

### Focused recovery and ordering

```powershell
uv --offline --cache-dir .uv-cache run python -c 'import pytest; a=["tests/unit/test_telegram_owner_outbound.py::test_gmail_callback_recovers_deferred_and_failed_send_once","tests/unit/test_owner_gmail_console.py::test_approved_gmail_send_deferrals_remain_retryable"]; b=list(reversed(a)); x=pytest.main([*a,"--basetemp=.pytest-heavy-thirteenth-gmail-a","-p","no:cacheprovider","-q"]); y=pytest.main([*b,"--basetemp=.pytest-heavy-thirteenth-gmail-b","-p","no:cacheprovider","-q"]); print({"callback_then_console":int(x),"console_then_callback":int(y)}); raise SystemExit(int(x or y))'
```

Result: `2 passed`, then `2 passed` in reverse order in the same process; both exit 0.

```powershell
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_telegram.py tests/unit/test_transcribe.py tests/unit/test_vnext_finalization.py::test_legacy_completed_conversation_claim_is_retained_after_recipient_upgrade tests/unit/test_vnext_finalization.py::test_finalization_recipient_ledger_retries_only_known_rejection tests/unit/test_vnext_finalization.py::test_empty_returning_session_cannot_borrow_an_old_sessions_visitor_message tests/unit/test_due_scan_worker.py::test_legacy_same_day_due_claim_does_not_resend_but_old_day_does tests/unit/test_due_scan_worker.py::test_due_scan_kill_switch_skips_owner_reminder tests/unit/test_hot_handoff.py::test_hot_handoff_kill_switch_mutates_nothing_in_the_real_store tests/unit/test_hot_handoff.py::test_hot_handoff_releases_only_after_confirmed_full_rejection tests/unit/test_hot_handoff.py::test_hot_handoff_retains_claim_after_ambiguous_transport_error tests/unit/test_migrate.py --basetemp .pytest-heavy-thirteenth-focused -p no:cacheprovider -q
```

Result: **57 passed**, exit 0.

### Required regression gates

```powershell
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_vnext_finalization.py tests/unit/test_website_handoff_owner_notify.py tests/unit/test_hot_handoff.py tests/unit/test_due_scan_worker.py tests/unit/test_comm_operating_model.py tests/unit/test_owner_notify.py tests/unit/test_website_client_graph.py tests/unit/test_vnext_graph_functions.py tests/unit/test_migrate.py tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py tests/unit/test_sheets.py tests/unit/test_vnext_principal.py tests/unit/test_vnext_owner_voice.py tests/unit/test_telegram.py tests/unit/test_transcribe.py tests/unit/test_telegram_owner_outbound.py tests/unit/test_telegram_owner_graph.py tests/unit/test_telegram_format.py --basetemp .pytest-heavy-thirteenth-combined -p no:cacheprovider -q
```

Result: **324 passed** (confirmed separately by collect-only), exit 0.

```powershell
uv --offline --cache-dir .uv-cache run pytest --basetemp .pytest-heavy-thirteenth-full -p no:cacheprovider -q
uv --offline --cache-dir .uv-cache run pytest --collect-only -q -o addopts=
```

Result: full progress reached 100%, exit 0; collect-only measured **2,466 tests**.

```powershell
uv --offline --cache-dir .uv-cache run ruff check app tests scripts
uv --offline --cache-dir .uv-cache run python scripts\assert_origin_bind.py
uv --offline --cache-dir .uv-cache run python scripts\eval_diff.py
```

Results: `All checks passed!`; `origin-bind: ok`; **273/273** across sales 51, buyer
43, calendar 20, website_handoff 15, safety 20, objection 20, routing 20, extract 30,
writing 33, and gold 21.

```powershell
$out = uv --offline --cache-dir .uv-cache run ruff check --select C901 app scripts --output-format concise --exit-zero; $count = @($out | Where-Object { $_ -match ' C901 ' }).Count; $out; "C901_COUNT=$count"
```

Result: **36 C901 findings**, matching current gate evidence.

```powershell
$current = @(rg -l '^[[:space:]]*(async[[:space:]]+)?def[[:space:]]' app scripts -g '*.py' | ForEach-Object { $_ -replace '\\','/' } | Sort-Object -Unique); $rows = @(); foreach ($matrix in @('gates/evidence/function-audit-api.md','gates/evidence/function-audit-domain.md','gates/evidence/function-audit-infra.md')) { Get-Content $matrix | ForEach-Object { if ($_ -match '^\|\s*\d+\s*\|\s*`((?:app|scripts)/[^`]+\.py)`') { $rows += $Matches[1] } } }; $uniqueRows = @($rows | Sort-Object -Unique); $missing = @($current | Where-Object { $_ -notin $uniqueRows }); $extra = @($uniqueRows | Where-Object { $_ -notin $current }); $definitions = (rg -n '^[[:space:]]*(async[[:space:]]+)?def[[:space:]]' app scripts -g '*.py' | Measure-Object).Count; $physical = 0; $nonblank = 0; foreach ($file in $current) { $content = @(Get-Content -LiteralPath $file); $physical += $content.Count; $nonblank += @($content | Where-Object { $_.Trim().Length -gt 0 }).Count }; [pscustomobject]@{ current_files=$current.Count; audit_rows=$rows.Count; unique_audit_rows=$uniqueRows.Count; duplicate_rows=($rows.Count-$uniqueRows.Count); missing=$missing; extra=$extra; definition_lines=$definitions; physical_lines=$physical; nonblank_lines=$nonblank } | ConvertTo-Json -Compress
```

Result: **164** current files, **164** audit rows, **164** unique rows, zero duplicates,
missing, or extra paths; **1,635** definition lines, **42,283** physical lines, and
**37,580** nonblank lines.

```powershell
git diff --check
```

Result before final evidence write: exit 0; line-ending warnings only.

## Architecture verdict

The deliberate runtime architecture remains intact: one production OwnerGraph owner agent,
one separate ClientGraph, shared core, thin Telegram/website channels, request-derived
`Principal`, named capability to policy to typed-adapter flow, and Postgres as system of
record. GA4 and GSC are API-backed typed reads, LinkedIn is profile-only, and bounded Sheets
operations are owner-only. Telegram voice is input-only and returns one text reply; no TTS,
runtime swarm, paid-Meta execution, campaign analysis/pacing/prelaunch, or LinkedIn member
analytics path was found. Historical paid capability identifiers remain `SPECIFIED` with
empty ports, and campaign requests are refusal-only.

That preservation is insufficient for release because the Sheets binding boundary still lets
the model choose one target from malformed or multiple owner-stated A1 targets.

## Final verdict

**FAIL — two unresolved P1 findings. Do not approve Phase 1.5.4, commit, or deploy this
tree.**

No completion item was checked in `gates/leaf-1.5.4f-final-review.md`,
`gates/leaf-1.5.4-function-cleanup.md`, `gates/node-1.5.md`, or `gates/root.md`.
