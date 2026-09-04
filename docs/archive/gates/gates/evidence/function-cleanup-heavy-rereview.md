# Phase 1.5 clean-room HEAVY rereview after repair

Date: 2026-08-28

Mode: independent read-only outcome review; only gate evidence was edited

Verdict: **FAIL**

The repair tree passes its mechanical regression gate, but it does not pass the required
outcome gate. One P1 authorization defect remains in owner Sheets writes, and one P2
delivery/idempotency defect remains in website HANDOFF fan-out. Phase 1.5 must not be
approved while either is open.

## Review basis

The reviewer read, in the required order, `AGENTS.md`, `docs/PRODUCT.md`,
`docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, the full `unlazy` skill and its required
references, the first failed HEAVY review, repair verification, synthesis, final-review
leaf, all three function-audit matrices, and the current production/test/diff surface.
No `.env` file or secret value was inspected. No network provider call, AWS mutation,
deployment, application edit, or test edit was made.

The diff review covered correctness, simplification, verification, owner/client trust,
provider boundaries, deployment scripts, voice input with no TTS, GA4/GSC/LinkedIn/Sheets
scope, and the ADR-031/ADR-032 one-production-owner-agent shape. Apart from the findings
below, the reviewed changes preserve the documented two-graph/shared-core architecture,
thin channels, explicit capabilities/policy/adapters, pinned provider operations, no TTS,
no arbitrary migration/deploy command, and one production owner-agent loop.

## Blocking findings

### P1 — Sheets mutation authorization is not exactly bound to the owner's operation, target, or literal multiplicity

`app/tools/registries/owner_tools.py:712-771` calls one shared
`_has_bound_sheets_write_request` for both update and append but does not pass the selected
operation into that guard. `app/tools/registries/owner_tools.py:774-785` accepts any one
write verb. Therefore an owner request for update authorizes a model-selected append (and
vice versa).

`app/tools/registries/owner_tools.py:788-817` applies Unicode normalization and plain
substring containment to the spreadsheet id, A1 range, and quoted cells. It does not
enforce token boundaries or a one-to-one multiset of quoted cells. Consequently:

- `sheet-allowed` is accepted from owner text containing only `sheet-allowed-extra`;
- `KPI!A1` is accepted from owner text containing only `KPI!A10`;
- one occurrence of `"x"` authorizes a two-cell payload `[["x", "x"]]`.

An isolated real-registry/real-store harness, using `FakeSheetsPort` only at the provider
boundary, produced:

```text
{'operation_mismatch': True, 'substring_collision': True, 'duplicate_literal': True,
 'adapter_calls': [('append', 'sheet-allowed', 'KPI!A1', [['value']]),
                   ('append', 'sheet-allowed', 'KPI!A1', [['value']]),
                   ('update', 'sheet-allowed', 'KPI!A1:B1', [['x', 'x']])]}
```

The harness set an explicit SQLite test DSN and blank provider configuration, built a
`ToolContext` with numeric-owner principal and allowlist `sheet-allowed`, then called
`execute_tool` for these three cases:

```text
owner: Please update sheet-allowed range KPI!A1 with "value" in the Sheet
tool:  sheets_append(sheet-allowed, KPI!A1, [["value"]])

owner: Please append "value" to sheet-allowed-extra at KPI!A10 in the Sheet
tool:  sheets_append(sheet-allowed, KPI!A1, [["value"]])

