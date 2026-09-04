# Phase 1.5 function cleanup: eighteenth-review P1 repair

Date: 2026-08-30

## Bounded repair

The owner Sheets residual candidate scan now treats unmatched JSON-container closers
(`]`, `}`, and adjacent closer sequences) symmetrically with openers at an explicit
English/Hebrew write/list cell position. Exact selected spreadsheet IDs, exact targets,
and every valid JSON-quoted string remain length-preservingly masked before scanning, so
quoted closer/container-looking content remains inert and exact.

The same bounded grammar recognizes `NaN`, `Infinity`, `-Infinity`, `None`, and
single-quoted pseudo-strings only after an explicit write/list introducer. They are
rejected as unquoted extra cells; ordinary prose outside that position, Hebrew `את`,
and target-first write wording are not added as candidates.

## Regression coverage

The counted real `execute_tool` regression exercises closers `]`, `}`, `])`, `}}`, and
`]}` before and after the quoted cell across all English and Hebrew append/update verbs;
English `and`/`or`/`plus`/`with`/comma/semicolon; and Hebrew plain-vav, hyphen-vav,
maqaf-vav, comma, and semicolon. Every denial verifies no port construction, no claim,
no owner-write idempotency row, and no fake-provider mutation. It additionally covers
the bounded JSON-like pseudo-cell spellings above.

Positive controls retain idempotent exact layouts and verify that quoted `]`, `]}`,
arrays, objects, scalar-looking text, escaped quotes, and escaped backslashes are exact
cell strings rather than residual candidates.

## Local evidence

```text
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py -q --basetemp .pytest-eighteenth-repair-owner-live -p no:cacheprovider
24 passed

uv --offline --cache-dir .uv-cache run pytest [the exact 19 files from function-cleanup-repair-verification.md] --basetemp .pytest-eighteenth-repair-19files -p no:cacheprovider --disable-warnings
330 passed, 326 warnings in 9.57s

uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py tests/unit/test_owner_live_tools.py
All checks passed!

git diff --check
exit 0; repository-wide LF-to-CRLF warnings only
```

Parent verification independently passed the same **330-test** exact suite and the
complete **2,472-test** tree, plus whole-tree Ruff, origin binding, deterministic evals
(**273/273**), and diff-check. The reconciled current inventory is 164 function files,
1,640 definitions, 42,429 physical lines, 37,710 nonblank lines, and 36 strict C901
findings.

## Explicit non-claims

No live provider, credential, deployment, AWS action, production concurrency, or live
Google Sheets behavior was exercised. This repair does not approve a gate, commit, push,
or deploy the dirty worktree; a fresh independent review remains required.
