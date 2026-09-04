# Phase 1.5.4a evidence: owner boundary and typed reply loop

Date: 2026-08-28

## Outcome and exact cleanup metrics

| Surface | Before | After |
| --- | ---: | ---: |
| Owner allowlist validation before inbound webhook claim | 0 | 1 shared numeric match before the claim |
| Owner helper validation before `Principal.owner`/persistence | caller assumption | 1 local fail-closed validation |
| Stale "no Google Sheets read tool" prompt statements | 1 | 0 |
| Explicit bounded Sheets prompt capabilities | 0 | 3 (`sheets_read`, `sheets_update`, `sheets_append`) |
| Parallel calls executable after a whole-run ceiling is reached | unbounded within a batch | 0; every refused call gets a tool result |
| `OwnerReplyPort` implementations with sync `compose` | 2 of 3 | 0 of 3 |
| Caller-side `inspect.isawaitable` branches | 1 | 0 |
| Dead `calendar_booking` owner compatibility parameters | 1 | 0 |
| No-op owner `pass` statements | 2 | 0 |
| Repeated owner `persist_due_at = None` branches | 16 (plus initial assignment) | 0 (one named-set expression) |
| Owner-only adapter constructions on a prospect inbound path | 8 unconditional | 0; builders are inside the authenticated-owner branch |

## Four passes

1. Implemented the request-derived numeric owner gate in owner and legacy inbound paths; moved owner-only construction behind that gate; replaced the stale Sheets prompt; made reply composing async end-to-end; made the total cap batch-aware.
2. Re-read the owner flow against ADR-041/042 and retained the existing shared prospect adapters (calendar, booking, Sheets mirror, research) while moving only owner-only adapters.
3. Added adversarial regressions for direct owner-helper bypass, a three-call batch with one remaining slot, and prospect traffic whose eight owner-only builders fail if constructed.
4. Final focused verification is below. A broader neighboring suite was attempted but is classified separately under cross-leaf blockers; it does not invalidate the focused leaf gate.

## Verification

```text
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider \
  tests/unit/test_brain_agent.py tests/unit/test_owner_reply.py \
  tests/unit/test_vnext_principal.py tests/unit/test_vnext_inbound_client.py \
  tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py
68 passed, 83 warnings in 3.54s

uv --offline --cache-dir .uv-cache run ruff check [owned production files and tests]
All checks passed!

git diff --check
exit 0
```

## Post-review P3 repair: unauthorized owner batches construct nothing

`process_owner_texts` now filters valid numeric-authorized owner items before calling
`get_settings` or constructing any default adapter. An empty, malformed, non-numeric, or
unauthorized batch returns the same zero-result shape with no claim, persistence, tool,
model, or adapter side effect. Authorized batches retain the existing duplicate counters and
processing path. `test_owner_batch_rejects_before_constructing_default_adapters` patches every
owner builder (and settings) to raise and proves an unauthorized batch returns safely.

Final repair verification (quoted-value binding plus P3):

```text
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider \
  tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py \
  tests/unit/test_sheets.py tests/unit/test_vnext_principal.py
94 passed, 74 warnings in 3.76s

uv --offline --cache-dir .uv-cache run ruff check [owner API, Sheets surfaces, and focused tests]
All checks passed!

git diff --check
exit 0
```

## Clean-room P1 follow-up: exact operation, complete targets, and literal multiplicity

The Sheets binder now takes the requested operation: `sheets_update` accepts only
update/fill/enter (`עדכן`, `מלא`) wording; `sheets_append` accepts only append/add
(`הוסף`, `הכנס`) wording. Quoted cell strings are removed before verb recognition, so a
cell value cannot supply an operation word. Spreadsheet IDs and A1 ranges use complete-token
matching, rejecting `sheet-allowed-extra` for `sheet-allowed` and `KPI!A10` for `KPI!A1`.
Parsed JSON-string literals are counted as a multiset, so one `"x"` cannot authorize two
`x` cells. All binding checks still precede `_owner_sheets_port` and `claim_operation`.

The real-registry adversarial test now covers both operation inverses, ID/range suffix
collisions, and duplicate literal multiplicity with throwing claim/port sentinels. Existing
English and Hebrew positive requests continue to pass.

