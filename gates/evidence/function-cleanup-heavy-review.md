# Phase 1.5.4f clean-room HEAVY review

Date: 2026-08-28
Reviewer mode: outcome review after implementation; application, tests, scripts, product
documents, git state, and AWS state remained read-only. No `.env`, secret value, live
provider, deployment, or AWS mutation was inspected or used.

## Verdict

**FAIL.** The mechanical gates are green and the 164-file audit inventory is exact, but
there is one unresolved P1 and three unresolved P2 correctness/safety defects. G4 cannot
pass and Phase 1.5.4 must not be approved until the repair gate at the end is green.

## Findings (highest severity first)

### P1 — Sheets writes are not bound to the owner's requested document, range, or values

- Code: `app/tools/registries/owner_tools.py:711-756` accepts the model-produced `args`
  after `_has_explicit_sheets_write_request`; `app/tools/registries/owner_tools.py:767-778`
  proves that guard checks only a generic mutation verb plus a Sheets noun. It never
  proves that `spreadsheet_id`, A1 `range`, operation, or exact literal `values` came from
  the authenticated owner's current message.
- Contract contradiction: the same registry promises at
  `app/tools/registries/owner_tools.py:1232-1249` that the owner requested the exact
  literal values and that the model will never choose a spreadsheet. The tests instead
  bless vague requests with model-supplied arguments at
  `tests/unit/test_owner_live_tools.py:244-263` and even authorize Hebrew requests that
  contain no id, range, or value at `tests/unit/test_owner_live_tools.py:277-304`.
- Impact: an owner saying only “Please update the Sheet” lets the model choose any
  allowlisted sheet, bounded range, and literal value. The allowlist, A1 bounds, RAW
  write mode, kill switch, and idempotency reduce blast radius but do not establish
  owner authorization for the actual mutation. This violates ADR-042's wrong-document
  boundary and the tool's own exact-value contract.
- Independent reproduction (real registry, `FakeSheetsPort`, SQLite):

  ```text
  owner_text = "Please update the Sheet"
  args = {"spreadsheet_id": "sheet-b", "range": "Payroll!C7",
          "values": [["MODEL_CHOSEN_VALUE"]]}
  allowed ids = sheet-a,sheet-b

  ToolResult(ok=True, text='1 Sheet row(s) updated.', error='')
  [('update', 'sheet-b', 'Payroll!C7', [['MODEL_CHOSEN_VALUE']])]
  ```

### P2 — A Gmail one-tap Approve consumes the approval but cannot complete the send

- Code: the Gmail callback branch only calls `decide_gmail_approval` at
  `app/domain/owner_callbacks.py:81-91`; `resolve_owner_callback` then reports approval
  at `app/domain/owner_callbacks.py:95-114`. Telegram's callback edge only resolves and
  edits the message at `app/api/telegram.py:99-140`; it has no Gmail send port/settings
  completion step.
- The text path sends only when `apply_gmail_send_decision` returns exactly approved
  (`app/api/owner.py:400-412`), while an already-approved callback row returns
  `already_decided` at `app/domain/gmail_drafts.py:205-244`. The later text path therefore
  cannot recover the send.
- Impact: the advertised one-tap completion path (ADR-026/030) is a dead end for Gmail.
  This does not cause an unintended send; it silently prevents an authorized, flag-gated
  send and presents the button decision as complete.
- Independent reproduction (valid hash/expiry, pending Gmail draft, SQLite):

  ```text
  resolve_owner_callback(... decision='approve', token=<approval id>)
  -> <b>✅ אושר</b> ...
  apply_gmail_send_decision(... text='send the draft <approval id>')
  -> ('already_decided', 'draft_callback_deadend')
  stored decision -> approved
  ```

### P2 — Kill-switch-suppressed finalization permanently consumes the delivery claim

- Code: `finalize_website_conversation` inserts the durable claim first at
  `app/services/finalization.py:117-127`, then returns `claimed=True, sent=False` without
  release when `send=False` at `app/services/finalization.py:128-129`.
  `qualify_and_finalize` supplies `send=not settings.kill_switch` at
  `app/services/finalization.py:198-224`.
