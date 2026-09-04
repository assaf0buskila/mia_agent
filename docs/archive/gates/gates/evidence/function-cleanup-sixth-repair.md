# Sheets sixth-review repair

Date: 2026-08-28
Scope: repair only the two P1 findings in `function-cleanup-heavy-sixth-review.md`.

## Changed symbols

- `app/tools/registries/owner_tools.py`
  - `_sheets_operation_mentions`: treats a standalone English `not` immediately
    before `append`/`add` or `update`/`fill`/`enter` as a prohibition.
  - `_A1_TARGET_RE`: captures the complete, owner-stated bounded A1 target,
    including an ASCII spaced tab prefix, after English/Hebrew target introducers.
  - `_has_exact_single_sheets_target`: requires exactly one extracted target and
    exact equality to the validated tool range; it no longer reconstructs a tab
    from a whitespace suffix or relies on a set of core cell references.
- `tests/unit/test_owner_live_tools.py`
  - Adds direct real-registry SQLite/FakeSheetsPort cases for `Please not append`,
    `Please not update`, spaced-tab suffix selection, two spaced targets, repeated
    spaced targets, different spaced ranges, and exact spaced-tab replay.

## Four-pass review

1. Complete implementation: both findings are checked in `_has_bound_sheets_write_request`,
   before `_owner_sheets_port`, `claim_operation`, and provider dispatch.
2. Expert reread: preserved JSON-quoted-cell removal, existing English/Hebrew operation
   forms, conflicting-operation denial, allowlist/principal/policy validation, and replay.
3. Defect hunt: NFKC normalization still happens before binding; target extraction requires
   an explicit target introducer and exact whole target equality, rejecting tab/range
   prefix/suffix collisions, quotes, and repeated or distinct complete targets. Hebrew
   `בטווח` and `את` target forms remain covered by the maintained suite.
4. Free polish: no new dependency, provider, graph, model, or runtime agent; the change is
   limited to the owner registry and its regression tests.

## Evidence

The new direct-registry test counts `_owner_sheets_port` and `claim_operation`, queries the
real SQLite idempotency table, and inspects `FakeSheetsPort.owner_operations` after every
denial. Each new denial proves: port `0`, claim `0`, idempotency rows `0`, provider operations
`0`. Exact `Foo Bar!A1` succeeds once; an exact replay returns success without a duplicate
provider operation.

Commands used (all local and offline):

```powershell
$env:MIA_DATABASE_URL='sqlite:///:memory:'
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
  --basetemp .pytest-sixth-repair-suite-final `
  tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py `
  tests/unit/test_sheets.py tests/unit/test_vnext_principal.py
# 99 passed, 74 warnings in 4.97s

uv --offline --cache-dir .uv-cache run ruff check `
  app/tools/registries/owner_tools.py tests/unit/test_owner_live_tools.py
# All checks passed

uv --offline --cache-dir .uv-cache run ruff check `
  app/tools/registries/owner_tools.py --select C901 --output-format concise
# one pre-existing finding: _website_kpis at line 587 (14 > 10); no changed symbol is reported

git diff --check
# exit 0 (Git emitted only pre-existing working-tree CRLF notices)
```

## Non-claims

This is not live Google Sheets, AWS, deployment, production database, or real-provider proof.
It does not claim complete-tree test status, alter existing review evidence/gates/plans, or
change Sheets authorization beyond the repaired owner-text binding boundary.

## Follow-up: modifier-bearing English prohibitions

The direct phrase matcher had a gap: it accepted only an immediately adjacent operation verb,
so `not to append`, `not ever append`, `do not ever append`, and `never ever append` were not
all denied. `_sheets_operation_mentions` now permits at most two `to`/`ever` modifiers between
the negative head and its English operation verb. This is deliberately bounded phrase matching,
not general language parsing.

The real-registry counter test now denies those four append forms plus `not to update`, `not
ever fill`, and `do not ever enter`; each proves zero port construction, claims, idempotency
rows, and fake-provider operations. A positive append whose JSON-quoted cell is `"not ever
append"` succeeds, proving quoted data is still excluded from instruction classification.
The existing `Don't append; update ...` case remains operation-specific: prohibited append is
denied while its separately requested update is handled by the existing ambiguity policy.

Follow-up commands:

```powershell
$env:MIA_DATABASE_URL='sqlite:///:memory:'
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
  --basetemp .pytest-sixth-repair-modifier `
  tests/unit/test_owner_live_tools.py::test_owner_sheets_semantic_binding_rejects_negation_conflicts_and_payload_mismatches
# 1 passed, 2 warnings in 1.12s
```

The owned Ruff/C901/diff checks were rerun after this test addition; owned Ruff passes,
`_website_kpis` remains the sole pre-existing C901 report, and `git diff --check` exits 0.