```text
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider \
  tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py \
  tests/unit/test_sheets.py tests/unit/test_vnext_principal.py
94 passed, 74 warnings in 3.83s
```

Strict C901 on the owned files reports only two pre-existing functions:
`app/api/owner.py::process_owner_item` (55) and
`app/tools/registries/owner_tools.py::_website_kpis` (14). The repaired binder does not
appear in that result.

Final operation-ambiguity follow-up: `הכנס` is append-only; it was removed from the
update verb set. The real-registry rejection test now proves that an explicit Hebrew
`הכנס` request cannot authorize `sheets_update` and reaches neither the throwing port
factory nor the throwing idempotency claim.

## Cross-leaf blocker, not changed here

The expanded owner regression command could not complete due to files outside this leaf:

- `tests/unit/test_owner_gmail_console.py` fails collection because `pytest` is not imported.
- `app/brain/vectors.py` raises `NameError: math is not defined`, causing two memory-tool assertions in `test_brain_agent.py` when the broader suite runs.

Those failures are concurrent/pre-existing worktree defects. This leaf did not edit either file. The 68-test focused leaf command was rerun after the final code change and passed.

## Post-review P1 repair: owner Sheets mutation binding

Fresh HEAVY review found that the former generic verb-plus-Sheet check did not bind a
model-selected mutation target or literal values to the authenticated owner's current
message. `owner_tools._has_bound_sheets_write_request` now requires the current message
to contain the explicit Sheets mutation verb/reference, exact `spreadsheet_id`, exact
bounded A1 `range`, and every non-empty literal cell value before constructing a port,
claiming idempotency, or invoking the named write capability. Reads retain their existing
allowlist-only scope. Empty mutation cells are denied, preventing a model-selected clear.

New adversarial HE/EN coverage submits explicit-looking requests with a missing ID, missing
range, mismatched range, or absent cell value and patches `claim_operation` to fail if it is
reached. All are refused with zero fake-port operations. After a second adversarial review
identified substring ambiguity (`1` inside `A1`, or `update` inside the command), cell binding
was tightened further: every cell must appear as its exact JSON-quoted literal (for example,
`"1"` or `"update"`) in the current owner message. ID/range remain verbatim exact. Positive
English multi-cell and existing Hebrew quoted-literal cases pass.

```text
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider \
  tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py tests/unit/test_sheets.py
86 passed, 56 warnings in 3.25s

uv --offline --cache-dir .uv-cache run ruff check \
  app/tools/registries/owner_tools.py app/capabilities/sheets.py \
  app/integrations/sheets.py app/core/config.py tests/unit/test_owner_sheets.py \
  tests/unit/test_owner_live_tools.py tests/unit/test_sheets.py
All checks passed!

git diff --check
exit 0
```

## Final clean-room P1 repair: pre-claim deterministic validation

The owner Sheets registry now runs the existing pure capability authorization and the
shared `validate_sheets_write_args` validator before `_owner_sheets_port` or
`LeadStore.claim_operation`. The execution handler reuses that exact validator, so the
pre-claim boundary cannot drift from allowlist, bounded A1, shape/cap, formula, and empty-cell
validation. Rejections for an outside allowlist ID, reversed A1 range,
over-wide shape, formula, empty literal, kill switch, and website principal now prove
`port == 0`, `claim == 0`, and zero `IdempotencyRow` records. A corrected request for the
same event then writes once and its replay is idempotent.

```text
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider \
  tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py \
  tests/unit/test_sheets.py tests/unit/test_vnext_principal.py
95 passed, 74 warnings in 3.90s

uv --offline --cache-dir .uv-cache run ruff check \
  app/api/owner.py app/tools/registries/owner_tools.py app/capabilities/sheets.py \
  app/integrations/sheets.py app/core/config.py tests/unit/test_owner_sheets.py \
  tests/unit/test_owner_live_tools.py tests/unit/test_sheets.py \
  tests/unit/test_vnext_principal.py
All checks passed!

uv --offline --cache-dir .uv-cache run ruff check \
  app/api/owner.py app/tools/registries/owner_tools.py app/capabilities/sheets.py \
  app/integrations/sheets.py app/core/config.py --select C901 --output-format concise
app/api/owner.py:164:11: C901 process_owner_item is too complex (55 > 10)
app/tools/registries/owner_tools.py:587:5: C901 _website_kpis is too complex (14 > 10)
Found 2 errors.
The repaired Sheets binder is absent; these two existing functions are outside this repair.

git diff --check
exit 0 (CRLF conversion warnings only from the pre-existing dirty worktree)
```

