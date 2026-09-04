# Phase 1.5 function cleanup: twenty-third clean-room HEAVY review

Date: 2026-08-30

## Verdict

**PASS.** The complete current dirty tree has no unresolved P0, P1, or P2 finding.
The persistent review-21 **126-case** and review-22 **42-case** full-`execute_tool`
matrices pass, and a fresh review-23 **168-case** matrix found no authority bypass or
intended-positive denial. Across all three matrices, **246 intended denials** produced
zero port construction, operation-claim, `IdempotencyRow`, or fake-provider effect;
all **90 intended positives** succeeded and replayed without a second persisted row or
provider mutation.

The twenty-second repair establishes a complete whole-turn grammar rather than a suffix
match. Compatibility/mark/control normalization remains confined to the documented
security view; spreadsheet IDs, A1 targets, decoded JSON literals, grid order/shape,
payloads, and provider values remain raw-exact.

## Scope and boundaries

This was an independent verifier pass over the complete current dirty tree after the
twenty-second-review repair. Required sources were read completely in the requested order:

1. `AGENTS.md`
2. `docs/PRODUCT.md`
3. `docs/ARCHITECTURE.md`
4. `docs/DECISIONS.md`
5. `PLAN.md`
6. `gates/root.md`
7. `gates/node-1.5.md`
8. `gates/leaf-1.5.4-function-cleanup.md`
9. `gates/leaf-1.5.4f-final-review.md`
10. `gates/evidence/function-cleanup-synthesis.md`
11. `gates/evidence/function-cleanup-repair-verification.md`
12. `gates/evidence/function-cleanup-heavy-twenty-second-review.md`
13. `gates/evidence/function-cleanup-twenty-second-repair.md`
14. Current code, tests, status, and diff

No `.env`, archive, secret value, network, provider, AWS, commit, push, deployment, or
production action was used. Product code and committed tests were read-only. The fresh
probe is disposable and ignored under `.heavy-review-23/`; pytest temporary paths are
also ignored.

## Findings

### P0

None.

### P1

None.

The pre-effect chain is explicit at `app/tools/registries/owner_tools.py:714-747`:
the bound request and pure value/allowlist validator run before port construction.
The durable operation claim occurs at `app/tools/registries/owner_tools.py:750-757`,
the capability/provider call at `app/tools/registries/owner_tools.py:762-770`, and
completion at `app/tools/registries/owner_tools.py:784`. Every denial in all three
counted matrices remained before all four effects.

### P2

None.

The sole compatibility-obscured operation behavior is intentional rather than a data
normalization bypass. `app/tools/registries/owner_tools.py:843-865` masks JSON strings,
applies NFKD, removes Unicode `M*`/`Cf`, and casefolds only the security view.
`app/tools/registries/owner_tools.py:877-919` separately binds the raw tool arguments,
exact target, exact literals, exact A1 dimensions, and row-major values. Full-width or
compatibility-only spreadsheet IDs, A1 targets, and literals denied with zero effects.

### P3

None. Inventory and strict-C901 measurements match the frozen repair evidence.

## Sheets authorization coverage

### Persistent review-21 matrix

The real owner principal/capability policy, operation ledger, `IdempotencyRow`, and
`FakeSheetsPort` path was rerun:

- **126 total**: **103 denials**, **23 positives**.
- All 63 M*/Cf-hidden earlier-operation cases, the full-width duplicate, nine obscured
  readable sentinels, operation ambiguity, raw readable/private sentinels, and all five
  equal-ID/target extra/reversed controls denied with zero effects.
- All **18** ID-equals-target forms passed: values-first and target-first for five
  English and four Hebrew verbs.
- Every positive replayed safely: two port constructions and claim attempts, but one
  durable row and one provider mutation.

### Persistent review-22 matrix

- **42 total**: **34 denials**, **8 positives**.
- All 24 former mutation-bearing prefix bypasses, target/quoted prefixes, invalid
  suffixes, compatibility-prefix duplicates, full-width ID/target, and compatibility-only
  literal substitution denied with zero effects.
- Bare, `Please`, exact `Please record this now:`, pointed `אלופה`, and four sole
  compatibility-obscured operation controls succeeded and replayed safely.

### Fresh review-23 matrix

The disposable full-path probe executed **168 cases**:

- **109 intended denials**: every result was false and every effect delta was exactly
  `(port=0, claim=0, idempotency_row=0, provider_mutation=0)`.
- **59 intended positives**: every first call and replay succeeded with exact combined
  delta `(port=2, claim=2, idempotency_row=1, provider_mutation=1)`.
- Closed-preface coverage included concatenation/substring boundaries, punctuation,
  alternate colons, newlines, tabs, repeated prefaces, prefaces after the operation,
  mixed English/Hebrew preface plus verb, case, full-width, marks, `Cf`, NFKD/casefold
  forms, injected quoted strings, raw PUA/readable sentinels, and extra prefix/suffix text.
