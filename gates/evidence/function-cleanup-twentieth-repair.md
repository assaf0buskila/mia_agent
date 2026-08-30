# Phase 1.5 function cleanup: twentieth-review bounded repair

Date: 2026-08-30

## Scope and design

This repair changes only the owner Sheets pre-effect request binder and its focused
full-tool-path regressions. It preserves the one OwnerGraph agent, the ADR-042 bounded
Sheets exception, Postgres as the system of record, and the existing policy,
idempotency, and adapter boundaries.

The requested operation is now counted across its English and Hebrew synonyms. Exactly
one occurrence must own a complete accepted mutation clause; a second same-operation
verb, an affirmative opposite operation, or any standalone negation denies before port
construction. The binder also rejects raw, case-insensitive `CELL`, `ID`, and `TARGET`
tokens and private-use sentinel boundaries outside JSON strings. Quoted lookalikes remain
literal data.

When the allowed spreadsheet ID equals the selected A1 range, the binder builds both
possible ID/target role assignments and accepts only the assignment that satisfies one
complete grammar. This preserves exact token multiplicity while allowing valid Hebrew
target-first order.

## Regression matrix

`test_owner_sheets_twentieth_repair_binds_one_operation_clause_before_effects` invokes
the real `execute_tool` path with `FakeSheetsPort` and a real operation ledger.

- Denials: 20 formerly effectful classes — five English same-operation prefixes, four
  Hebrew same-operation prefixes, one cross-language prefix, one operation-verb prefix,
  and nine raw readable-placeholder variants. Five raw private-use boundary/sequence
  controls are also denied.
- For every denial, the test snapshots and preserves all four effect counters: port
  construction, operation claim, persisted idempotency row, and fake-provider mutation.
- Positives: harmless prose before the sole operation; quoted readable and private-use
  lookalikes; exact ordered A1 grid binding with an order-mismatch denial; idempotent
  replay; and Hebrew target-first with `spreadsheet_id == range == "A1"`.

## Local verification

```text
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py -q --basetemp .pytest-twentieth-owner-live -p no:cacheprovider
25 passed; 2 existing warnings.

uv --offline --cache-dir .uv-cache run pytest tests/unit/test_vnext_finalization.py tests/unit/test_website_handoff_owner_notify.py tests/unit/test_hot_handoff.py tests/unit/test_due_scan_worker.py tests/unit/test_comm_operating_model.py tests/unit/test_owner_notify.py tests/unit/test_website_client_graph.py tests/unit/test_vnext_graph_functions.py tests/unit/test_migrate.py tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py tests/unit/test_sheets.py tests/unit/test_vnext_principal.py tests/unit/test_vnext_owner_voice.py tests/unit/test_telegram.py tests/unit/test_transcribe.py tests/unit/test_telegram_owner_outbound.py tests/unit/test_telegram_owner_graph.py tests/unit/test_telegram_format.py --basetemp .pytest-twentieth-19files -p no:cacheprovider --disable-warnings
331 passed, 326 warnings in 10.78s

uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py tests/unit/test_owner_live_tools.py
All checks passed.

git diff --check
exit 0; only repository-wide Windows LF-to-CRLF warnings were emitted.
```

## Parent current-tree verification

```text
Exact 19-file suite: 331 passed
Full pytest: 2,473 passed
Whole-tree Ruff: All checks passed
Origin binding: ok
Deterministic evals: 273/273
Inventory: 164 function files; 1,645 definitions; 42,507 physical lines; 37,778 nonblank lines; 36 strict C901 findings
git diff --check: exit 0; Windows line-ending warnings only
```

## Non-claims and residual risk

No network, AWS, credential, live Google Sheets/provider, deployment, production
concurrency, commit, push, or release action was performed. The grammar is intentionally
strict and denies unrecognized write sentence forms; a fresh independent review remains
required before any gate or release decision.