owner: Please update sheet-allowed range KPI!A1:B1 with "x" in the Sheet
tool:  sheets_update(sheet-allowed, KPI!A1:B1, [["x", "x"]])
```

All three returned `ok=True` and reached the adapter. This violates the required
fail-before-claim/fail-before-adapter guarantee for mismatched or hallucinated model
arguments. The allowlist, bounded A1 validation, RAW input mode, capability policy,
kill switch, caps, and idempotency remain present, but they do not repair this missing
owner-intent binding.

Required repair gate:

1. Bind `sheets_update` only to an explicit update instruction and `sheets_append` only
   to an explicit append instruction, including the accepted Hebrew forms.
2. Match the complete spreadsheet id and complete bounded A1 range, not substrings of
   longer ids/ranges.
3. Bind quoted cell literals one-to-one, including multiplicity, or require and parse one
   canonical JSON payload stated by the owner.
4. Prove every rejection occurs before `claim_operation` and before the Sheets port.
5. Retain positive English/Hebrew cases, allowlist, bounded range/shape, RAW, caps,
   kill-switch, capability policy, and exact-event idempotency tests.

### P2 — HANDOFF uses one global lead claim, so recoverable owners can be skipped permanently

`app/services/notifications.py:63-68` classifies missing token, missing owner ids, or empty
text as `confirmed_failure=False` even though no send was attempted. `apply_hot_handoff`
in `app/domain/hot_handoff.py:119-138` first wins one claim keyed by hot-lead kind and
lead, then releases it only for `confirmed_failure=True`. A later request with valid
configuration therefore cannot retry.

The same global claim cannot represent per-owner fan-out state. If owner `111` succeeds
and owner `222` receives a known HTTP 400 rejection, `delivered` is non-empty, so the
claim is retained. A later healthy replay is suppressed for both owners: it correctly
avoids duplicating `111`, but permanently skips `222`.

Real `LeadStore` reproductions with an in-memory SQLite DSN and a local fake HTTP client
produced:

```text
{'missing_then_valid_calls': [], 'first_attempted': True,
 'second_attempted': False, 'second_delivered': ()}

{'first_delivered': ('111',), 'calls_after_first': ['111', '222'],
 'calls_after_retry': ['111', '222'], 'retry_attempted': False,
 'retry_delivered': ()}
```

This fails the required exactly-one-card-per-configured-numeric-owner outcome and its
missing-configuration, partial-rejection, and retry semantics. It also makes the second
HANDOFF result indistinguishable from an ordinary duplicate claim, so the client graph
does not emit newly truthful recovery copy on that replay.

Required repair gate:

1. Treat a no-token/no-owner/no-text result as a known no-attempt and release the claim,
   then prove a later valid replay sends exactly once.
2. Track fan-out idempotency per `(kind, lead, numeric owner)` (or an equivalent durable
   recipient ledger). A known rejection for one recipient must be retryable without
   resending recipients already accepted.
3. Retain the conservative no-retry rule for a recipient whose transport result is truly
   ambiguous, while allowing independently known-failed recipients to recover.
4. Prove all-success fan-out sends one card per owner, full rejection recovers, partial
   known rejection recovers only the missing owner, ambiguous delivery does not duplicate,
   repeated successful HANDOFF does not duplicate, and HANDOFF never also finalizes into
   a second owner card.

## Mandatory outcome checks that passed

- **Telegram Gmail Approve:** current code returns the approved draft id from the bound
  callback result, then executes Gmail only after numeric owner authentication, approval
  binding, demo/kill/write-flag checks, and an operation claim. Focused tests prove
  disabled/demo/kill and provider-`False` leave a later valid replay available; a
  completed send does not duplicate; tampered, rebound, expired, and otherwise invalid
  approvals do not send. This is local/fake-provider proof, not a live Gmail claim.
- **Website finalization:** `send=False` returns before the durable delivery claim.
  Focused tests prove kill-on suppression followed by kill-off delivery exactly once and
  preserve concurrent claim dedupe.
- **No HANDOFF double path:** `app/agents/client/graph.py:188-197` returns from HANDOFF
  before ordinary website finalization, so an otherwise successful HANDOFF does not send
  a hot-handoff card plus a finalization card. The blocking defect is recipient recovery,
  not this branch separation.
- **Unauthorized/empty owner batches:** `process_owner_texts` filters the batch before
  `get_settings` or default adapter construction; `process_owner_item` validates numeric
  membership before owner authority or effects. Tests replace all relevant builders with
  throwing sentinels and pass.
- **Voice and graph shape:** Telegram/website audio is input-only STT with text output;
  no TTS implementation was found. Owner reasoning still enters the single OwnerGraph and
  one bounded `run_owner_agent` loop. Client knowledge retrieval remains principal-bound.
- **Providers and scope:** GA4/GSC/LinkedIn remain read-only owner capabilities with pinned
  schemas and explicit malformed/error classification. Owner Sheets remains allowlisted,
  bounded and RAW, subject to the P1 binding failure above. Direct Meta ingress, human
  WhatsApp handling, and no dual-send rule remain intact.
- **Deployment scripts:** plaintext `--env` mutation and arbitrary migration command
  options are removed; migration is pinned to `mia-migrate`. No deployment was run.

## Independent inventory and matrix reconciliation

The inventory was rebuilt from current function-bearing Python files, not copied from
the synthesis:

```powershell
$files = @(rg -l '^[[:space:]]*(async[[:space:]]+)?def[[:space:]]' app scripts -g '*.py')
```

Current result:

```text
function-bearing files : 164
definition lines       : 1,616
physical lines         : 41,827
non-blank lines        : 37,163
matrix rows            : 164
unique matrix paths    : 164
missing/extra/duplicate: 0 / 0 / 0
dispositions           : KEEP 139 / SIMPLIFY 24 / MERGE 1 / REMOVE 0
strict C901 findings   : 37
partition              : API 23 / domain 73 / infrastructure 68
```

`uv --offline --cache-dir .uv-cache run ruff check app scripts --select C901
--output-format concise` exited 1 with exactly `Found 37 errors.` This is the recorded
complexity measurement, not a lint-pass command.

There is no measured pre-cleanup physical-line baseline. The synthesis explicitly says
so and records only the repeatable current physical/non-blank totals. The historical
failed review's 37,034 value is not reused as a physical baseline. This amended evidence
contract passes.

## Commands and exact results

All Python commands used the repository-local `.uv-cache`; pytest used repository-local
`--basetemp` and `MIA_DATABASE_URL=sqlite:///:memory:`.

