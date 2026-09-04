# Thirteenth HEAVY review repair — Sheets target-binding grammar

Date: 2026-08-28

## Scope

This repair addresses only the two P1 findings in
`gates/evidence/function-cleanup-heavy-thirteenth-review.md`. It changes the owner-side
Sheets request binder and its direct unit regressions. No graph topology, provider,
credential, environment file, completion gate, or live external system was changed or read.

## Cause and exact grammar fix

1. The preceding-introducer tail excluded `!`, `:`, and `-`, so those separators made a
   preceding approved introducer invisible. The introducer boundaries now use Unicode word
   boundaries and the tail accepts every non-word separator. Thus repeated exact approved
   introducers fail closed when separated only by punctuation, brackets, quotes, emoji,
   whitespace/newlines, Unicode marks, or format controls. English matching remains scoped
   case-insensitive; the selected target remains a raw exact match.
2. The residual-target scan recognized only relative bounded cells/ranges. It is now one
   case-insensitive ASCII A1 grammar covering bounded cells/ranges, absolute and mixed cells,
   whole columns, and whole rows. The exact selected raw target is blanked first, then any
   remaining unquoted actual A1 reference, bare or bang-qualified (including quoted/spaced
   tab names), rejects before port construction, idempotency claim, or provider execution.

JSON string masking, M*/Cf negation quote masking, raw literal equality, ID allowlisting,
validator bounds, and idempotency behavior were not changed.

## Durable regressions

`tests/unit/test_owner_live_tools.py` uses a counted port and counted idempotency claim plus
the fake Sheets provider. It proves zero claims, ports, idempotency rows, and provider effects
for `at: range`, `at! RANGE`, `at- RaNgE`, Hebrew/English chains with colon, and a chain with
a Unicode mark, format control, emoji, brackets, and mixed casing. It retains successful,
idempotent single exact `at`, `RANGE`, and `RaNgE` bindings.

The same direct no-side-effect test now rejects residual `$B$2`, `$B2`, `B$2`, `B:B`,
`$B:$D`, `2:2`, `$2:$4`, `Other!$B$2`, `Other!2:2`, and `'Other Tab'!B:B`, alongside the
previous lowercase/bang-qualified/spaced-tab bounded references. Quoted JSON cell literals
remain accepted by the existing exact raw-literal test.

## Verification

All pytest commands used the workspace offline cache, a workspace-local basetemp, and no
pytest cache provider.

```text
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py \
  --basetemp .pytest-thirteenth-repair-owner-live -p no:cacheprovider -q
18 passed, exit 0

uv --offline --cache-dir .uv-cache run pytest [the established 19-file function-cleanup suite] \
  --basetemp .pytest-thirteenth-repair-combined -p no:cacheprovider -q
324 passed, exit 0

uv --offline --cache-dir .uv-cache run pytest \
  --basetemp .pytest-thirteenth-repair-full -p no:cacheprovider -q
completed successfully, exit 0; output capture truncated before pytest's final summary

uv --offline --cache-dir .uv-cache run pytest --collect-only -q -o addopts=
2,466 tests collected, exit 0

uv --offline --cache-dir .uv-cache run ruff check app tests scripts
All checks passed!, exit 0

uv --offline --cache-dir .uv-cache run python scripts\\assert_origin_bind.py
origin-bind: ok, exit 0

uv --offline --cache-dir .uv-cache run python scripts\\eval_diff.py
273/273 passed, exit 0

uv --offline --cache-dir .uv-cache run ruff check --select C901 app scripts \
  --output-format concise --exit-zero
C901_COUNT=36, matching current evidence

Inventory reconciliation: 164 current files, 164 audit rows, 164 unique audit rows,
zero duplicate/missing/extra paths, exit 0

git diff --check
exit 0; existing repository-wide LF-to-CRLF warnings only
```

## Explicit non-claims

This local SQLite/fake-adapter evidence proves the fail-closed authorization ordering and
binding behavior. It does not prove Google Sheets permissions/provider behavior, deployed
runtime behavior, production concurrency, credentials, or live external systems. A
**fourteenth independent HEAVY review remains required** before approval, commit, or deploy.