- Impact: a session end, inactivity event, or handoff while the kill switch is on is
  permanently recorded as notified. Turning the switch off and retrying returns a
  duplicate, so the owner summary can never be delivered. This is the exact
  kill-switch-suppression failure the API audit required the cleanup to repair.
- Independent reproduction with the same summary:

  ```text
  first, send=False -> FinalizeResult(claimed=True, sent=False,
                                      duplicate=False, kind='web_final_v1')
  retry, send=True  -> FinalizeResult(claimed=False, sent=False,
                                      duplicate=True, kind='web_final_v1')
  ```

### P2 — One website HANDOFF sends two distinct owner Telegram notifications

- Code: ClientGraph sends the hot-handoff brief at
  `app/agents/client/graph.py:151-178`, then the same `next_action == "handoff"` continues
  into `qualify_and_finalize` at `app/agents/client/graph.py:188-210`. The paths use
  different notification kinds, so their individual claims do not deduplicate each
  other.
- The regression at `tests/unit/test_website_handoff_owner_notify.py:64-91` checks only
  that at least one send exists and inspects the first payload; it never asserts exactly
  one send.
- Impact: every successfully delivered website handoff pings every configured owner
  twice with two differently formatted summaries. This contradicts the one concise
  owner-summary product behavior and the claimed centralized delivery path.
- Independent graph reproduction with one owner and one HANDOFF:

  ```text
  next_action='handoff'
  Telegram send count = 2
  first prefix  = '<b>ליד מהאתר — צריך אותך</b>'
  second prefix = 'New website conversation'
  ```

### P3 — The direct owner batch helper constructs owner adapters before local authorization

`process_owner_texts` constructs all default owner provider ports at
`app/api/owner.py:893-917` and only checks each item's numeric owner membership at
`app/api/owner.py:918-923`. Current production Telegram and legacy inbound callers
pre-authorize, so no production escalation was reproduced. Nevertheless, the leaf's
claim that the helper rejects before provider/tool/model work is too broad. Prefilter
authorized items before constructing default ports, and add a regression whose builders
raise if an unauthorized/empty batch reaches them.

### P3 — The synthesis omits the exact before/after physical-line metric required by G2

`gates/evidence/function-cleanup-synthesis.md:49-61` records files, definition lines,
C901, and surface counts, but not total physical lines. The current independently
remeasured total is 37,034 lines across the 164 function-bearing files; the pre-cleanup
physical-line total is no longer reconstructable from the retained evidence. Therefore
the literal “exact before/after ... lines” clause in final-review G2 is not proven.

## Independent coverage and disposition reconciliation

The inventory was rebuilt from the current filesystem, not copied from the three audit
documents. Then the disposition rows were parsed from the matrices and compared by
normalized path.

```text
Current function-bearing files : 164
Current definition lines       : 1,614
Current physical lines         : 37,034
Matrix rows / unique paths     : 164 / 164
Missing / extra / duplicates   : 0 / 0 / 0
Partition                      : API 23, domain/brain 73, infra/scripts 68
Disposition                    : KEEP 139, SIMPLIFY 24, MERGE 1, REMOVE 0
```

Every disposition row names concrete symbols plus callers/tests and a behavior risk or
benefit. The cleanup synthesis reconciles all 21 ranked audit findings. The resulting
metrics truthfully show targeted cleanup rather than broad shrinkage: 164 -> 164 files,
1,622 -> 1,614 definition lines, and 37 -> 37 strict C901 offenders. The flat file and
complexity totals are not themselves failures. The missing physical-line baseline is the
P3 evidence gap above.

## Architecture, trust, provider, and release-script checks

- One production owner agent remains: `run_owner_agent` is the sole bounded owner tool
  loop. OwnerGraph and ClientGraph remain distinct; there is no runtime swarm or added
  planner/rewriter agent. The accepted inner deterministic sales graph remains the
  documented ClientGraph strangler, not a new authority domain.
- Request-derived `Principal.owner` / `Principal.client` propagation and ClientGraph's
  owner-principal rejection are intact. Approval hash/expiry/action/risk/resource
  validation is centralized and fail-closed. Claim release, authorization, and delivery
  are correct except for the findings above.
- The shared Telegram delivery primitive fans out to all numeric owners with per-owner
  isolation. The duplicate-HANDOFF finding is two valid workflow calls, not a regression
  to first-owner-only delivery.
