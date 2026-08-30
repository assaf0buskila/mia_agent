# Function cleanup HEAVY eighteenth review

- Date: 2026-08-30
- Reviewer role: fresh independent HEAVY verifier after review 17 repairs; this reviewer did not implement the tree.
- Verdict: **FAIL**
- Release decision: Phase 1.5 cannot close because one independently reproduced P1 Sheets authority-binding class remains. No P0 or P2 finding was found.

## Scope and sources

The complete current dirty tree was reviewed read-only except for disposable reviewer probes
and this evidence file. Existing dirty work was preserved. Production code and tests were not
edited.

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
11. `gates/evidence/function-cleanup-heavy-seventeenth-review.md`
12. `gates/evidence/function-cleanup-seventeenth-repair.md`
13. Current diff, code, tests, and all three audit matrices:
    `function-audit-api.md`, `function-audit-domain.md`, and
    `function-audit-infra.md`.

`docs/archive/` and `.env` were not opened. No secret value was inspected or emitted.

## Findings

### P0

None found.

### P1-1: unmatched closing container cells are silently discarded before a narrower Sheets write

- Location: `app/tools/registries/owner_tools.py:1044-1087`, especially
  `_UNQUOTED_JSON_CANDIDATE_START_RE` at line 1044 and the candidate loop at lines
  1074-1087. The real effect boundary is `_sheets_write` at lines 716-778.
- Cause: the complete-cell residual scanner recognizes scalar starts and container **openers**
  `[` / `{`, but never recognizes an unmatched closing `]` or `}` in an explicit cell/list
  position. The model can therefore omit that malformed owner-stated extra and submit only the
  quoted subset.
- Boundary: real `execute_tool`, numeric owner `Principal`, real `LeadStore`, counted
  `_owner_sheets_port`, counted `claim_operation`, real `IdempotencyRow`, and
  `FakeSheetsPort` provider mutation.
- Minimal reproductions:
  - `Append "x" and ] to sheet-main at KPI!A1 in the Sheet`
  - `Update "x" with:} to sheet-main at KPI!A1 in the Sheet`
  - `הוסף "x" ו־\n] לגיליון sheet-main בטווח KPI!A1`
  - `מלא } ו"x" לגיליון sheet-main בטווח KPI!A1`
- Broadened forms: `]`, `}`, `])`, `}}`, and `]}` before or after the quoted cell; every
  English write verb (`Append`, `add`, `Update`, `fill`, `enter`); every Hebrew write verb
  (`הוסף`, `הכנס`, `עדכן`, `מלא`); English `and` / `or` / `plus` / `with` and comma/semicolon;
  Hebrew plain-vav, hyphen-vav, maqaf-vav, comma, and semicolon; whitespace, colon, slash,
  backslash, brackets, newline, tab, emoji, punctuation, combining marks, and format controls.
- Effect evidence: the full probe executed **18,877** cases and reported **1,227 failures**.
  Every unexpected case returned success and changed each effect by exactly one:
  `(port construction, operation claim, idempotency row, fake-provider mutation) =
  (1, 1, 1, 1)`. Final totals were `port=1243`, `claim=1243`, `rows=1235`,
  `provider=1235`; eight valid positive controls plus their idempotent replays account for
  the expected remainder.
- Impact: the executed grid is not the complete value request authenticated by the owner.
  This violates ADR-042's narrow explicit-write authority contract and is release-blocking.

### P2

None found beyond the P1 authority defect above.

### P3

None recorded.

## Adversarial Sheets matrix

The independent real-boundary probe covered:

- valid and malformed numeric, boolean, null, signed, exponent, array, object, nested, and
  quoted-member extras before and after quoted cells;
- punctuation and Unicode non-word separator runs across all English/Hebrew mutation verbs and
  list connectors;
- malformed quotes, escapes, raw newline, brackets, and container delimiters;
- exact single-cell, `1xN`, `Nx1`, and `2x2` positives; swapped/repeated values, duplicate
  permutations, transpose, reverse, ragged, partial, reshaped, reversed-endpoint, and oversized
  negatives;
- exact row-major owner literal order and exact rectangular target fill;
- repeated/mixed target introducers, multiple target/ID, A1-like opaque IDs, ID/target
  collision, secondary relative/absolute/whole-column/whole-row references, negation,
  kill switch, and client principal denial;
