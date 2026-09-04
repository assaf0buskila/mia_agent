# Function Cleanup HEAVY Seventh Review

Date: 2026-08-28

Mode: independent clean-room outcome review of the complete current dirty worktree

Verdict: **FAIL**

The current tree clears the recorded mechanical gates and the sixth-review Sheets cases,
but it is not releasable. One new P1 authorization defect and two P2 boundary/evidence
defects reproduce on the current tree. The decision rule requires zero unresolved
P0/P1/P2, so none of the PASS-only gate files were changed.

## Findings

### P1 — an explicitly negated Sheets mutation is authorized and executed

- Severity: **P1**
- File/line: `app/tools/registries/owner_tools.py:821-845`, especially line 838
- Boundary: OwnerGraph live Sheets mutation authorization

`_sheets_operation_mentions` removes an affirmative mutation only when the words between
the negator and verb are `to` or `ever`:

```text
(?:do not|don't|never|not)(?: (?:to|ever)){0,2} <verb>
```

Consequently, the ordinary explicit prohibition `do not even <verb>` is treated as an
affirmative command. I reproduced this through the real owner-tool registry and a real
`LeadStore` backed by local SQLite, with a counting fake Sheets provider. This is not a
regex-only unit probe.

Reproduction inputs and result:

```text
Please do not even append "x" to sheet-allowed at KPI!A1 in the Sheet
-> ok=True; provider calls=1; claim calls=1; idempotency rows=1;
   provider operation=append(sheet-allowed, KPI!A1, [["x"]])

Please do not even update "x" to sheet-allowed at KPI!A1 in the Sheet
Please do not even fill "x" to sheet-allowed at KPI!A1 in the Sheet
Please do not even enter "x" to sheet-allowed at KPI!A1 in the Sheet
-> all three also ok=True
-> cumulative provider calls=4; claim calls=4; idempotency rows=4
-> provider operations=append, update, update, update
```

An owner instruction that expressly forbids a write therefore constructs and invokes the
port, claims the operation, persists idempotency state, and reaches provider mutation.
This violates the authorization and no-unintended-write contract even though the six
specified modifier families now pass.

Required closure: make explicit negation fail closed across ordinary intervening adverbs,
add adversarial regression coverage for all supported append/update/fill/enter verbs, and
prove zero port construction/call, claim, idempotency write, and provider operation.

### P2 — Telegram forwards non-audio response bodies to STT

- Severity: **P2**
- File/line: `app/integrations/telegram.py:161-203`; `app/api/telegram.py:45-66`
- Boundary: authenticated owner voice media validation

`TelegramPort.download_voice` enforces the HTTPS Telegram host/path and a 16,000,000-byte
limit, but lines 199-203 accept any response `Content-Type`. `_transcribe_telegram_voice`
then forwards that type and body to STT without an audio MIME allowlist.

Adversarial `httpx.MockTransport` reproduction:

```text
Telegram getFile -> valid allowlisted file path
Telegram file GET -> 200, Content-Type: text/html, body: <html>not audio</html>
download_voice -> {bytes: 22, mime: "text/html"}

_transcribe_telegram_voice with the same media result
-> STT called once with mime_type="text/html"
-> item text becomes "junk accepted"; failed=False
```

Authentication still precedes media download and the owner response remains text-only,
but content-type failure is not fail-closed. This exposes the STT adapter to arbitrary
non-audio content and avoidable provider cost/failure.

Required closure: allowlist the supported audio types before STT invocation (with a safe
policy for absent/parameterized types), reject non-audio types as media failure, and add
provider-call-negative tests alongside the existing size/HTTP failure cases.

### P2 — Gmail recovery regression evidence is order-dependent

- Severity: **P2 (verification integrity)**
- File/line: `tests/unit/test_telegram_owner_outbound.py:257`;
  `tests/unit/test_owner_gmail_console.py:254`; `app/integrations/gmail.py:254-262`;
  `app/db/store.py:1582-1645`
- Boundary: Gmail approval recovery and retry proof

The two relevant tests share the process-lifetime in-memory database but only initialize
it; they do not isolate/reset it. Each new `FakeGmailPort` starts again at `draft_1`
(`app/integrations/gmail.py:257`). A previously completed approval for that resource is
not replaced because `upsert_gmail_approval` updates only an existing pending row
(`app/db/store.py:1635`). The later test then finds no pending approval.

Reproduction:

