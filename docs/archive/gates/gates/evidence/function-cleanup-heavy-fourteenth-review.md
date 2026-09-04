# Phase 1.5 function cleanup: fourteenth independent HEAVY review

Timestamp: `2026-08-28T10:17:38.7288211+03:00`

Mode: fresh adversarial review of the frozen dirty worktree; production code and tests
were read-only

Decision: **FAIL**

## Scope and method

I loaded `AGENTS.md`, `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, and
`docs/DECISIONS.md` in that order, followed by `PLAN.md`, the four requested gate files,
the thirteenth-review/repair/verification/synthesis evidence, current diff, current code,
and current tests. I did not inspect `.env`, secret values, `docs/archive`, AWS, or live
providers. Historical pass claims were treated as hypotheses and rerun locally.

The Sheets review used a temporary reviewer-only probe with the real `execute_tool` path,
`FakeSheetsPort`, a real SQLite `LeadStore`, a counted `_owner_sheets_port`, a counted
`claim_operation`, real `IdempotencyRow` counts, and provider-operation counts. The probe
was removed after its results were captured. The first launch inherited an unusable local
database setting and stopped at SQLAlchemy URL parsing before any case ran; the successful
rerun explicitly set only the reviewer process database to `sqlite://`. No existing setting
value was read or printed.

## Findings

### P0

None found.

### P1

1. **U+005F LOW LINE bypasses the repeated Sheets target-introducer guard and reaches
   the provider.**

   `_SHEETS_TARGET_INTRO_RE` and `_SHEETS_TARGET_INTRO_TAIL_RE` at
   `app/tools/registries/owner_tools.py:909-912` use Python `\w` boundaries. Although
   underscore is Unicode connector punctuation (`Pc`) and is in ASCII `string.punctuation`,
   Python treats it as a word character. For the authenticated owner turn
   `Please append "x" to sheet-main at_ range KPI!A1 in the Sheet`, `range` binds the
   model-selected target, while the earlier `at` is hidden because `(?!\w)` fails before
   the underscore. The checks at lines 943-958 then accept the malformed chain.

   The real counted probe returned `ok=True`, constructed the Sheets port once, called
   `claim_operation` once, persisted one `owner_sheets_write` idempotency row, and recorded
   one fake-provider append. The effect path is port construction at line 746, claim at
   line 756, and capability/provider execution at line 761. A malformed repeated target
   introducer must fail before all four effects.

### P2

1. **A valid explicitly allowlisted opaque spreadsheet ID whose final hyphen-delimited
   segment looks like A1 is rejected.**

   After only the selected A1 target is blanked at
   `app/tools/registries/owner_tools.py:948-954`, `_A1_REFERENCE_RE` scans the entire
   remaining turn at line 958. It therefore treats the `B2` suffix of an opaque configured
   ID such as `1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-B2` as a secondary cell reference.
   The shared validator accepts this exact ID because the authorization contract is exact
   allowlist membership (`app/integrations/sheets.py:229-231`), but `execute_tool` returns
   `ok=False`. The direct reproduction produced zero port calls, zero claims/idempotency
   rows, and zero provider operations. This is fail-closed, but it denies a valid configured
   Google-ID shape; the exact bound spreadsheet-ID span must not be confused with a
   secondary A1 target.

### P3

1. `app/graph/owner_agent.py:104` advertises bounded `sheets_update` and
   `sheets_append`, while line 148 still tells the model, "You have read tools only."
   The more specific tool description prevents this wording issue from becoming an
   authority bypass, but the prompt should state the narrow bounded-write exception.

## Adversarial Sheets results

- Repeated-introducer sweep covered every runtime Unicode `M*`, `P*`, `S*`, and `Cf`
  codepoint: **12,040** separator characters. It therefore included all 32 ASCII
  punctuation characters, brackets, straight/curly quotes, slashes/backslashes, currency
  and math symbols, emoji, Unicode punctuation, all mark categories, and bidi/format
  controls. Space, tab, newline, CRLF, NBSP, and U+2028 were also exercised. Exactly one
  separator was accepted: U+005F LOW LINE, the P1 above. Every other denial had zero port,
  claim, idempotency-row, or provider delta.
