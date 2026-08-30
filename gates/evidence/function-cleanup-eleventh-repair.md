# Eleventh HEAVY review repair — exact Sheets binding

Date: 2026-08-28

Scope was limited to the two P1 findings in `function-cleanup-heavy-eleventh-review.md`. No
provider, credential, AWS resource, production state, graph, or canonical product contract
was inspected or changed.

## Repair

- Quoted owner JSON cell literals now remain raw decoded strings for the multiset comparison.
  The binding compares them to the provider-contract trimmed tool values, without applying
  NFKC/NFKD/case normalization to literal data. The separate denial-only negation comparison
  continues to normalize only its already-masked text.
- The exact selected target must have one approved introducer immediately before it. If another
  approved introducer is adjacent in that chain, the write is rejected before adapter or claim.
  This is structural and applies to English/Hebrew mixtures and longer chains, without treating
  unrelated earlier prose as an adjacent target introducer.

## Regressions

The real registry/local SQLite/counting `FakeSheetsPort` coverage proves zero incremental
adapter, claim, idempotency-row, and provider effects for owner `e\u0301` versus tool `é`,
the inverse, fullwidth versus ASCII, variation-selector and control differences, and all
repeated/mixed introducer chains. The raw exact decomposed literal succeeds after correction
of the same source event and its exact replay is idempotent. Existing tests retain one normal
English target, normal Hebrew target, suffix denial, and uppercase/lowercase/mixed spaced-tab
positives.

## Verification

All pytest runs used the offline workspace cache, a workspace-local basetemp, and no pytest
cache provider.

```text
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py \
  tests/unit/test_owner_sheets.py tests/unit/test_sheets.py tests/unit/test_vnext_principal.py \
  --basetemp .pytest-eleventh-repair -p no:cacheprovider
102 passed, 74 warnings in 3.38s

uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py \
  -k "literal_binding_preserves_raw_json_codepoints or binds_exactly_one_unquoted_target" \
  --basetemp .pytest-eleventh-repro -p no:cacheprovider
2 passed, 15 deselected, 2 warnings in 1.04s

uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py \
  tests/unit/test_owner_live_tools.py
All checks passed!

uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py \
  --select C901 --output-format concise
app/tools/registries/owner_tools.py:587:5: C901 `_website_kpis` is too complex (14 > 10)
```

The C901 output is the pre-existing `_website_kpis` inventory entry; this repair did not
modify it or add a C901 finding. `git diff --check` exited 0 with repository-wide line-ending
warnings only.

## Explicit non-claims

This local SQLite/fake-adapter evidence proves authorization ordering and exact local binding,
not live Google Sheets permissions or provider semantics, deployed behavior, credentials, or
production concurrency. The gates remain open pending a twelfth fresh HEAVY review.

## Parent follow-up hardening

Before trimming model cells, `_sheet_write_binding` now validates every flattened cell is a
string. Integers, nulls, nested lists, and objects return an ordinary failed `ToolResult`
through the existing pre-port/pre-claim path; they do not call `.strip()` or escape as an
exception. The focused real registry/counting-port test proves zero adapter, claim,
idempotency-row, and provider effects for each value.

The adjacent-introducer tail detector is now case-insensitive for English only in that
structural detector. Mixed-capitalization `AT at`, `Range at`, and three-token `AT Range at`
chains therefore deny, while spreadsheet ids, A1 ranges, and quoted cell values remain exact
and case-sensitive.

```text
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py \
  tests/unit/test_owner_sheets.py tests/unit/test_sheets.py tests/unit/test_vnext_principal.py \
  --basetemp .pytest-eleventh-followup -p no:cacheprovider
102 passed, 74 warnings in 3.40s

uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py \
  -k "binds_exactly_one_unquoted_target or prevalidates_policy" \
  --basetemp .pytest-eleventh-followup-repro -p no:cacheprovider
2 passed, 15 deselected, 2 warnings in 1.06s

uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py \
  tests/unit/test_owner_live_tools.py
All checks passed!
```

Strict C901 still reports only pre-existing `_website_kpis` (14 > 10), and `git diff --check`
exited 0 with only repository-wide line-ending warnings. A fresh twelfth HEAVY review remains
required.