## Fourth HEAVY review P1 repair: deterministic Sheets semantic binding

The current owner turn now yields an operation classification after JSON-quoted cell
literals are removed. The requested operation requires an affirmative matching verb,
must have no matching negated verb, and must not share the turn with an affirmative
opposite Sheets mutation. English `do not` / `don't` / `never` and Hebrew `אל` / `לא`
prohibitions cannot authorize their prohibited operation. This is a pure local parser;
it introduces no model, routing step, discovery, or broader Sheets authority.

The owner literal multiset must now equal the flattened tool payload exactly. A missing
owner value in the tool call, an invented extra value, or duplicate mismatch fails the
binding before `_owner_sheets_port`, `claim_operation`, capability execution, and the
provider. Complete spreadsheet-ID/range token boundaries, the shared pre-claim allowlist,
A1 order/shape/cap/formula/empty checks, kill switch, principal policy, RAW writes, and
same-event idempotency remain unchanged.

`test_owner_sheets_semantic_binding_rejects_negation_conflicts_and_payload_mismatches`
uses the real owner registry, real SQLite `LeadStore`, and `FakeSheetsPort`. It proves:

- `Do not append. Update sheet-allowed at KPI!A1 with "x" in the Sheet` cannot call
  `sheets_append`.
- The fourth-review `"red" and "blue"` request cannot authorize a one-cell update.
- Hebrew negation, positive append/update conflicts, multiple target/value instructions,
  and a strict supplied-value superset are rejected.
- Every rejected case has zero adapter construction, zero claim calls, zero
  `IdempotencyRow` records, and zero fake-provider operations. Correcting the same event
  writes once; replaying it returns the already-handled result without a second provider
  call.

Four unlazy passes were completed: implementation; domain re-read against ADR-042 and the
existing pre-claim boundary; adversarial defect hunt covering negation/conflicts and
multiset directionality; and no-cost polish/verification.

### Natural-language negation follow-up

The negation parser additionally recognizes the production Hebrew forms `אל תוסיף`,
`לא להוסיף`, `אל תעדכן`, and `לא לעדכן`, while preserving the existing affirmative
operation vocabulary. English negation now recognizes ASCII apostrophe, curly apostrophe,
and apostrophe-less input: `don't`, `don’t`, and `dont`. These variants are negation-only;
they cannot create an affirmative operation. The same real-registry test proves each is
rejected before adapter construction, operation claim, `IdempotencyRow`, and fake-provider
call.

The same symmetry is now explicit for the remaining affirmative verbs without expanding
their authorization forms: append's existing `הכנס` has negation-only `אל תכניס` and
`לא להכניס`; update's existing `מלא` has negation-only `אל תמלא` and `לא למלא`.
Each is covered by the same zero-port/claim/row/provider sentinel loop.

```text
$env:MIA_DATABASE_URL='sqlite:///:memory:'; uv --offline --cache-dir .uv-cache run pytest \
  -p no:cacheprovider --basetemp .pytest-sheets-semantic-natural-negation \
  tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py \
  tests/unit/test_sheets.py tests/unit/test_vnext_principal.py
96 passed, 74 warnings in 3.87s

uv --offline --cache-dir .uv-cache run ruff check \
  app/tools/registries/owner_tools.py tests/unit/test_owner_sheets.py \
  tests/unit/test_owner_live_tools.py
All checks passed!

uv --offline --cache-dir .uv-cache run ruff check \
  app/tools/registries/owner_tools.py --select C901 --output-format concise
app/tools/registries/owner_tools.py:587:5: C901 _website_kpis is too complex (14 > 10)
Found 1 error. The repaired negation helper is absent.

git diff --check
exit 0 (pre-existing dirty-worktree CRLF conversion warnings only)
```

Final symmetry rerun:

```text
$env:MIA_DATABASE_URL='sqlite:///:memory:'; uv --offline --cache-dir .uv-cache run pytest \
  -p no:cacheprovider --basetemp .pytest-sheets-semantic-negation-symmetry \
  tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py \
  tests/unit/test_sheets.py tests/unit/test_vnext_principal.py
96 passed, 74 warnings in 4.09s

uv --offline --cache-dir .uv-cache run ruff check \
  app/tools/registries/owner_tools.py tests/unit/test_owner_sheets.py \
  tests/unit/test_owner_live_tools.py
All checks passed!

uv --offline --cache-dir .uv-cache run ruff check \
  app/tools/registries/owner_tools.py --select C901 --output-format concise
app/tools/registries/owner_tools.py:587:5: C901 _website_kpis is too complex (14 > 10)
Found 1 error. The repaired negation helper is absent.

git diff --check
exit 0 (pre-existing dirty-worktree CRLF conversion warnings only)
```

## Fifth HEAVY review P1 repair: exact Sheet target and normalized-empty binding

The pre-port binder now receives the effective configured allowlist. After stripping
JSON-quoted literals, it requires exactly the model-selected allowlisted spreadsheet ID
and exactly the model-selected A1 target reference. A second complete allowlisted ID or
second bounded A1 reference, including a different sheet-tab name with the same A1 core,
fails closed. Quoted ID/range text cannot supply a target. Complete-token matching remains
in force, so prefix/suffix collisions do not become targets.

The shared `_normalize_sheet_values` validator now rejects any cell that becomes empty
after trimming. This is used by both the owner pre-claim validation and all typed adapter
paths. Non-empty cells retain internal spaces; only their former boundary trimming remains.

`test_owner_sheets_write_binds_exactly_one_unquoted_target_before_side_effects` uses the
real owner registry, real SQLite `LeadStore`, and `FakeSheetsPort` to cover the reviewer's
two-target reproducer, same-ID/two-range, two-ID/same-range, different-tab/same-core,
quoted targets, token collisions, exact success, and corrected same-event replay. Every
denial proves zero adapter construction, claims, idempotency rows, and fake-provider calls.
The existing semantic sentinel adds the exact whitespace-only review payload and proves
the same zero effects; `test_owner_sheets_normalization_preserves_internal_spaces_in_nonempty_cells`
proves `"x  y"` reaches the RAW adapter payload unchanged.

```text
$env:MIA_DATABASE_URL='sqlite:///:memory:'; uv --offline --cache-dir .uv-cache run pytest \
  -p no:cacheprovider --basetemp .pytest-sheets-fifth-repair-final \
  tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py \
  tests/unit/test_sheets.py tests/unit/test_vnext_principal.py
98 passed, 74 warnings in 4.03s

uv --offline --cache-dir .uv-cache run ruff check \
  app/tools/registries/owner_tools.py app/integrations/sheets.py \
  app/capabilities/sheets.py tests/unit/test_owner_sheets.py \
  tests/unit/test_owner_live_tools.py tests/unit/test_sheets.py
All checks passed!

uv --offline --cache-dir .uv-cache run ruff check \
  app/tools/registries/owner_tools.py app/integrations/sheets.py \
  app/capabilities/sheets.py --select C901 --output-format concise
app/tools/registries/owner_tools.py:587:5: C901 _website_kpis is too complex (14 > 10)
Found 1 error. The repaired binding and normalization helpers are absent.

git diff --check
exit 0 (pre-existing dirty-worktree CRLF conversion warnings only)
```

```text
$env:MIA_DATABASE_URL='sqlite:///:memory:'; uv --offline --cache-dir .uv-cache run pytest \
  -p no:cacheprovider --basetemp .pytest-sheets-semantic-final \
  tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py \
  tests/unit/test_sheets.py tests/unit/test_vnext_principal.py
96 passed, 74 warnings in 4.02s

uv --offline --cache-dir .uv-cache run ruff check \
  app/tools/registries/owner_tools.py tests/unit/test_owner_sheets.py \
  tests/unit/test_owner_live_tools.py
All checks passed!

uv --offline --cache-dir .uv-cache run ruff check \
  app/tools/registries/owner_tools.py --select C901 --output-format concise
app/tools/registries/owner_tools.py:587:5: C901 _website_kpis is too complex (14 > 10)
Found 1 error.
The repaired semantic-binding helpers are absent from this strict measurement.

git diff --check
exit 0 (pre-existing dirty-worktree CRLF conversion warnings only)
```