- Clause coverage included all four complete English/Hebrew values-first/target-first
  grammars, extra verbs/clauses/IDs/targets/literals, all 18 equal-ID/target positives,
  reversed/extra occurrences, exact literal order and duplicate multiplicity, rectangular
  grid/A1 dimensions, raw Unicode literals, negation, malformed JSON strings, containers,
  scalars, bare tokens, and target introducer/case/boundary collisions.
- Compatibility/full-width scaffolding that normalizes to the documented grammar was
  accepted. Longer-word collisions and extra compatibility-obscured operations denied.
  Marks/format controls that vanish only in the security view did not normalize any raw
  ID, target, literal, payload, or provider datum.

The first disposable probe invocation failed before collection because tests outside
`tests/` did not inherit the repository's test database setup. The probe was made
self-contained with an in-memory SQLite engine and then passed; this was probe setup,
not a product-path failure.

## Voice, Gmail, notifications, authority, and architecture

- Telegram verifies the webhook secret and kill switch before parsing
  (`app/api/telegram.py:176-194`), checks the numeric owner allowlist before persistence
  (`app/api/telegram.py:195-213`), and claims the canonical audio webhook before media or
  STT (`app/api/telegram.py:215-230`). Media shape, MIME, byte validation, and STT failures
  cannot enter OwnerGraph (`app/api/telegram.py:46-110`). Exact preclaim reuse requires
  the same provider/event/channel/envelope and `received` status
  (`app/api/owner.py:935-954`). Voice and text then enter the same OwnerGraph
  (`app/api/owner.py:719-750`); output remains text only.
- Numeric owner authorization precedes principal minting, claims, and persistence at
  `app/api/owner.py:141-149` and `app/api/owner.py:186-192`. Unauthorized batches return
  before any owner adapter construction at `app/api/owner.py:895-934`.
- Gmail demo/kill/config/binding/risk checks precede the send claim
  (`app/domain/gmail_drafts.py:147-190`); a known provider failure releases the claim and
  a completed send remains idempotent (`app/domain/gmail_drafts.py:191-199`). The two
  recovery tests passed in both execution orders.
- Owner Telegram fan-out classifies accepted, rejected, and ambiguous outcomes per
  recipient (`app/services/notifications.py:61-115`). Finalization releases only confirmed
  rejects and keys claims by conversation plus recipient
  (`app/services/finalization.py:127-202`); due reminders use local day plus recipient
  (`app/workers/due_scan.py:58-117`); hot handoff enforces risk before takeover/inbox/claim
  and likewise releases only confirmed rejects (`app/domain/hot_handoff.py:105-178`).
- The runtime retains one three-node OwnerGraph (`app/agents/owner/graph.py:15-57`) and one
  ClientGraph entry point that refuses a non-client principal
  (`app/agents/client/graph.py:58-78`). The owner loop explicitly remains one agent and one
  model hop (`app/graph/owner_agent.py:1-33`). The inner sales orchestrator remains the
  documented ClientGraph node implementation, not a third trust entry point.
- Sheets read/update/append, GSC, GA4, and LinkedIn profile are owner-only named
  capabilities at `app/capabilities/registry.py:33-54`; ClientGraph receives only client
  capabilities. Repository search found Sheets operational access only through the owner
  tool/capability/typed-port chain. There is no Drive spreadsheet discovery or Sheets
  state/recovery read-back; Postgres remains the system of record. LinkedIn remains
  profile-only.

No additional P0/P1/P2 issue was found in these non-Sheets seams.

## Commands and results

```text
Persistent review-21 + review-22 matrix tests: 2 passed.
Fresh review-23 disposable execute_tool matrix: 1 passed; 168/168 classified as above.
tests/unit/test_owner_live_tools.py: 27 passed.
Exact ordered 19-file suite: 333 passed.
Gmail recovery pair, order A then B: 2 passed + 2 passed.
pytest --collect-only: 2,475 collected, exit 0.
Full pytest: 2,475 passed, 1,955 warnings in 58.13s.
Whole-tree Ruff app/tests/scripts: All checks passed.
Strict Ruff C901 app/scripts: 36 findings.
Origin binding: origin-bind: ok.
Deterministic eval diff: 273/273 across sales 51, buyer 43, calendar 20,
website_handoff 15, safety 20, objection 20, routing 20, extract 30,
writing 33, and gold 21.
Inventory: 164 current function-bearing files; 164 audit rows; 164 unique;
zero missing, extra, or duplicate paths; 1,646 definitions; 42,537 physical
lines; 37,805 nonblank lines.
git diff --check: exit 0; line-ending warnings only.
```

## Gate decision and non-claims

With no unresolved P0/P1/P2, the independent completion items are checked only in
`gates/leaf-1.5.4f-final-review.md`, `gates/leaf-1.5.4-function-cleanup.md`,
`gates/node-1.5.md`, and root G7. Root G2/G3 remain unchecked and root G6 is unchanged.

This evidence makes no AWS, deployment, live Telegram, live Sheets/provider, credential,
production-concurrency, commit, push, or release claim.
