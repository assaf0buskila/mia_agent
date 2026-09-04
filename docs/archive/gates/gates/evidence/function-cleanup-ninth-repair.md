# Ninth HEAVY review repair — Sheets mutation binding

Date: 2026-08-28

Scope was limited to the two P1 findings in
`function-cleanup-heavy-ninth-review.md`. No runtime architecture, provider adapter, AWS
configuration, credentials, or production state was changed or inspected.

## Repair

- `app/tools/registries/owner_tools.py` now normalizes candidate owner text with NFKD and
  removes Unicode combining marks only for explicit-negation detection. Standalone Hebrew
  `לא`/`אל` therefore remains a prohibition with niqqud or cantillation, while JSON-quoted
  cell literals remain masked before that test and Hebrew words containing those letters are
  not standalone negators.
- The unquoted A1 binding now requires the exact requested range after an explicit target
  introducer, masks that selected span, then rejects every remaining bare or bang-qualified
  bounded A1 target. This accepts validator-legal lowercase and mixed-case spaced tabs,
  ignores quoted cell literals, and cannot split the selected `A1:B1` range into a second
  target.
- The existing pre-port/pre-claim binding remains unchanged: a denied proposal constructs no
  Sheets adapter, claims no operation, creates no idempotency row, and performs no provider
  write. Regressions also prove correction of the same event then has one write and an
  idempotent replay.

## Regressions added

`tests/unit/test_owner_live_tools.py` covers `לֹא`, `אַל`, extra combining mark variants,
embedded pointed Hebrew-word control, and a quoted pointed negator cell value. It also covers
second targets separated by comma, semicolon, slash, parentheses, and newline (including a
lowercase spaced tab and a bare cell), positive quoted target-looking cell values, positive
uppercase/lowercase/mixed-case spaced tabs, and no suffix match from the selected range.

## Verification

All pytest commands used the offline workspace cache, a workspace-local basetemp, and no
pytest cache provider.

```text
uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py \
  tests/unit/test_owner_sheets.py tests/unit/test_sheets.py tests/unit/test_vnext_principal.py \
  --basetemp .pytest-ninth-repair4 -p no:cacheprovider
101 passed, 74 warnings in 3.40s

uv --offline --cache-dir .uv-cache run pytest tests/unit/test_owner_live_tools.py \
  -k "bounded_negation_modifiers or binds_exactly_one_unquoted_target or target_extraction_ignores" \
  --basetemp .pytest-ninth-repro2 -p no:cacheprovider
3 passed, 13 deselected, 2 warnings in 1.09s

uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py \
  tests/unit/test_owner_live_tools.py
All checks passed!

uv --offline --cache-dir .uv-cache run ruff check app/tools/registries/owner_tools.py \
  --select C901 --output-format concise
app/tools/registries/owner_tools.py:587:5: C901 `_website_kpis` is too complex (14 > 10)
```

The C901 result is the pre-existing `_website_kpis` inventory entry; this repair did not
modify that function and introduced no new C901 finding. `git diff --check` exited 0 with
only repository-wide line-ending warnings.

## Explicit non-claims

This is fake-adapter/local-SQLite evidence only. It does not prove live Google Sheets
permissions, provider semantics, deployment, credentials, or production behavior. The review
gate remains open pending a fresh tenth HEAVY review.