Focused outcome suite:

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider --basetemp .pytest-heavy-rereview-focused-20260828 tests/unit/test_owner_live_tools.py tests/unit/test_owner_sheets.py tests/unit/test_sheets.py tests/unit/test_owner_gmail_console.py tests/unit/test_telegram_owner_outbound.py tests/unit/test_vnext_finalization.py tests/unit/test_website_handoff_owner_notify.py tests/unit/test_hot_handoff.py tests/unit/test_vnext_principal.py tests/unit/test_vnext_inbound_client.py tests/unit/test_telegram.py tests/unit/test_due_scan_worker.py
```

Result: **164 passed, 92 warnings in 7.13s**.

Full regression:

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider --basetemp .pytest-heavy-rereview-full-20260828b
```

Result: **2,414 passed, 1,856 warnings in 76.37s**.

Whole-tree lint:

```powershell
uv --offline --cache-dir .uv-cache run ruff check app tests scripts
```

Result: **All checks passed**.

Origin binding:

```powershell
uv --offline --cache-dir .uv-cache run python scripts/assert_origin_bind.py
```

Result: **origin-bind: ok**.

Deterministic evals:

```powershell
uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py
```

Result: **273/273**, with zero failures in all ten families: sales 51, buyer 43,
calendar 20, website_handoff 15, safety 20, objection 20, routing 20, extract 30,
writing 33, and gold 21.

Diff hygiene:

```powershell
git diff --check
```

Result: exit **0**. Git printed only existing LF-to-CRLF working-copy warnings; it found
no whitespace error.

## Decision and non-claims

**FAIL — G4 remains open.** Passing tests and inventory do not supersede the direct
adversarial reproductions. Do not close the parent Phase 1.5 review gates until both
blocking findings are repaired and a new independent HEAVY reviewer reruns this exact
outcome gate.

This review does not claim live Telegram, Gmail, Sheets, GA4, GSC, LinkedIn, AWS,
deployment, migration, PostgreSQL concurrency, real-device voice latency, or production
traffic proof. It does not claim a pre-cleanup physical-line baseline.