```powershell
uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
  tests/unit/test_telegram_owner_outbound.py::test_gmail_callback_recovers_deferred_and_failed_send_once `
  tests/unit/test_owner_gmail_console.py::test_approved_gmail_send_deferrals_remain_retryable
```

Result: **1 passed, 1 failed**. The second test fails at its pending-approval lookup with
`IndexError`. Running the second test alone passes. The exact recorded 19-file order also
passes because it happens to run the Gmail-console test before the outbound recovery test.

This does not establish a production collision with provider-issued draft IDs. It does
establish that a required recovery/retry proof is not test-isolated and can be masked by
suite ordering, so the green combined/full runs are weaker than claimed for this boundary.

Required closure: isolate database state or guarantee unique fake resource IDs, then prove
both tests pass alone and in both pair orders.

## Adversarial probes and closed historical findings

### Sheets sixth failures and required modifier variants

I exercised the real owner registry plus real local `LeadStore` for each of append,
update, fill, and enter with all five requested negation forms:

```text
not <verb>
not to <verb>
not ever <verb>
do not ever <verb>
never ever <verb>
```

All **20/20** returned a denial with provider calls=0, claims=0, idempotency rows=0, and
provider operations=0. A positive append whose quoted cell value was `"not ever append"`
succeeded, demonstrating that quoted negation remains inert cell data. These specific
sixth-review failures are closed; the P1 above is a distinct modifier bypass.

### Sheets semantic target binding

Direct adversarial outcomes on the current tree:

- exact Hebrew spaced-tab target using `את` and `בטווח`: allowed;
- exact spaced target/replay contract: maintained regression passes;
- suffix selection (`Foo Bar!A1` tool target versus `Bar!A1` request): denied with zero
  port/claim/idempotency/provider effects;
- two spaced targets, repeated same target, different target, quoted target, spreadsheet-ID
  token collision, range token collision, and a range present only inside quoted/data text:
  all denied with zero effects;
- quoted negation as a cell value: inert and the otherwise exact positive write succeeds.

The 99-test Sheets/principal suite also passes its EN/HE introducer, prior-operation
conflict, allowlist, principal/source/policy/kill-switch, RAW input, empty-value,
idempotency, and replay boundaries. This does not close the independently reproduced
`do not even` authorization bypass.

### Telegram owner voice

Source tracing and focused/full tests confirm numeric-owner authorization occurs before
media acquisition, STT text enters the same `process_owner_texts`/OwnerGraph path as typed
text, and outbound replies are Telegram text/HTML only; no TTS implementation is present.
HTTP/provider failure and the 16 MB size guard are present. The missing non-audio content
type rejection remains the P2 finding above.

### Provider capability boundaries

The focused GA4, Search Console, LinkedIn, capability, and owner-live-tool suite passes
**62 tests**. Current routing remains typed capability -> policy -> adapter: GA4/GSC return
normalized KPI data and LinkedIn is profile-only. No second live implementation was found.
No live provider was called in this review.

### Notification, finalization, recovery, and handoff

The exact combined suite and full suite pass the recipient-claim, legacy migration,
empty-returning-session, retry, Gmail recovery, and hot-handoff kill-order assertions in
their recorded order. Source tracing keeps the ClientGraph handoff return ahead of normal
finalization and keeps the kill switch ahead of handoff effects. The Gmail pair-order
failure above means the recovery evidence still needs isolation before release.

## Mechanical commands and results

All commands were run against the current dirty tree without reset, clean, checkout,
deployment, AWS access, or live-provider access.

1. Focused/adversarial Sheets and principal suite:

   ```powershell
   uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
     tests/unit/test_owner_sheets.py tests/unit/test_owner_live_tools.py `
     tests/unit/test_sheets.py tests/unit/test_vnext_principal.py -q
   ```

   Result: **99 passed**.

2. Exact recorded 19-file combined suite, in this order:

   ```text
   test_vnext_finalization.py
   test_website_handoff_owner_notify.py
   test_hot_handoff.py
   test_due_scan_worker.py
   test_comm_operating_model.py
   test_owner_notify.py
   test_website_client_graph.py
   test_vnext_graph_functions.py
   test_migrate.py
   test_owner_sheets.py
   test_owner_live_tools.py
   test_sheets.py
   test_vnext_principal.py
   test_vnext_owner_voice.py
   test_telegram.py
   test_transcribe.py
   test_telegram_owner_outbound.py
   test_telegram_owner_graph.py
   test_telegram_format.py
   ```

   Result: **290 passed**.