- Mixed EN/HE chains and arbitrary English casing denied with zero effects across colon,
  exclamation, hyphen, brackets, emoji, marks, and format controls. Word-bearing separation
  (`at topic range ...` and its Hebrew counterpart) remained accepted, confirming the
  guard distinguishes an adjacent punctuation chain from ordinary intervening words.
- Boundary probes denied target-internal ZWJ and combining marks, exact-target repetition,
  prefix/suffix selection, and spreadsheet-ID substring selection with zero effects.
- Residual-target sweep denied **28/28** before/after cases: relative cell/range;
  absolute/mixed cell/range; whole relative/absolute columns and rows; bare,
  bang-qualified, single-quoted, double-quoted, and spaced tab names; lower/mixed case;
  and external-book-looking forms. Unusual punctuation did not reopen them. JSON-quoted
  `B2` and `Other!$B$2` cell strings remained inert and one valid append reached the fake.
- Positive bindings passed for one `at`, arbitrary-case `range`, Hebrew `בטווח`, exact
  lowercase/mixed/spaced tab targets, escaped JSON values, and duplicate-value multisets.
  Trim behavior also passed: owner literal `"x"` plus model cell `" x "` reached the
  provider as normalized `"x"`. A whitespace-only cell remained denied by the existing
  suite.
- Invalid JSON, non-string cells, missing/extra duplicate multiplicity, raw Unicode
  canonical/compatibility substitutions, variation-selector removal, and format-control
  removal all denied with zero effects. Exact raw decoded codepoints passed.
- All **2,671** runtime Unicode `M*`/`Cf` insertions into standalone Hebrew `לא` denied
  with zero effects. Quoted EN/HE negation text remained inert and valid.

## Broader review

- Telegram owner authentication remains numeric-only and occurs before the voice claim or
  media work. The exact combined suite passed auth-before-media, pre-media dedupe, MIME
  normalization/allowlist, real-byte non-empty and 16,000,000-byte bounds, alternate-port
  validation, one visible HTML text failure, one text success reply, and zero OwnerGraph
  calls on failure. No TTS path was found.
- Gmail approved-send recovery passed callback-then-console and console-then-callback in
  one Python process; each ordering passed both tests. The send path remains approval,
  write-flag, demo, kill-switch, binding, claim, provider, and completion/failure ordered.
- Finalization, hot handoff, due reminders, multi-recipient delivery, confirmed-rejection
  release, ambiguous-result retention, returning-session isolation, kill-switch-before-
  mutation, legacy exact-conversation retention, legacy same-day reminder retention, and
  recipient-ledger migration coverage passed in the exact 19-file suite.
- GA4, GSC, LinkedIn profile, and Sheets are bounded typed owner capabilities behind the
  request-derived `Principal`, capability policy, and adapters. ClientGraph rejects owner
  authority.

## Exact commands and results

### Reviewer Sheets probes

```powershell
uv --offline --cache-dir .uv-cache run python gates\evidence\_fourteenth_probe.py
```

Successful SQLite rerun result: 12,040 separator codepoints, exactly one acceptance
(`U+005F LOW LINE`) with deltas `(port=1, claim=1, row=1, provider=1)`; 28/28 residual
secondary references denied; 2,671/2,671 marked/formatted Hebrew negations denied; all
other positive/negative controls behaved as recorded above. The temporary probe was
removed.

```powershell
uv --offline --cache-dir .uv-cache run python -c '<direct trim control>'
```

Result: owner `"x"` plus model `" x "` returned `ok=True`; fake provider recorded
`[["x"]]`.

```powershell
uv --offline --cache-dir .uv-cache run python -c '<direct opaque-id control>'
```