- every current Unicode `M*` or `Cf` character (**2,671 code points**) inserted into five
  representative secondary A1 forms at the real boundary. All **13,355** such cases denied
  before every effect;
- quoted scalar/container lookalikes remained exact inert string values; the valid single-cell,
  `1x4`, `4x1`, `2x2`, duplicate, bare-target, and spaced-tab positives mutated once and their
  replay did not duplicate the durable row/provider operation.

All unexpected cases belonged to P1-1. Outside that class, unauthorized cases denied with exact
zero effect deltas.

The owner-side A1 dimension grammar was compared directly with
`validate_owner_sheet_request` for bare and sheet-prefixed single/range targets, spaced and
hyphen/underscore tab names, reversed endpoints, lowercase, absolute, whole-row/column,
malformed, and over-bound targets. Result: **14 cases, zero disagreement**. Both sides accept
the same optional sheet-prefix syntax and endpoint order; downstream limits of 20 rows by 10
columns remain an additional consistent restriction before port construction or claiming.

## Telegram, Gmail, notification, and finalization assessment

No additional P0-P2 finding was found:

- Telegram verifies the webhook secret and numeric owner ID before media access. Authorized
  voice updates take the canonical webhook claim before download, STT, OwnerGraph, or reply.
- Telegram media validation enforces supported normalized audio MIME, actual non-empty bytes,
  and the 16,000,000-byte bound at both the default adapter and pre-STT boundary. Voice and
  text reach the same OwnerGraph; output stays text-only.
- The duplicate-success and duplicate-transcription-failure routes, plus Gmail callback and
  console recovery, passed in order A and reverse order B: **4 passed** each run.
- Gmail approval binding, write flag, kill switch, provider-failure release, and completed-send
  dedupe remain ordered before the send boundary.
- Hot handoff applies policy before takeover/inbox/recipient claims/transport. Finalization and
  due reminders retain per-instance/per-recipient claims, release only confirmed rejection,
  and retain accepted or ambiguous outcomes duplicate-safely.

## Architecture and 164-file assessment

- Runtime remains one owner-agent loop with two thin graphs: OwnerGraph for Telegram and
  ClientGraph for website/prospect traffic. ADR-031/032's no-sub-agent/no-second-planner shape
  remains intact.
- The privileged path remains capability -> policy -> typed adapter. Request/channel facts mint
  principals; website/client paths do not acquire owner capabilities.
- Postgres remains the system of record. Sheets remains only the narrow ADR-042 operational
  surface; P1-1 is a failure inside that narrow binding, not an alternate source of truth.
- Removed runtime files remain absent: `app/integrations/meta_ads.py`,
  `app/integrations/linkedin_analytics.py`, `app/domain/campaigns.py`,
  `app/domain/pacing.py`, and `app/domain/prelaunch.py`. No live import of them was found.
- `.gitignore` entries `.pytest-*/` and `.heavy-review-*/` ignore reviewer artifacts only.
  Repository probes matched those shapes and did not match production source or gate evidence.

Independent inventory reconciliation:

```text
function_files=164 audit_rows=164 unique_audit_rows=164
definitions=1640 physical=42426 nonblank=37707
duplicates=[]
missing=[]
extra=[]
```

Whole-tree strict C901 reported **36** findings, matching the expected inventory. The three audit
matrices therefore account for all current 164 function-bearing production/script files exactly
once, but inventory correctness does not override P1-1.

## Exact mechanical commands and results

### Reviewer adversarial probe

```powershell
uv --offline --cache-dir .uv-cache run pytest .heavy-review-eighteenth/test_eighteenth_probe.py --basetemp .pytest-heavy-eighteenth-probe2 -p no:cacheprovider -q -s
```

Result: expected fail from P1-1; `eighteenth_adversarial_cases=18877 failures=1227`,
`effects=(1243, 1243, 1235, 1235)`, `marks=2671`. Separate A1 comparison passed with
`a1_grammar_cases=14 disagreements=[]`.

### Exact 19-file cleanup suite

The exact ordered 19-file command from
`function-cleanup-repair-verification.md` was run with workspace basetemp and
`-p no:cacheprovider`.

Result: exit 0; progress reached 100%; independent collection reported
**330 tests collected**.

### Full pytest

```powershell
uv --offline --cache-dir .uv-cache run pytest --basetemp .pytest-heavy-eighteenth-full -p no:cacheprovider -q --disable-warnings
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

The disposable inventory probe reported the exact 164/164 metrics above.

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
