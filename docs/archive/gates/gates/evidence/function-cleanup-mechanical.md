# Phase 1.5.4c mechanical cleanup evidence

Date: 2026-08-28
Scope: only the Phase 1.5.4c owned production files and focused tests.

## Measured cleanup

The worktree was already dirty before this leaf. The metrics below count the named
constructs rather than using whole-file Git deltas.

| Item | Before | After | Verification |
| --- | ---: | ---: | --- |
| Proven-unreferenced brain/domain symbols | 4 | 0 | `cosine_similarity`, `short_lead_id`, `ProposedInstruction`, and `human_turn_count` have no non-evidence references. |
| Duplicate unused `LeadStore` range counter | 1 | 0 | `count_canonical_events_in_range` has no non-evidence references. |
| Sheets name-discovery helpers | 2 | 0 | `pick_spreadsheet_id` and `_map_spreadsheet_files` have no non-evidence references; its sole obsolete unit test/import was removed. |
| Owner-task identical phrase helpers | 8 | 1 | `_phrase_in_text` retains the exact ASCII case-fold / non-ASCII substring semantics; all former callers use it. |
| Search Console identical `x or x` fallbacks | 4 | 0 | Site entry, site URL, inspection result, and index-status fallbacks now have one equivalent lookup. |
| `eval_diff` registered eval families | 8 | 10 | Added `calendar` and `routing`; test asserts both keys. |
| Plaintext ECS revision `--env` injection | 1 option + 1 helper | 0 | Parser rejects `--env`; no container environment mutator remains. |
| ECS migration command override | 1 `--command` option | 0 | Exact serialized override is `["mia-migrate"]`; parser rejects `--command`. |

The Sheets module claim now accurately distinguishes authorized bounded reads/values
operations from prohibited use as Mia's truth, decision input, or recovery source.
No explicit spreadsheet ID, allowlist, idempotency, or mirror behavior was changed.

## Fresh reference searches

Pass 1 ran repository-wide `rg -n` for all six removed definitions and the eight
former phrase-helper names, excluding this evidence and the leaf gate. Result: zero
matches. Pass 2 searched the Search Console module for each duplicated same-key
fallback pattern. Result: zero matches.

## Verification passes

1. Focused tests:
   `uv run --offline --cache-dir .uv-cache pytest -p no:cacheprovider tests/unit/test_brain_memory.py tests/unit/test_lead_label.py tests/unit/test_learning.py tests/unit/test_owner_tasks.py tests/unit/test_sheets.py tests/unit/test_search_console.py tests/unit/test_evals.py tests/unit/test_scripts.py tests/unit/test_kpis.py`
   Result: **286 passed** (warnings only).
2. Focused Ruff:
   `uv run --offline --cache-dir .uv-cache ruff check` over every owned production
   file and focused test file. Result: **All checks passed**.
3. Focused operator-script regression rerun:
   `uv run --offline --cache-dir .uv-cache pytest -p no:cacheprovider tests/unit/test_scripts.py`.
   Result: **6 passed**.
4. Diff/reference integrity:
   `git diff --check` completed with no diff errors (Git emitted pre-existing CRLF
   warnings); both fresh reference searches above returned zero matches.

## Scope boundary

No production architecture, provider selection, policy, explicit-ID/allowlist, or
idempotency contract changed. Existing unrelated dirty changes were preserved.

## Windows deployment-test addendum

The full suite exposed three failures in `test_deploy_secret_box.py` before the
PowerShell scripts started: child `powershell -NoProfile -File` processes inherited this
host's Restricted execution policy. This was test-process tooling, not a production script
or assertion defect. The shared `_run_ps1` helper and the one direct `subprocess.run` call
now add process-scoped `-ExecutionPolicy Bypass` before `-File`; production `.ps1` files and
all existing assertions are unchanged.

Verification after the change:

1. `uv run --offline --cache-dir .uv-cache pytest -p no:cacheprovider tests/unit/test_deploy_secret_box.py tests/unit/test_scripts.py` — **19 passed**.
2. `uv run --offline --cache-dir .uv-cache ruff check tests/unit/test_deploy_secret_box.py tests/unit/test_scripts.py scripts/eval_diff.py scripts/deploy_ecs_revision.py scripts/run_ecs_migration.py` — **All checks passed**.
3. `git diff --check` — no diff errors (pre-existing CRLF warnings only).
