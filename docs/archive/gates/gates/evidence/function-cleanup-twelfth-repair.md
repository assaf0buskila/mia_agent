# Twelfth HEAVY review repair — exact single Sheets target binding

Date: 2026-08-28

Scope is limited to the three findings in
`gates/evidence/function-cleanup-heavy-twelfth-review.md`. No provider, credential, AWS
resource, production state, graph, or canonical product contract was inspected or changed.

## Cause and fix

- The selected-target introducer pattern only accepted lowercase English `at` and `range`.
  English introducers are now case-insensitive through scoped regex flags, while the selected
  spreadsheet ID and A1 range remain raw exact matches.
- The preceding-introducer detector only recognized whitespace-separated chains. It now denies
  a preceding approved introducer when only punctuation, parentheses, or newlines intervene.
  This covers English/Hebrew mixtures and mixed English casing without treating intervening
  word text as a chain.
- Secondary-target scans only recognized uppercase A1 columns. They now use scoped
  case-insensitive ASCII matching for bare and bang-qualified A1 cells/ranges, including
  spaced tab names before the bang-qualified target.

The binder still runs before port construction, idempotency claim, and capability execution.
Raw quoted JSON cell-literal equality, Unicode `M*`/`Cf` quote masking, negation handling, and
argument prevalidation were not changed.

## Durable regressions

`tests/unit/test_owner_live_tools.py` now proves zero claim, adapter, idempotency-row, and fake
provider effects for punctuation, parenthesis, newline, mixed-language, and mixed-case repeated
introducer chains. It accepts one exact `at`, `RANGE`, or `RaNgE` target, with exact replay
idempotency. A new counting-port test rejects lowercase bare, bang-qualified, and spaced-tab
secondary A1 targets (including a lowercase range) before side effects.

Existing regressions still cover invalid JSON, non-string cells, raw decomposed/precomposed
literal mismatch, quoted-literal masking, selected lowercase/mixed-case spaced tabs, and
negation controls.

## Verification

All pytest runs used the offline workspace cache, a workspace-local basetemp, and no pytest
cache provider.

```text
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py \
  --basetemp .pytest-twelfth-repair-owner-live -p no:cacheprovider -q
18 passed, exit 0

uv --offline --cache-dir .uv-cache run python -c '<direct binder probe>'
[False, False, False, False, True, True], exit 0
# at, range; at (range; lowercase bare/bang secondary -> False
# RANGE; RaNgE single exact target -> True

uv --offline --cache-dir .uv-cache run pytest tests/unit/test_vnext_finalization.py \
  tests/unit/test_website_handoff_owner_notify.py tests/unit/test_hot_handoff.py \
  tests/unit/test_due_scan_worker.py tests/unit/test_comm_operating_model.py \
  tests/unit/test_owner_notify.py tests/unit/test_website_client_graph.py \
  tests/unit/test_vnext_graph_functions.py tests/unit/test_migrate.py \
  tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py \
  tests/unit/test_sheets.py tests/unit/test_vnext_principal.py \
  tests/unit/test_vnext_owner_voice.py tests/unit/test_telegram.py \
  tests/unit/test_transcribe.py tests/unit/test_telegram_owner_outbound.py \
  tests/unit/test_telegram_owner_graph.py tests/unit/test_telegram_format.py \
  --basetemp .pytest-twelfth-repair-combined -p no:cacheprovider -q
324 passed, exit 0

uv --offline --cache-dir .uv-cache run pytest \
  --basetemp .pytest-twelfth-repair-full -p no:cacheprovider -q
2,466 passed, exit 0

uv --offline --cache-dir .uv-cache run ruff check \
  app/tools/registries/owner_tools.py tests/unit/test_owner_live_tools.py
All checks passed!

uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py
273/273 passed, exit 0

git diff --check
exit 0; repository-wide LF-to-CRLF warnings only
```

## Explicit non-claims

This local SQLite/fake-adapter evidence proves the authorization ordering and target binding, not
live Google Sheets permissions or provider semantics, deployed behavior, credentials, or
production concurrency. A **thirteenth independent HEAVY review is still required** before any
approval, commit, or deploy decision.