Result: `validate_owner_sheet_request` accepted
`1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-B2`, `KPI!A1`, and `[["x"]]`; the owner tool
returned `ok=False` with zero provider operations.

### Gmail same-process recovery

```powershell
uv --offline --cache-dir .uv-cache run python -c "import pytest; ..."
```

The two exact tests were
`test_gmail_callback_recovers_deferred_and_failed_send_once` and
`test_approved_gmail_send_deferrals_remain_retryable`. Result: **2 passed**, then
**2 passed** in reverse order in the same process; both pytest returns were 0.

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
  --basetemp .pytest-heavy-fourteenth-combined -p no:cacheprovider -q
```

Result: **324 passed**, exit 0.

### Full and mechanical gates

```powershell
uv --offline --cache-dir .uv-cache run pytest \
  --basetemp .pytest-heavy-fourteenth-full -p no:cacheprovider -q
uv --offline --cache-dir .uv-cache run pytest --collect-only -q -o addopts=
```

Result: full progress reached 100%, exit 0; collect-only confirmed **2,466 tests**.

```powershell
uv --offline --cache-dir .uv-cache run ruff check app tests scripts
uv --offline --cache-dir .uv-cache run python scripts\assert_origin_bind.py
uv --offline --cache-dir .uv-cache run python scripts\eval_diff.py
```

Results: `All checks passed!`; `origin-bind: ok`; **273/273** across sales 51,
buyer 43, calendar 20, website_handoff 15, safety 20, objection 20, routing 20,
extract 30, writing 33, and gold 21.

```powershell
$out = uv --offline --cache-dir .uv-cache run ruff check --select C901 app scripts \
  --output-format concise --exit-zero
```

Result: **36 C901 findings**.

```powershell
# Rebuild app/scripts function inventory and reconcile all three audit matrices.
```

Result: **164** current function-bearing files, **164** audit rows, **164** unique
audit rows, zero duplicates/missing/extra paths; **1,635** definition lines,
**42,288** physical lines, and **37,584** nonblank lines.

```powershell
git diff --check
```

Result before and after this evidence write: exit 0; repository-wide LF-to-CRLF warnings only.

## Architecture verdict

The deliberate production architecture remains intact: one OwnerGraph with one bounded
`owner_agent_v4` loop (the same single-agent continuation of ADR-031/032), a distinct
ClientGraph, shared serializable core, thin Telegram/website channels, request-derived
numeric-owner/client principals, named capability to policy to typed-adapter flow, and
Postgres as system of record. Sheets is an explicitly allowlisted operational surface,
not a recovery source. Telegram voice is input-only and returns text.

The removed paid-runtime inventory remains absent:
`app/integrations/meta_ads.py`, `app/integrations/linkedin_analytics.py`,
`app/domain/campaigns.py`, `app/domain/pacing.py`, and `app/domain/prelaunch.py` do not
exist. Historical Meta/campaign capability identifiers remain `SPECIFIED` with empty
ports. No production swarm, sub-agent, TTS, Meta Ads execution, campaign analysis/pacing/
prelaunch, or LinkedIn member-analytics path was found.

The architecture preservation does not make the tree releasable because the owner Sheets
authority binder still lets one punctuation-separated repeated introducer reach mutation.

## Inventory result

The three audit matrices still reconcile exactly to the current function-bearing tree:
164 rows for 164 unique current files, with zero missing, extra, or duplicate paths. The
current metrics are 1,635 definitions, 42,288 physical lines, 37,584 nonblank lines, and
36 strict C901 findings. No unavailable pre-cleanup physical-line baseline is claimed.

## Final verdict

**FAIL — one unresolved P1 and one unresolved P2 remain. Do not approve Phase 1.5.4,
commit, or deploy this tree.**

No completion item was changed in `gates/leaf-1.5.4f-final-review.md`,
`gates/leaf-1.5.4-function-cleanup.md`, `gates/node-1.5.md`, or `gates/root.md`.
