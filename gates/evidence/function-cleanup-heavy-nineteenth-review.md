# Function cleanup HEAVY nineteenth review

- Date: 2026-08-30
- Reviewer role: fresh independent HEAVY verifier after review 18 repair; this reviewer did not implement the tree.
- Verdict: **FAIL**
- Release decision: Phase 1.5 cannot close because one independently reproduced P1 Sheets complete-cell authority class remains. No P0 or separate P2 finding was found.

## Scope and sources

The complete current dirty tree was reviewed read-only except for a disposable reviewer probe
and this evidence file. The probe and all nineteenth-review pytest temp directories were removed
after their output was captured. Existing dirty work was preserved. Production code and tests
were not edited.

Mandatory sources were read fully and in order:

1. `AGENTS.md`
2. `docs/PRODUCT.md`
3. `docs/ARCHITECTURE.md`
4. `docs/DECISIONS.md`
5. `PLAN.md`
6. `gates/node-1.5.md`
7. `gates/leaf-1.5.4-function-cleanup.md`
8. `gates/leaf-1.5.4f-final-review.md`
9. `gates/evidence/function-cleanup-synthesis.md`
10. `gates/evidence/function-cleanup-repair-verification.md`
11. `gates/evidence/function-cleanup-heavy-eighteenth-review.md`
12. `gates/evidence/function-cleanup-eighteenth-repair.md`
13. Current code, tests, diff, and all three audit matrices:
    `function-audit-api.md`, `function-audit-domain.md`, and
    `function-audit-infra.md`.

`docs/archive/` and `.env` were not opened. No secret value was inspected or emitted.

## Findings

### P0

None found.

### P1-1: arbitrary unquoted bare cells are silently discarded before a quoted-subset Sheets write

- Location: `app/tools/registries/owner_tools.py:1044-1089`, especially
  `_UNQUOTED_JSON_CANDIDATE_START_RE` at line 1044 and `_has_unquoted_json_cell` at
  lines 1055-1089. The real mutation boundary is `_sheets_write` at lines 714-778.
- Cause: the residual complete-cell scanner recognizes numeric/boolean/null scalars,
  JSON-container delimiters, single quotes, and a finite pseudo-literal list. It does not
  recognize an arbitrary unquoted bare word, a backtick string, or punctuation-only token in
  the same explicit write/list grammar position. `_quoted_literals` therefore sees only the
  quoted subset, which can still equal the model payload and authorize the write.
- Boundary: real `execute_tool`, numeric owner
  `Principal.owner(source="telegram", actor_id="123456789")`, real `LeadStore`, counted
  `_owner_sheets_port`, counted `claim_operation`, real `IdempotencyRow`, and
  `FakeSheetsPort` mutation.
- Minimal reproductions:
  - `Append "x" and blue to sheet-main at KPI!A1 in the Sheet`
  - `Update foo-bar with "x" to sheet-main at KPI!A1 in the Sheet`
  - `הוסף "x" וundefined לגיליון sheet-main בטווח KPI!A1`
  - `מלא blue ו־"x" לגיליון sheet-main בטווח KPI!A1`
- Effect: each request submitted model payload `[["x"]]`, returned a successful one-row
  append/update, and changed every measured boundary by exactly one:
  `(port construction, operation claim, idempotency row, fake-provider mutation) =
  (1, 1, 1, 1)`.
- Breadth: the independent probe ran **2,468** denial candidates. Exactly **1,000** were
  effectful failures: 100 variants each for `blue`, `foo-bar`, `undefined`, `yes`,
  `TRUEISH`, `` `x` ``, `???`, `!!!`, `...`, and an em-dash token. Each token was placed
  before and after the quoted cell across all five English operation spellings with
  `and`/`or`/`plus`/`with`/comma/semicolon and all four Hebrew operation spellings with
  plain-vav/hyphen-vav/maqaf-vav/comma/semicolon.
- Impact: the executed grid is not the complete value request authenticated by the owner.
  This violates the strict every-cell-JSON-quoted contract and ADR-042's narrow explicit-write
  authority boundary. The model may select and execute a smaller write than the owner stated,
  so this is release-blocking P1.

### P2

None found beyond the P1 authority defect above.

### P3

None recorded.

## Adversarial Sheets matrix

The priority probe deliberately stopped broad optional expansion after P1-1 reproduced, as the
review contract requested. Its bounded matrix still distinguished the defect from the repaired
and valid classes:

- **1,300** numeric/boolean/null/pseudo-literal variants, including `NaN`, signed `NaN`,
  signed `Infinity`, signed `None`, other-case booleans/null, and single-quoted strings,
  denied before every effect.
- **168** representative malformed opening, closing, mixed, and nested container sequences
  across bounded punctuation/control-looking separator forms denied before every effect.
- Six positive prose/quoted controls (`Please`, target-first English, Hebrew object marker
  `את`, target-first Hebrew, quoted `"blue"`, and a normal Sheet phrase) succeeded with exact
  `(1,1,1,1)` effects. Quoted bare-word lookalikes remained exact cell data.
- The final aggregate was `port=1006`, `claim=1006`, `rows=1006`, `provider=1006`: the
  1,000 unauthorized mutations plus six intended positive controls. Every denied candidate
  had an exact zero delta.

The exact 19-file suite then re-exercised the complete existing target/ID/quote/Unicode/
negation/principal/kill/idempotency matrix and passed all **330** collected tests. In particular,
its owner-live grid covers exact row-major single-cell, row, column, `2x2`, and duplicate layouts;
swaps, transpose, duplicate reorder, ragged, partial, reshaped, reversed, and oversized payloads;
secondary targets/IDs; raw quoted-codepoint preservation; negation; and zero-effect denials.

