# Phase 1.5 function cleanup: sixteenth-review P1 repair

Date: 2026-08-30

## Bounded change

The owner Sheets complete-cell binder now performs a bounded candidate scan over the
residual owner request after masking only the exact selected spreadsheet ID, selected
target, and quoted JSON-string candidates. It recognizes non-string JSON scalars and
containers only when they occupy an explicit English/Hebrew write or list position.
This denies a model-selected quoted subset when the authenticated owner also named an
unquoted JSON cell, before port construction, durable idempotency claim creation, or
fake-provider mutation. Raw selected IDs, targets, and quoted cell codepoints are not
normalized or changed.

The counted boundary regression covers signed integer, decimal, exponent, boolean, and
null forms; English `and`/`plus`/`with`; Hebrew plain-vav, hyphen, and maqaf forms;
append/update English and Hebrew verbs; and array/object extra cells before and after
the quoted value. Every rejection asserts `port=0`, `claim=0`, no
`owner_sheets_write` `IdempotencyRow`, and no fake-provider operation. Existing
positive controls continue to cover numeric-looking IDs/targets, quoted numeric text,
escaped strings, and exact raw-codepoint binding.

Follow-up: an unquoted `[` or `{` at one of those explicit cell positions now fails
closed even when its JSON container is malformed. The counted matrix covers valid and
malformed array/object openers before and after the quoted cell for English
`and`/`or`/`plus`/`with` and Hebrew plain-vav, hyphen, and maqaf forms, with the same
zero-effect assertions. Quoted brackets and exact selected ID/target text remain masked
and inert.

## Local evidence

```text
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py -q --basetemp .pytest-sixteenth-repair-owner-live -p no:cacheprovider
23 passed

uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py -q --basetemp .pytest-sixteenth-repair-owner-live-followup -p no:cacheprovider
23 passed

uv --offline --cache-dir .uv-cache run pytest [the exact 19 files from function-cleanup-repair-verification.md] --basetemp .pytest-sixteenth-repair-19files-summary -p no:cacheprovider --disable-warnings
329 passed, 326 warnings in 8.71s

uv --offline --cache-dir .uv-cache run pytest [the exact 19 files from function-cleanup-repair-verification.md] --basetemp .pytest-sixteenth-repair-19files-followup -p no:cacheprovider --disable-warnings
329 passed, 326 warnings in 8.90s

uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py tests/unit/test_owner_live_tools.py
All checks passed!

git diff --check
exit 0; repository-wide LF-to-CRLF warnings only
```

Parent verification independently passed the same **329-test** exact suite and the
complete **2,471-test** tree, plus whole-tree Ruff, origin binding, deterministic evals
(**273/273**), and diff-check. The reconciled current inventory is 164 function files,
1,638 definitions, 42,392 physical lines, 37,679 nonblank lines, and 36 strict C901
findings.

## Explicit non-claims

No live provider, credential, deployment, production concurrency, or live Google
Sheets behavior was exercised. This bounded repair does not approve the broader gate,
commit, push, or deploy the dirty worktree; a fresh independent review remains the
approval boundary.