- Sheets retains explicit allowlisted ids, bounded A1 ranges, RAW writes, cell/row caps,
  no Drive discovery, and database-as-system-of-record behavior. Exact owner-to-argument
  binding is the unresolved P1.
- GA4, Search Console, and LinkedIn stay typed/read-only/profile-scoped; local adapter and
  capability tests pass. Voice remains input-only transcription feeding the same graph;
  no TTS/voice-output surface was found.
- `scripts/deploy_ecs_revision.py` exposes no plaintext environment injection option;
  `scripts/run_ecs_migration.py` exposes no arbitrary migration command and remains pinned
  to `mia-migrate`. Focused script tests and repository inspection agree.

## Commands and results

All commands used workspace-local cache/temp and no live provider configuration.

1. Independent full suite:

   ```powershell
   $env:MIA_DATABASE_URL='sqlite:///:memory:'
   uv --offline --cache-dir .uv-cache run pytest -p no:cacheprovider --basetemp .heavy-review-full-20260828
   ```

   Result: **2,405 passed, 1,847 warnings in 60.73s**.

2. Independent risk-focused suite covering principals, owner Sheets/live tools,
   approvals/Gmail/callbacks, calendar writes, finalization/due-scan/handoff/multi-owner,
   visitor knowledge, Telegram/voice, GA4/GSC/LinkedIn, and release scripts:

   Result: **363 passed, 146 warnings in 17.62s**.

3. `uv --offline --cache-dir .uv-cache run ruff check app tests scripts`

   Result: **All checks passed**.

4. `uv --offline --cache-dir .uv-cache run python scripts/assert_origin_bind.py`

   Result: **origin-bind: ok**.

5. `uv --offline --cache-dir .uv-cache run python scripts/eval_diff.py`

   Result: **273/273 passed**: sales 51, buyer 43, calendar 20,
   website_handoff 15, safety 20, objection 20, routing 20, extract 30, writing 33,
   gold 21.

6. `git diff --check`

   Result: exit 0; Windows LF/CRLF warnings only.

These green commands are incorporated, but they do not contradict the four explicit
semantic reproductions: the relevant exact-boundary assertions are missing or permissive.

## Exact repair gate

Phase 1.5.4f may be rerun only after all of the following are true:

1. Bind Sheets operation, spreadsheet id, A1 range, and literal values deterministically
   to the authenticated current owner request (or route ambiguous/missing data through a
   hash-bound pending approval). Reject any missing or mismatching field. Add positive
   exact-binding tests plus negative vague-request, multiple-allowlist, hallucinated-id,
   hallucinated-range, and hallucinated-value tests; preserve RAW mode, caps, kill switch,
   and idempotency.
2. Make the Telegram Gmail Approve button an executable completion path under the same
   binding, `MIA_GMAIL_SEND`, demo, kill-switch, risk, and idempotency boundary, or keep
   the row pending in an explicitly executable state. Test exactly one send when enabled,
   zero when disabled/killed/misbound, replay idempotency, and provider-failure recovery.
3. Do not retain a finalization claim when send is suppressed by the kill switch. Add a
   kill-on then kill-off retry test that proves exactly one later delivery and keeps the
   concurrent duplicate-send test green.
4. Make a HANDOFF choose one owner notification result and reuse its delivery truth for
   finalization/visitor copy. Assert exactly one Telegram send per owner, including
   multiple-owner, retry, rejection, and ambiguous-delivery cases.
5. Move direct-helper authorization before default owner-adapter construction and add the
   no-builder-touch regression.
6. Record an honest physical-line baseline if recoverable; otherwise amend G2 to match the
   retained measurable contract instead of claiming absent evidence.
7. Rerun the focused new regressions, the full suite, Ruff, origin binding, all ten eval
   families, inventory reconciliation, and diff-check; then obtain a fresh HEAVY review
   with no unresolved P0/P1/P2.

## Non-claims

This review does not prove a deployed image, a live Telegram callback/voice interaction,
a real Google Sheet mutation, a Gmail send, or live GA4/GSC/LinkedIn behavior. It made no
network/provider/AWS calls and inspected no secret values. It also does not claim that a
green aggregate suite proves the four missing transition contracts above.