Static comparison found the owner-side `_BOUNDED_A1_RANGE_RE` and downstream
`validate_owner_sheet_request` use the same optional sheet-prefix and ordered bounded-cell
syntax. The binder additionally requires exact target dimensions, while the downstream parser
enforces the consistent 20-row by 10-column maximum. No target/grid grammar drift was found.

## Telegram, Gmail, notification, and finalization assessment

No additional P0-P2 finding was found in the required bounded rereview:

- Telegram still authenticates the webhook and numeric owner before media work; voice uses the
  canonical preclaim, supported audio MIME/byte bounds, shared OwnerGraph, and text-only output.
- Gmail approval binding, write flag, kill switch, provider-failure recovery, and completed-send
  dedupe remain on the deterministic path.
- Hot handoff, ordinary finalization, and due reminders retain policy-first/per-recipient claim
  ordering and duplicate-safe accepted/ambiguous outcomes.
- The exact 19-file suite covering these paths passed 330/330, and the complete tree passed
  2,472/2,472.

## Architecture and 164-file assessment

- Runtime remains one owner-agent loop with two graph entry points: OwnerGraph for Telegram and
  ClientGraph for website/prospect traffic. ADR-031/032's no-sub-agent/no-second-planner shape
  remains intact.
- Privileged execution remains capability -> policy -> typed adapter. Numeric owner identity
  mints `Principal.owner`; website, inbound prospect, and due-scan paths mint
  `Principal.client`.
- Postgres remains the system of record. Sheets remains ADR-042's narrow operational surface;
  P1-1 is a defect inside its request binding, not an alternate source of truth.
- Voice remains input-only with no TTS. Gmail/notifications/handoff/finalization runtime paths
  remain present.
- Removed runtime files remain absent: `app/integrations/meta_ads.py`,
  `app/integrations/linkedin_analytics.py`, `app/domain/campaigns.py`,
  `app/domain/pacing.py`, and `app/domain/prelaunch.py`. No live import of them was found;
  only the truthful removed capability enum remains.
- `.gitignore` entries `.pytest-*/` and `.heavy-review-*/` match reviewer artifacts. Fresh
  `git check-ignore` probes confirmed production source and gate evidence are not ignored.

Independent inventory reconciliation:

```text
function_files=164 audit_rows=164 unique_audit_rows=164
definitions=1640 physical=42429 nonblank=37710
duplicates=[]
missing=[]
extra=[]
```

Whole-tree strict C901 reported **36** findings, matching the expected inventory. The three
audit matrices therefore account for all current 164 function-bearing production/script files
exactly once, but inventory correctness does not override P1-1.

## Exact mechanical commands and results

### Reviewer adversarial probe

```powershell
uv --offline --cache-dir .uv-cache run pytest .heavy-review-nineteenth/test_nineteenth_probe.py --basetemp .pytest-heavy-nineteenth-probe3 -p no:cacheprovider -q -s
```

Result: expected failure from P1-1; `cases=2468 unexpected=1000`,
`effects=(1006,1006,1006,1006)`. Each of the ten failing token classes produced 100
effectful variants. The disposable probe and temp directories were removed afterward.

### Exact 19-file cleanup suite

The exact ordered 19-file command from
`function-cleanup-repair-verification.md` was run with workspace-local basetemp and
`-p no:cacheprovider`.

Result: exit 0; progress reached 100%; independent collection reported
**330 tests collected**.

### Full pytest

```powershell
uv --offline --cache-dir .uv-cache run pytest --basetemp .pytest-heavy-nineteenth-full -p no:cacheprovider -q --disable-warnings
```

Result: exit 0; progress reached 100%. Independent collection reported
**2,472 tests collected**.

### Ruff and strict complexity inventory

```powershell
uv --offline --cache-dir .uv-cache run ruff check app tests scripts
```

Result: exit 0; `All checks passed!`

```powershell
uv --offline --cache-dir .uv-cache run ruff check --select C901 app scripts --output-format concise --exit-zero
```

Result: `Found 36 errors.` This is the expected complexity inventory, not a clean-complexity
claim.

### Origin binding and deterministic evals

```powershell
uv --offline --cache-dir .uv-cache run python scripts/assert_origin_bind.py
uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py
```

Results: `origin-bind: ok`; **273/273** across sales 51, buyer 43, calendar 20,
website handoff 15, safety 20, objection 20, routing 20, extract 30, writing 33, and gold 21.

### Inventory and diff hygiene

The disposable PowerShell inventory reconciliation reported the exact 164/164 metrics above.

```powershell
git diff --check
```

Result: exit 0; only working-tree LF-to-CRLF conversion warnings were emitted.

## Gate changes and non-claims

- Gate changes: **none**. Because the verdict is FAIL, no independent-review/completion item was
  checked in `gates/leaf-1.5.4f-final-review.md`,
  `gates/leaf-1.5.4-function-cleanup.md`, `gates/node-1.5.md`, or `gates/root.md`.
- No live G2/G3, AWS, deployment, provider, or production gate was marked or claimed.
- No AWS API, external provider, deployment, commit, push, or live mutation was performed.
  Provider effects in this review used a local fake only.
- Passing tests, lint, evals, origin binding, and inventory reconciliation do not compensate for
  the independently reproduced P1 authority defect. This review remains **FAIL**.
