# Phase 1.5 function cleanup: fifteenth independent HEAVY review

Timestamp: `2026-08-28T10:49:39.1213625+03:00`

Mode: fresh adversarial review of the frozen dirty worktree; production code and tests
were read-only

Decision: **FAIL**

## Scope and method

I loaded `AGENTS.md`, `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, and
`docs/DECISIONS.md` in that order. I then loaded `PLAN.md`, the four requested gate
files, the fourteenth-review/repair/verification/synthesis evidence, current code,
current tests, and the current diff/inventory. I did not inspect `.env`, secret values,
`docs/archive`, AWS, or live providers. Historical pass claims were rerun locally rather
than treated as current evidence.

The Sheets review used a temporary reviewer-only Python probe over the real
`execute_tool` path with a numeric owner `Principal`, `FakeSheetsPort`, an in-memory
SQLite `LeadStore`, counted `_owner_sheets_port` and `claim_operation` calls, real
`IdempotencyRow` counts, and fake-provider operation counts. The probe was removed after
its output was captured. Its first launch used the wrong environment alias and stopped
at SQLAlchemy URL parsing before any case executed. The successful probe explicitly set
only the reviewer process `MIA_DATABASE_URL` to `sqlite:///:memory:`; no existing setting
or secret value was read or printed.

## Findings

### P0

None found.

### P1

1. **Unicode mark/format controls inside a secondary A1 token bypass residual-target
   rejection and reach the Sheets provider.**

   `_A1_REFERENCE_RE` at `app/tools/registries/owner_tools.py:922-924` recognizes only
   contiguous raw A1 syntax. `_has_exact_single_sheets_target` blanks the selected target
   and chosen spreadsheet ID, then searches the otherwise raw `remaining` string at
   lines 963-967. Unlike the negation path, it does not remove visually inert Unicode
   `M*` or `Cf` characters before the security scan.

   With exact selected target `KPI!A1` appearing once, each of the following secondary
   targets was accepted before and after it: `B<ZWJ>2`, `B<ZWNJ>2`, `B<LRM>2`,
   `B<CGJ>2`, `B<COMBINING ACUTE>2`, and `Other!B<ZWJ>2`. All **12** cases returned
   `ok=True` and had the same counted delta:
   `port=1`, `claim=1`, `IdempotencyRow=1`, `provider append=1`.

   This is the same authority property as the Wave 9/13 residual-target repairs: an
   authenticated owner who states two visually A1-like targets must not let the model
   choose one. The effect path is live-port construction at line 746, operation claim at
   line 756, and capability/provider execution at line 761.

2. **Malformed or non-string additional requested cells are masked/ignored, allowing a
   model-selected subset to mutate the provider.**

   `_JSON_STRING_RE` at line 838 is a permissive quote masker, not a JSON validator.
   `_quoted_literal_counts` at lines 971-978 silently continues on
   `JSONDecodeError`. The final authorization comparison therefore sees only the valid
   quoted subset. The counted probe showed each of these owner turns wrote only `x`:

   - append valid `"x"` and malformed JSON string `"\q"`;
   - append valid `"x"` and a raw-newline quoted string;
   - append valid JSON string `"x"` and unquoted JSON number `123`.

   Each returned `ok=True` with `port=1`, `claim=1`, `IdempotencyRow=1`, and one fake
   append. By contrast, an invalid-only quoted cell, a non-string model payload cell,
   a whitespace-only model cell, and a raw Unicode-equivalent substitution all denied
   with zero effects. The gap is specifically that an additional invalid/non-string
   owner-requested cell is not part of the multiset and the model is allowed to drop it.
   That violates the existing complete-multiset rule and the documented requirement that
   every requested cell be an exact JSON-quoted string.

### P2

1. **A different allowlisted opaque ID that exactly overlaps the selected A1 span causes
   a valid request to be denied.**

   `mentioned_ids` at lines 936-940 scans the entire unquoted turn before the selected
   target span is known/excluded. With allowlist `{sheet-main, KPI!A1}`, the valid request
   `append "x" to sheet-main at KPI!A1 in the Sheet` is treated as mentioning both
   spreadsheet IDs because the second configured ID is exactly the selected range.
   The equality check at line 965 then returns false.

   The real probe returned `ok=False` with zero port, claim, idempotency, or provider
   effects. This is fail-closed but breaks a validator-legal opaque allowlist: unrelated
   configured IDs must not become owner mentions merely because one overlaps the selected
   target. The binder must still mask only one raw complete chosen ID outside the selected
   target and must not hide a real secondary A1.

### P3

1. `app/graph/owner_agent.py:104-108` advertises bounded `sheets_update` and
   `sheets_append`, while line 148 still says, "You have read tools only." The Python
   authorization path prevents this wording conflict from creating additional authority,
   but it can make the single production owner agent refuse a valid bounded Sheets write.
   The narrow authenticated/allowlisted Sheets exception should be stated explicitly.