3. Full suite:

   ```powershell
   uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
     --basetemp .pytest-heavy-sixth-full-review
   ```

   Result: **2,432 passed, 1,856 warnings in 98.50s**.

4. Whole-tree lint:

   ```powershell
   uv --offline --cache-dir .uv-cache run ruff check app tests scripts
   ```

   Result: **All checks passed**.

5. Origin binding:

   ```powershell
   uv --offline --cache-dir .uv-cache run python scripts/check_origin_bind.py
   ```

   Result: **origin-bind: ok**.

6. Deterministic eval gate:

   ```powershell
   uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py
   ```

   Result: **273/273 passed** across all ten families: sales 51, buyer 43,
   calendar 20, website_handoff 15, safety 20, objection 20, routing 20,
   extract 30, writing 33, and gold 21.

7. Strict complexity measurement:

   ```powershell
   uv --offline --cache-dir .uv-cache run ruff check app scripts `
     --select C901 --output-format concise
   ```

   Result: expected measurement exit 1; **37 C901 findings**.

8. Diff hygiene:

   ```powershell
   git diff --check
   ```

   Result before this evidence edit: exit 0; only existing LF-to-CRLF warnings.

9. Provider-focused suite:

   ```powershell
   uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider `
     tests/unit/test_ga4.py tests/unit/test_search_console.py `
     tests/unit/test_linkedin.py tests/unit/test_owner_composio_capabilities.py `
     tests/unit/test_owner_live_tools.py -q
   ```

   Result: **62 passed**.

Mechanical green is necessary but does not override reproducible P1/P2 outcome failures.

## Independent inventory reconciliation

I independently enumerated Python files under `app`, `scripts`, and `tests`, counted AST
function/async-function definition lines, counted physical/non-blank lines, parsed every
audit table row, and compared normalized paths to the current function-bearing set.

```text
function-bearing files                  164
definition lines                        1,631
physical lines                          42,198
non-blank lines                         37,503
strict app+scripts C901 findings        37
matrix rows / unique paths              164 / 164
missing / extra / duplicate paths       0 / 0 / 0
partition                               23 API / 73 domain / 68 infra
disposition                             139 KEEP / 24 SIMPLIFY / 1 MERGE / 0 REMOVE
```

The three audit matrices therefore disposition exactly the current 164-file inventory.
The independently measured hypotheses 164 / 1,631 / 42,198 / 37,503 / 37 are confirmed.

## Minimality and architecture reconciliation

- One production OwnerGraph agent remains; no production runtime swarm, subagent router,
  rewrite loop, or multi-agent orchestration was found.
- OwnerGraph and ClientGraph remain separate thin-channel graphs over shared core policy
  and adapters.
- Removed runtime surfaces named by the accepted cleanup ADR are absent from executable
  code; retained capability metadata is intentional catalog/policy surface, not a revived
  runtime implementation.
- Sheets health/registry surfaces advertise implemented read/update/append behavior, and
  `ComposioWhatsAppPort` remains present; no duplicate live provider path was found.
- No accepted ADR reversal was identified. The current defects are boundary correctness
  failures, not justification for architecture expansion.

## Explicit non-claims

- No `.env`, secret value, AWS resource, production database, or live provider was read or
  called. No deployment or external mutation occurred.
- This review does not prove Postgres concurrency, production migration application,
  Telegram's real upstream content-type behavior, live STT behavior, or live provider
  schemas/permissions.
- SQLite/fake-adapter probes prove local authorization and state transitions, not external
  provider success.
- A passing full suite does not prove order independence; the Gmail reverse-order pair
  explicitly contradicts that stronger claim.
- No pre-cleanup physical-line baseline is claimed.
- This FAIL does not modify `gates/leaf-1.5.4f-final-review.md`,
  `gates/leaf-1.5.4-function-cleanup.md`, `gates/node-1.5.md`, or `gates/root.md`.
- The only file created by this review is this evidence file. No app code, test, migration,
  canonical doc, plan, prior evidence, or gate decision was edited.

## Release decision

**FAIL.** Phase 1.5 cannot pass while explicit owner negation can authorize a Sheets write,
Telegram can forward non-audio bodies to STT, and the Gmail recovery proof depends on test
order. A fresh HEAVY reviewer must reproduce repairs and rerun the complete outcome gate.
