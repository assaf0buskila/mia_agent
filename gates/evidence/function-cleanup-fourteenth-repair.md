# Fourteenth HEAVY review repair — Sheets target binding

Date: 2026-08-28

## Scope

This repair addresses exactly the P1 and P2 findings in
`function-cleanup-heavy-fourteenth-review.md`. It changes only the owner-side
Sheets request binder and its direct unit regressions. No graph topology,
provider, credential, environment file, completion gate, or live external
system was changed or read.

## Cause and exact fix

1. Python treats U+005F LOW LINE as `\\w`, which let `at_ range` hide a
   preceding approved introducer. The preceding-introducer tail now treats
   underscore as a separator alongside Unicode non-word separators while using
   an alphanumeric boundary for the introducer itself. Thus `at_ range`, mixed
   English/Hebrew variants, and repeated underscores reject before the port,
   idempotency claim, row, and provider. `at_foo range` intentionally remains
   valid: `at` is part of a longer alphanumeric word, not an approved
   introducer chain.
2. The residual A1 scan interpreted A1-looking fragments inside an explicitly
   mentioned allowlisted spreadsheet ID as a second target. It now blanks only
   complete raw occurrences of the selected spreadsheet ID, as well as the
   selected raw A1 target, before scanning residual references. This uses the
   same exact token boundaries as allowlist mention detection; it does not
   case-normalize the ID, relax the one-allowlisted-ID set check, or blank a
   separate A1 reference before or after the ID.

Raw JSON codepoint equality, quoted-value inertness, target/ID exactness,
negation behavior, existing cell trimming, policy validation, idempotency, and
the no-side-effect ordering are unchanged.

## Durable regressions

`tests/unit/test_owner_live_tools.py` now proves with a counted Sheets-port
constructor, counted idempotency claim, real `IdempotencyRow` count, and fake
provider operations that `at_ range`, `AT___ RaNgE`, and Hebrew/English
underscore chains fail with zero effects. It also proves `at_foo range` remains
one valid target and is idempotent.

The direct opaque-ID regression accepts exact configured IDs `sheet-B2`, `A1`,
and `opaque_sheet-B2`, each with a counted idempotent append. It rejects the
same `sheet-B2` request if a separate `Other!B2` appears before or after the
selected target, without adding claims, ports, rows, or provider operations.

### Parent follow-up: repeated A1-looking opaque ID

The initial repair blanked every complete selected-ID occurrence. That was too broad
when the ID itself was A1-looking: a second bare `B2` could be hidden alongside a
legitimate opaque ID `B2`. The binder now requires exactly one complete raw ID
occurrence outside the selected-target span and blanks only that occurrence. Zero or
multiple ID occurrences fail closed. A target span overlapping a bare ID identical to
the selected range is excluded from this count, so `spreadsheet_id=A1` with the single
target `A1` remains a valid exact binding when the validator accepts it.

New counted regressions prove: one `B2` ID plus `KPI!A1` is idempotently accepted;
an additional bare `B2` before or after, or `Other!B2`, has zero additional port,
claim, idempotency-row, or provider effects. The validator accepts the safe overlap
control `spreadsheet_id=A1`, `range=A1`, with exactly one distinct ID mention and one
selected target; it is also idempotent.

## Verification

All commands used the workspace offline cache and a workspace-local pytest
base temporary directory.

```text
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py \
  --basetemp .pytest-fourteenth-repair-owner-live -p no:cacheprovider -q
20 passed after the parent follow-up, exit 0

uv --offline --cache-dir .uv-cache run pytest [established 19-file suite] \
  --basetemp .pytest-fourteenth-repair-combined -p no:cacheprovider -q
326 passed after the parent follow-up, exit 0

uv --offline --cache-dir .uv-cache run pytest \
  --basetemp .pytest-fourteenth-repair-full -p no:cacheprovider -q
completed through the full suite, exit 0; console progress capture was truncated

uv --offline --cache-dir .uv-cache run pytest --collect-only -q -o addopts=
2,467 tests collected, exit 0

uv --offline --cache-dir .uv-cache run ruff check app tests scripts
All checks passed, exit 0

uv --offline --cache-dir .uv-cache run python scripts\\assert_origin_bind.py
origin-bind: ok, exit 0

uv --offline --cache-dir .uv-cache run python scripts\\eval_diff.py
273/273 passed, exit 0

uv --offline --cache-dir .uv-cache run ruff check --select C901 app scripts \
  --output-format concise --exit-zero
36 findings, exit 0 (inventory metric)

git diff --check
exit 0; existing repository-wide LF-to-CRLF warnings only
```

Current function inventory: 164 function-bearing files, 1,637 definition lines,
42,307 physical lines, and 37,599 nonblank lines. The audit matrices were not
edited by this repair.

## Explicit non-claims

This evidence proves local fake-adapter binding and ordering only. It does not
prove Google Sheets permissions or provider behavior, deployed runtime behavior,
production concurrency, credentials, or any live external system. A fresh
**fifteenth independent HEAVY review remains required** before approval, commit,
or deployment.