## Mandatory Sheets adversarial results

### Separator and introducer behavior

- Exhaustive runtime sweep covered all Unicode `M*`, `P*`, `S*`, and `Cf` codepoints
  plus space, tab, newline, CRLF, NBSP, and U+2028: **12,046 cases**.
- Every `at<separator> range KPI!A1` chain denied. Aggregate effects across the sweep
  were exactly `port=0`, `claim=0`, `IdempotencyRow=0`, `provider=0`.
- Explicit underscore controls `at_ range`, `AT___ RaNgE`, mixed EN/HE underscore
  chains, and repeated underscores denied with zero effects.
- `at_foo`, `format`, `chat`, `at4ever`, and `atFoo` before the real `range`
  introducer were not misread as earlier introducers; each positive executed exactly once.

### Opaque allowlisted IDs

- Exact one-occurrence positives passed for `sheet-B2`, `A1`, `B2`,
  `opaque_sheet-B2`, `sheet_A1`, `B2-tail`, `pre-B2-suf`, `MiXeD-b2`, and
  `KPI!A1`.
- Zero occurrence, duplicate before/after, wrong case, a second allowlisted ID,
  a bare same-token secondary target, and a tab-qualified same-token secondary target
  denied with zero effects.
- Same/equal and overlapping selected-target controls passed for ID/range `A1`/`A1`,
  `A1`/`A1:B2`, and `KPI!A1`/`KPI!A1` when only the selected ID was configured.
- Longer alphanumeric ID substrings/prefixes/suffixes remained inert. Quoted `"B2"`
  remained a valid cell literal for selected opaque ID `B2`.
- The only opaque-ID failure was the P2 other-allowlisted-ID/selected-span collision above.

### Residual A1 classes

- All ordinary before/after secondary references denied with zero effects: relative,
  absolute, and mixed cells/ranges; whole rows/columns; bare, bang-qualified,
  single-quoted, spaced-tab, lowercase/mixed-case, and external-book-like forms.
- Boundary punctuation did not reopen ordinary references. Exact selected-target
  repetition and overlapping secondary ranges denied with zero effects.
- In-token zero-width/combining controls produced the 12 P1 mutations above.

### Raw JSON and negation behavior

- Exact raw decoded codepoints, internal spaces, compatibility-looking fullwidth text,
  and quoted EN/HE negation text (including `M*`/`Cf`) were preserved as inert cell data
  and executed exactly once.
- Canonically equivalent but non-exact substitutions, invalid-only JSON, non-string model
  cells, whitespace-only cells, and multiplicity mismatch denied with zero effects.
- Additional malformed/raw-newline/non-string owner-requested cells produced the P1
  subset mutations above.

### Positive and retry behavior

- One English lower/mixed-case introducer and one Hebrew introducer passed.
- Exact lowercase/mixed/spaced tab names with validator-legal uppercase A1 endpoints passed.
- Five positive cases were immediately replayed with the same owner event. First execution
  had `(port=1, claim=1, row=1, provider=1)`; retry had
  `(port=1, claim=1, row=0, provider=0)`. No provider write duplicated.

## Broader review

- Telegram numeric owner authorization remains before webhook claim, media download, STT,
  and OwnerGraph. The exact suite passed MIME normalization/allowlist, non-empty and
  16,000,000-byte bounds, alternate-port validation, duplicate success/failure effects,
  one visible text reply, and no OwnerGraph call on failure. No TTS path was found.
- Gmail approved-send recovery passed callback-then-console and console-then-callback in
  one Python process, then repeated in reverse order. Both pytest invocations returned 0.
- Finalization, hot handoff, due reminders, kill-switch ordering, migration compatibility,
  per-recipient claims, confirmed-rejection release, ambiguous retention, returning-session
  isolation, and one-card fan-out passed in the exact 19-file suite.
- GA4, GSC, LinkedIn profile, and Sheets remain named bounded owner capabilities behind a
  request-derived `Principal`, Python policy, and typed adapters. ClientGraph has only its
  narrower capability set. Full and focused principal/integration tests passed.

## Exact commands and results

### Reviewer Sheets probe

```powershell
$env:MIA_DATABASE_URL='sqlite:///:memory:'
$env:PYTHONIOENCODING='utf-8'
uv --offline --cache-dir .uv-cache run python gates\evidence\_fifteenth_probe.py
```

Final result: exit 1 by design with **16 mismatches**: 12 control-marked secondary-A1
mutations, three invalid/non-string additional-cell subset mutations, and one
other-allowlisted-ID overlap false denial. All successful negative and positive controls
are enumerated above. The temporary probe was removed.

### Gmail same-process recovery

