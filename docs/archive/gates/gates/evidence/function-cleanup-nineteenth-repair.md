# Phase 1.5 function cleanup: nineteenth-review structural repair

Date: 2026-08-30

## Design

The finite residual JSON/pseudo-token blacklist was removed. The binder now builds a
security view where valid JSON string literals, the exact selected spreadsheet ID, and
the exact A1 target become distinct private-use internal sentinels. It accepts only a
small positive set of complete value clauses:

- English values-first: quoted `CELL` list, selected `ID`, `at`/`range`, selected
  `TARGET`, optional Sheet wording.
- English target-first: selected `ID`, `at`/`range`, selected `TARGET`, `with`, quoted
  `CELL` list, optional Sheet wording.
- Hebrew values-first and target-first equivalents, including the existing object marker
  `את`, Google Sheet wording, and `ב-` value introduction.

The operation clause must fully match one of these shapes. Unquoted text in a value slot,
between quoted cells, or after/before the quoted list cannot be interpreted as a cell and
therefore fails before port construction, claim creation, idempotency-row creation, or
provider mutation. Raw target, ID, and quoted literal codepoints remain unchanged in the
underlying binding checks. Exact order and rectangular grid shape remain mandatory.

Follow-up: the values-first English shape permits no wildcard words between `ID` and
`at`/`range`. The old `at_foo range` test is now a fail-closed denial. Counted seam
regressions deny arbitrary English/Unicode words and punctuation between `ID` and the
target introducer, after `TARGET`, and before target-first `with`; ordinary accepted
values-first/target-first English and Hebrew shapes remain positive controls.

Second follow-up: readable `CELL`/`ID`/`TARGET` placeholders were replaced with
non-user private-use sentinels. The scanner rejects either sentinel boundary when it
appears outside a JSON-quoted literal, so raw placeholder-looking words cannot collide
with injected grammar tokens and raw sentinel text fails closed. Counted zero-effect
denials cover raw uppercase/lowercase/mixed-case placeholder words plus sentinel text
before, after, between quoted cells, and in ID/target scaffolding. Quoted `"CELL"`,
`"ID"`, `"TARGET"`, and a private-use-looking literal remain exact authorized data.

## Regression coverage

The counted real-boundary test exercises all English append/update verbs over six list
connectors and all Hebrew verbs over five connectors, before and after each review token
class plus an unknown Unicode-letter word. It covers arbitrary bare words, hyphenated
words, backticks, punctuation-only values, prior JSON/pseudo/malformed forms, closers,
and extras between two quoted cells. Every denial asserts zero port/claim/idempotency-row/
fake-provider delta.

Existing and extended positives retain harmless pre-verb prose; values-first and
target-first English/Hebrew scaffolding; exact single-cell, row, column, 2x2, and duplicate
layouts; numeric-looking ID/target text; quoted lookalikes; escaped strings; and idempotent
replay. The deliberately strict grammar rejects unrecognized sentence styles within the
mutation clause (for example alternative prepositions or explanatory prose mixed among
cells); owners must restate the bounded write using the accepted explicit shapes.

## Local evidence

```text
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py -q --basetemp .pytest-nineteenth-repair-owner-live -p no:cacheprovider
24 passed

uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py -q --basetemp .pytest-nineteenth-followup-owner-live -p no:cacheprovider
24 passed

uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py -q --basetemp .pytest-nineteenth-sentinel-owner-live -p no:cacheprovider
24 passed

uv --offline --cache-dir .uv-cache run pytest [the exact 19 files from function-cleanup-repair-verification.md] --basetemp .pytest-nineteenth-followup-19files -p no:cacheprovider --disable-warnings
330 passed, 326 warnings in 10.53s

uv --offline --cache-dir .uv-cache run pytest [the exact 19 files from function-cleanup-repair-verification.md] --basetemp .pytest-nineteenth-repair-19files -p no:cacheprovider --disable-warnings
330 passed, 326 warnings in 10.54s

uv --offline --cache-dir .uv-cache run pytest [the exact 19 files from function-cleanup-repair-verification.md] --basetemp .pytest-nineteenth-sentinel-19files -p no:cacheprovider --disable-warnings
330 passed, 326 warnings in 10.52s

uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py tests/unit/test_owner_live_tools.py
All checks passed!

uv --offline --cache-dir .uv-cache run pytest --basetemp .pytest-parent-nineteenth-repair-full -p no:cacheprovider -q
2,472 passed

uv --offline --cache-dir .uv-cache run pytest --collect-only -q -o addopts=
2,472 tests collected

Parent current-tree inventory
164 function files; 1,642 definitions; 42,459 physical lines; 37,736 nonblank lines; 36 strict C901 findings

git diff --check
exit 0; repository-wide LF-to-CRLF warnings only
```

## Explicit non-claims

No live provider, credential, AWS action, deployment, production concurrency, or live
Google Sheets behavior was exercised. This repair does not approve a gate, commit, push,
or deploy the dirty worktree; fresh independent review remains required.
