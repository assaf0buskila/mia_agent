# Phase 1.5 function cleanup: seventeenth-review P1 repair

Date: 2026-08-30

## Bounded repairs

- The residual non-string cell scan now accepts arbitrary non-word separators between
  an explicit English/Hebrew write/list introducer and a candidate. This closes the
  punctuation/bracket bypass while keeping quoted strings and the exact selected
  spreadsheet ID/target masked before scanning. Malformed signed candidates and valid
  or malformed container openers fail closed at the real tool boundary.
- Complete-cell binding is now ordered and two-dimensional: the owner-stated decoded
  JSON string sequence must equal the row-major `values` sequence exactly, and the
  payload must be rectangular with the exact row/column size of the bounded A1 target.
  This uses the same bounded A1 endpoint grammar as the downstream validator and adds
  no provider behavior. Update and append both receive exactly the owner-authorized
  target-shaped payload.

## Regression coverage

The counted `execute_tool` regression proves zero port construction, claim calls,
`owner_sheets_write` rows, and fake-provider operations for the review punctuation
reproductions and malformed signed forms across every English and Hebrew append/update
verb. It also proves the same zero-effect denial for swapped order, duplicate-order
permutation, a 2x2 permutation, ragged, partial, and reshaped payloads.

Positive controls prove idempotent retry for exact single-cell, ordered 1x2, ordered
2x2, and duplicate layouts; quoted scalar/container-looking values; escaped quote and
backslash strings; and numeric-looking ID/target text. Existing target tests were
updated from partial 1x2 payloads to the new exact target-layout contract.

## Local evidence

```text
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py -q --basetemp .pytest-seventeenth-repair-owner-live -p no:cacheprovider
24 passed

uv --offline --cache-dir .uv-cache run pytest [the exact 19 files from function-cleanup-repair-verification.md] --basetemp .pytest-seventeenth-repair-19files -p no:cacheprovider --disable-warnings
330 passed, 326 warnings in 9.00s

uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py tests/unit/test_owner_live_tools.py
All checks passed!

git diff --check
exit 0; repository-wide LF-to-CRLF warnings only
```

Parent verification independently passed the same **330-test** exact suite and the
complete **2,472-test** tree, plus whole-tree Ruff, origin binding, deterministic evals
(**273/273**), and diff-check. The reconciled current inventory is 164 function files,
1,640 definitions, 42,426 physical lines, 37,707 nonblank lines, and 36 strict C901
findings.

## Explicit non-claims

No live provider, credential, deployment, AWS operation, production concurrency, or
live Google Sheets behavior was exercised. This repair does not approve a gate, commit,
push, or deploy the dirty worktree; a fresh independent review remains required.
