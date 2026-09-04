# Phase 1.5 function cleanup: twenty-second-review structural repair

Date: 2026-08-30

## Scope and whole-turn grammar

This bounded repair changes only the owner Sheets pre-effect request binder and its
owner-live regression. It preserves one production OwnerGraph, ADR-042's allowlisted,
bounded Sheets update/append exception, and Postgres as the system of record. No network,
provider, AWS, credential, deployment, or live-channel call was made.

`_has_authorized_sheets_cell_clause` now full-matches the complete normalized security-view
turn. The only accepted shape is one supported requested operation verb followed by exactly
one existing complete mutation clause (English values-first, English target-first, Hebrew
values-first, or Hebrew target-first), optionally preceded by one closed harmless preface.
There is no discarded prefix and no wildcard prose before or after the mutation clause.

The minimal preface set is derived from the existing intended positive owner regressions:

- no preface (bare operation);
- `Please` followed by bounded whitespace;
- exact `Please record this now:` followed by bounded whitespace; and
- `אלופה` followed by bounded whitespace, including its pointed form after the documented
  security-view mark removal.

`בבקשה` was not added because no existing product-positive regression requires it. Prefix
punctuation and spacing are therefore closed rather than treated as arbitrary harmless text.

The review-21 security view remains unchanged for masked JSON strings, NFKD compatibility
normalization, `M*`/`Cf` removal, operation/sentinel detection, and internal grammar
sentinels. Raw JSON string values, spreadsheet ID, A1 target, literal order, grid shape,
payload, and provider binding remain exact and are assigned before security-view matching.
Sole compatibility-obscured operation words remain supported under that contract; arbitrary
prefixes are not discarded.

## Persistent full-path regression

`test_owner_sheets_twenty_second_whole_turn_grammar_is_effect_free_on_denial` is a counted
`execute_tool` regression using the real principal/capability policy, operation ledger,
`IdempotencyRow`, and `FakeSheetsPort`.

- **42 cases total:** **34 denials** and **8 positive controls**.
- The denials include all **24** review-22 effectful prefix classes: nine mixed-script
  operation lookalikes, three sentinel confusables, seven split-operation forms, and five
  structural/multilingual forms. It also covers target-like and quoted-value prefixes,
  invalid suffixes, four compatibility-prefix controls, full-width ID, full-width target,
  and a compatibility-only literal substitution.
- Every denial proves a zero delta across port construction, operation claim, persisted
  `IdempotencyRow`, and fake-provider mutation.
- Positive controls cover bare, ordinary `Please`, exact record-now, and pointed `אלופה`
  prefaces, plus the four documented sole compatibility-obscured operation controls.
- The existing review-21 test remains a **126-case** matrix (**103 denials**, **23 positive
  controls**) and retains all **18** ID-equals-target values-first/target-first positives.

## Verification

```text
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py -q --basetemp .pytest-twentysecond-owner-live -p no:cacheprovider
27 passed; 2 existing warnings.

uv --offline --cache-dir .uv-cache run pytest tests/unit/test_vnext_finalization.py tests/unit/test_website_handoff_owner_notify.py tests/unit/test_hot_handoff.py tests/unit/test_due_scan_worker.py tests/unit/test_comm_operating_model.py tests/unit/test_owner_notify.py tests/unit/test_website_client_graph.py tests/unit/test_vnext_graph_functions.py tests/unit/test_migrate.py tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py tests/unit/test_sheets.py tests/unit/test_vnext_principal.py tests/unit/test_vnext_owner_voice.py tests/unit/test_telegram.py tests/unit/test_transcribe.py tests/unit/test_telegram_owner_outbound.py tests/unit/test_telegram_owner_graph.py tests/unit/test_telegram_format.py --basetemp .pytest-twentysecond-19files -p no:cacheprovider --disable-warnings
333 passed, 326 warnings in 18.48s.

uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py tests/unit/test_owner_live_tools.py
All checks passed.

git diff --check
exit 0; only repository-wide Windows LF-to-CRLF warnings were emitted.
```

## Parent current-tree verification

```text
Exact 19-file suite: 333 passed
Full pytest: 2,475 passed
Whole-tree Ruff: All checks passed
Origin binding: ok
Deterministic evals: 273/273
Inventory: 164 function files; 1,646 definitions; 42,537 physical lines; 37,805 nonblank lines; 36 strict C901 findings
git diff --check: exit 0; Windows line-ending warnings only
```

## Non-claims and residual risk

This is not a claim of AWS/deployment/live Telegram/live Sheets/provider execution, credential
validity, production concurrency, commit, push, or release. The grammar is intentionally
strict: future owner-facing prefaces require an explicit product-positive regression and a
reviewed grammar extension rather than implicit acceptance of conversational prose.