```powershell
uv --offline --cache-dir .uv-cache run python -c "import pytest; ..."
```

The exact tests were
`test_gmail_callback_recovers_deferred_and_failed_send_once` and
`test_approved_gmail_send_deferrals_remain_retryable`. Result: **2 passed**, then
**2 passed** in reverse order in the same process; `orders=0,0`.

### Exact 19-file suite

```powershell
uv --offline --cache-dir .uv-cache run pytest \
  tests/unit/test_vnext_finalization.py \
  tests/unit/test_website_handoff_owner_notify.py \
  tests/unit/test_hot_handoff.py \
  tests/unit/test_due_scan_worker.py \
  tests/unit/test_comm_operating_model.py \
  tests/unit/test_owner_notify.py \
  tests/unit/test_website_client_graph.py \
  tests/unit/test_vnext_graph_functions.py \
  tests/unit/test_migrate.py \
  tests/unit/test_owner_sheets.py \
  tests/unit/test_owner_live_tools.py \
  tests/unit/test_sheets.py \
  tests/unit/test_vnext_principal.py \
  tests/unit/test_vnext_owner_voice.py \
  tests/unit/test_telegram.py \
  tests/unit/test_transcribe.py \
  tests/unit/test_telegram_owner_outbound.py \
  tests/unit/test_telegram_owner_graph.py \
  tests/unit/test_telegram_format.py \
  --basetemp .pytest-heavy-fifteenth-combined -p no:cacheprovider -q
```

Result: **326 passed**, exit 0.

### Full and mechanical gates

```powershell
uv --offline --cache-dir .uv-cache run pytest \
  --basetemp .pytest-heavy-fifteenth-full -p no:cacheprovider -q
uv --offline --cache-dir .uv-cache run pytest --collect-only -q -o addopts=
```

The full run reached 100% with exit 0. Current collect-only reported **2,468 tests**, one
more than the historical/expected 2,467 count; all current tests passed. This review records
the observed current count and does not rewrite it to the expected value.

```powershell
uv --offline --cache-dir .uv-cache run ruff check app tests scripts
uv --offline --cache-dir .uv-cache run python scripts\assert_origin_bind.py
uv --offline --cache-dir .uv-cache run python scripts\eval_diff.py
```

Results: `All checks passed!`; `origin-bind: ok`; **273/273** across sales 51,
buyer 43, calendar 20, website_handoff 15, safety 20, objection 20, routing 20,
extract 30, writing 33, and gold 21.

```powershell
uv --offline --cache-dir .uv-cache run ruff check --select C901 app scripts \
  --output-format concise --exit-zero
```

Result: **36 C901 findings**.

```powershell
# AST-rebuild app/scripts function inventory and reconcile the three audit matrices.
```

Result: **164** current function-bearing files; **164** audit rows; **164** unique rows;
zero duplicates/missing/extras; **1,637** definitions; **42,312** physical lines;
**37,604** nonblank lines.

```powershell
git diff --check
```

Result after this evidence write: exit 0; repository-wide LF-to-CRLF warnings only.

## Architecture verdict

The deliberate architecture remains intact: one production OwnerGraph with one bounded
`owner_agent_v4` loop (ADR-031/032), a distinct ClientGraph, shared serializable core,
thin Telegram/website channels, request-derived numeric-owner/client principals, named
capability to policy to typed-adapter flow, and Postgres as system of record. Sheets is an
explicitly allowlisted operational surface, not a state or recovery source. Voice remains
input-only and returns text.

The removed paid/runtime inventory remains absent:
`app/integrations/meta_ads.py`, `app/integrations/linkedin_analytics.py`,
`app/domain/campaigns.py`, `app/domain/pacing.py`, and `app/domain/prelaunch.py` do not
exist. Historical capability identifiers remain non-live specification inventory. No
production swarm/sub-agent, TTS, Meta Ads execution, campaign analysis/pacing/prelaunch,
or LinkedIn member-analytics execution path was found.

Architecture preservation does not make the tree releasable: the owner Sheets binder
still lets control-marked secondary targets and incomplete malformed/non-string value
sets reach mutation.

## Inventory result

The three audit matrices reconcile exactly to the current function-bearing tree: 164 rows
for 164 unique current files, zero missing/extra/duplicate paths. Current metrics are
1,637 definitions, 42,312 physical lines, 37,604 nonblank lines, and 36 strict C901
findings. No unavailable pre-cleanup physical-line baseline is claimed.

## Final verdict

**FAIL — two unresolved P1 findings and one unresolved P2 finding remain. Do not approve
Phase 1.5.4, commit, or deploy this tree.**

No completion item was changed in `gates/leaf-1.5.4f-final-review.md`,
`gates/leaf-1.5.4-function-cleanup.md`, `gates/node-1.5.md`, or `gates/root.md`.
