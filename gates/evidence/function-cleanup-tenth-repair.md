# Tenth HEAVY review repair — Unicode-mark Sheets negation

Date: 2026-08-28

Scope was limited to the single P1 in `function-cleanup-heavy-tenth-review.md`. No provider,
AWS resource, credential, production state, or canonical product/architecture contract was
inspected or changed.

## Repair

After JSON-quoted literals are masked, the Sheets prohibition check now performs NFKD and
removes only Unicode marks (`Mn`, `Mc`, or `Me`) plus format controls (`Cf`) for this
denial-only comparison. This closes class-zero marks such as U+034F COMBINING GRAPHEME JOINER
and U+FE0F VARIATION SELECTOR-16 plus visually inert separators such as U+200D ZWJ, without
changing stored text or provider cell values and without stripping letters, punctuation,
whitespace, or other categories. The resulting text still uses the existing standalone
Hebrew-word boundaries for `לא` and `אל`.

## Regressions

The real registry/local SQLite/counting `FakeSheetsPort` test now proves each rejected turn
has zero incremental adapter constructions, claims, idempotency rows, and provider operations
for both `ל\u034fא` and `א\u034fל`, a class-zero variation selector, representative `Mc`
(U+0903) and `Me` (U+20DD) insertion, mixed marks, niqqud, cantillation, ZWJ, LRM, WORD
JOINER, and zero-width space. It asserts U+034F is `Mn` with combining class zero and ZWJ/LRM
are `Cf`. A JSON-quoted control-containing negator remains a literal, an embedded pointed
Hebrew word remains non-negating, and a corrected request for the same source event writes
once with its exact replay idempotent.

## Verification

All pytest runs used the offline workspace cache, a workspace-local basetemp, and no pytest
cache provider.

```text
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py \
  tests/unit/test_owner_sheets.py tests/unit/test_sheets.py tests/unit/test_vnext_principal.py \
  --basetemp .pytest-tenth-cf -p no:cacheprovider
101 passed, 74 warnings in 3.37s

uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py \
  -k "bounded_negation_modifiers" --basetemp .pytest-tenth-cf-repro -p no:cacheprovider
1 passed, 15 deselected, 2 warnings in 1.12s
```

```text
uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py \
  tests/unit/test_owner_live_tools.py
All checks passed!

uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py \
  --select C901 --output-format concise
app/tools/registries/owner_tools.py:587:5: C901 `_website_kpis` is too complex (14 > 10)
```

The C901 output is the pre-existing `_website_kpis` inventory entry; this narrow repair did
not modify it or introduce a new C901 finding. `git diff --check` exited 0 with only
repository-wide line-ending warnings.

## Explicit non-claims

This is local SQLite/fake-adapter proof of binding order only. It does not prove live Google
Sheets permissions or semantics, deployment, production concurrency, or credentials. The
gate remains open pending a fresh eleventh HEAVY review.
