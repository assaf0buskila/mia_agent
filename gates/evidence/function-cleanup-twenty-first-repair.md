# Phase 1.5 function cleanup: twenty-first-review bounded repair

Date: 2026-08-30

## Scope and design

This repair changes only the owner Sheets pre-effect request binder and its focused
full-tool-path regression. It preserves one OwnerGraph agent, the ADR-042 bounded
Sheets exception, Sheets as an operational surface rather than a system of record, and
the existing fail-closed policy, idempotency, and adapter boundaries.

`_sheets_security_view` first masks exact JSON string literals, then applies NFKD
compatibility normalization, removes every Unicode `M*` category and `Cf` control, and
casefolds only for intent/security matching. It is used for requested/opposite EN+HE
operation counting, negation recognition, readable `CELL`/`ID`/`TARGET` collision
detection, and the positive clause operation scan. Internal grammar sentinels are
restored after casefolding only inside that security view.

Raw binding remains independent and precedes grammar normalization: decoded JSON string
values, spreadsheet ID, A1 target, validated adapter payload, and provider data are not
normalized, casefolded, or altered. ID/target roles are assigned by raw exact occurrence
before the grammar clause security view is made. Thus compatibility normalization exposes
an obscured instruction but cannot authorize a compatibility-equivalent identifier,
target, or quoted value.

## Persistent full-path regression matrix

`test_owner_sheets_twenty_first_unicode_security_view_is_effect_free_on_denial`
exercises `execute_tool` with the real principal/capability policy, real operation ledger
and `IdempotencyRow`, plus `FakeSheetsPort`.

- **126 cases total:** **103 denials** and **23 valid controls**.
- The 103 denials include all **63** M*/Cf-hidden EN/HE earlier verbs (seven Unicode
  characters across nine supported operation synonyms), the full-width `Ａｐｐｅｎｄ`
  duplicate, nine M*/Cf/full-width readable collisions, and 30 existing ambiguity,
  raw sentinel, and equal-ID/target extra-occurrence controls.
- Every denial snapshots and proves zero delta in live-port construction, operation claim,
  persisted idempotency rows, and fake-provider mutation.
- The 23 valid controls include `CELLULAR`, `myID`, and `TARGETS`; harmless prose;
  quoted operation/sentinel data; and all **18** equal-ID/target positives: values-first
  and target-first grammar for five English and four Hebrew operation verbs.
- Each valid control asserts idempotent replay. Earlier owner-live regressions continue to
  assert exact JSON literal order, rectangular grid shape, A1 dimensions, and mismatch
  denials.

## Local verification

```text
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py -q --basetemp .pytest-twentyfirst-owner-live -p no:cacheprovider
26 passed; 2 existing warnings.

uv --offline --cache-dir .uv-cache run pytest tests/unit/test_vnext_finalization.py tests/unit/test_website_handoff_owner_notify.py tests/unit/test_hot_handoff.py tests/unit/test_due_scan_worker.py tests/unit/test_comm_operating_model.py tests/unit/test_owner_notify.py tests/unit/test_website_client_graph.py tests/unit/test_vnext_graph_functions.py tests/unit/test_migrate.py tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py tests/unit/test_sheets.py tests/unit/test_vnext_principal.py tests/unit/test_vnext_owner_voice.py tests/unit/test_telegram.py tests/unit/test_transcribe.py tests/unit/test_telegram_owner_outbound.py tests/unit/test_telegram_owner_graph.py tests/unit/test_telegram_format.py --basetemp .pytest-twentyfirst-19files -p no:cacheprovider --disable-warnings
332 passed, 326 warnings in 20.30s.

uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py tests/unit/test_owner_live_tools.py
All checks passed.

git diff --check
exit 0; only repository-wide Windows LF-to-CRLF warnings were emitted.
```

## Parent current-tree verification

```text
Exact 19-file suite: 332 passed
Full pytest: 2,474 passed
Whole-tree Ruff: All checks passed
Origin binding: ok
Deterministic evals: 273/273
Inventory: 164 function files; 1,646 definitions; 42,529 physical lines; 37,797 nonblank lines; 36 strict C901 findings
git diff --check: exit 0; Windows line-ending warnings only
```

## Non-claims and residual risk

No network, AWS, credential, live Google Sheets/provider, deployment, production
concurrency, commit, push, or release action was performed. The positive grammar remains
intentionally strict and therefore denies unrecognized owner write phrasing. A fresh
independent review remains required before any gate or release decision.
