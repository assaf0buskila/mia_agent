# Plan: Mia reliability, live owner capabilities, and simplification

Depth: tree 4   Mode: orchestrated
Budget note: Multi-subsystem repair with production-facing integrations; implementation is split into disjoint leaves and independently verified.

## Contract

- Interfaces: Mia remains two graphs with one owner agent and one client agent. Development sub-agents do not become runtime agents. Channels remain thin and all external reads remain capability -> policy -> typed adapter.
- Safety: no secret values are read or printed; no `.env` inspection; no TTS; no unapproved writes; no automatic deployment before local gates pass. Live probes must be read-only unless a gate explicitly covers Telegram voice receipt/reply.
- Data ownership: leaf owners are disjoint. Shared-file changes are serialized and reassigned in the status log before editing.
- Naming and conventions: existing `MIA_*` settings, typed ports, `Principal`, risk policy, fake/disabled/live adapters, and pytest patterns are preserved.
- Verification: each leaf records evidence in its gate file; the driver reruns checks; a fresh HEAVY reviewer performs the final code review.

## Tree

- 1 Mia reliability and simplification ................................ gates/root.md
  - 1.1 Truth and dead-surface alignment ............................... gates/node-1.1.md
    - 1.1.1 Config, health, docs, and probes ........................... gates/leaf-1.1.1-truth.md
    - 1.1.2 Finish accepted ADR-039 campaign removal ................... gates/leaf-1.1.2-campaign-removal.md
  - 1.2 Owner Telegram capabilities ................................... gates/node-1.2.md
    - 1.2.1 Telegram voice input end to end ............................ gates/leaf-1.2.1-telegram-voice.md
    - 1.2.2 Google and LinkedIn owner capabilities ..................... gates/leaf-1.2.2-owner-integrations.md
  - 1.3 Graph and code simplification ................................. gates/node-1.3.md
    - 1.3.1 Single website knowledge retrieval ........................ gates/leaf-1.3.1-client-knowledge.md
    - 1.3.2 Minimality audit and bounded refactor ...................... gates/leaf-1.3.2-minimality.md
  - 1.4 Production proof and independent review ....................... gates/node-1.4.md
    - 1.4.1 Safe live probes and deployment evidence ................... gates/leaf-1.4.1-live-proof.md
    - 1.4.2 Fresh HEAVY review and final regression gate ............... gates/leaf-1.4.2-review.md

## Status log

- 2026-08-28 plan written; contracts fixed before fan-out.
- 2026-08-28 leaves 1.1.1, 1.2.1, and 1.2.2 dispatched with disjoint file ownership.
- 2026-08-28 leaf 1.2.2 parent-verified: 113 focused tests passed and ledger 3/3 met.
- 2026-08-28 leaf 1.1.2 dispatched after integration leaf released its slot.
- 2026-08-28 leaf 1.1.1 parent-verified: 20 focused tests passed and ledger 4/4 met.
- 2026-08-28 leaf 1.3.1 dispatched after truth-alignment leaf released its slot.
- 2026-08-28 leaf 1.2.1 parent-verified: 90 Telegram/STT regression tests passed and ledger 3/3 met.
- 2026-08-28 branch 1.2 owner Telegram capabilities integrated: both leaves and branch ledger verified.
- 2026-08-28 Assaf expanded the contract: explicitly authorized Sheets require owner read and scoped write; GSC/GA4 must answer AssafWeb KPI questions. Leaf 1.2.2 and branch 1.2 reopened.
- 2026-08-28 leaf 1.3.1 parent-verified: 14 focused graph/knowledge tests passed and ledger 3/3 met.
- 2026-08-28 ADR-042 accepted and canonical docs aligned for authorized Sheets plus AssafWeb KPIs; leaf 1.2.2 G5 verified.
- 2026-08-28 local implementation complete: final 2,375-test suite, Ruff, origin binding, 233 deterministic evals plus routing 20/20, and second independent HEAVY rereview all passed; root gates G1/G4/G5/G6 verified.
- 2026-08-28 live preflight refresh (second consecutive goal turn): AWS identity remains unauthenticated and all public health endpoints fail before HTTP; root G2/G3 remain unmet with explicit evidence in leaf 1.4.1.
- 2026-08-28 third live refresh: independent HEAVY and parent checks now prove all three public health endpoints return 200/ok with sanitized voice and owner-integration readiness; AWS identity remains unavailable, ECS state is unobserved, and real Telegram/provider executions still require owner actions, so root G2/G3 remain unmet.

## Phase 1.5 contract: exhaustive function-file cleanup

- Scope: every current Python file under `app/` or `scripts/` containing at least one
  `def` or `async def`; tests, migrations, archived material, generated files, and
  function-free package markers are excluded. Baseline inventory: 164 files and 1,622
  definition lines, measured before audit.
- Required disposition: every in-scope file is opened and receives one evidence-backed
  `KEEP`, `SIMPLIFY`, `MERGE`, or `REMOVE` disposition. Findings must name concrete
  symbols, callers/tests, behavior risk, and expected benefit; line count alone is not a
  reason to edit.
- Preservation contract: no architecture redesign, new runtime agent/model/provider,
  ambient authority, weakened test, or accepted-ADR reversal. One production OwnerGraph
  agent remains. Existing dirty-worktree changes are preserved.
- Audit leaves are read-only and own disjoint path groups. Only the driver may authorize
  implementation after synthesis; implementation ownership is then reassigned by exact
  file list. A fresh HEAVY reviewer with no implementation context reviews the result.

### Phase 1.5 tree

- 1.5 Exhaustive function-file cleanup ............................... gates/node-1.5.md
  - 1.5.1 API, channels, graphs, services (23 files) ................ gates/leaf-1.5.1-function-audit-api.md
  - 1.5.2 Domain and brain (73 files) ............................... gates/leaf-1.5.2-function-audit-domain.md
  - 1.5.3 Capabilities, core, DB, integrations, workers, scripts
    (68 files) ...................................................... gates/leaf-1.5.3-function-audit-infra.md
  - 1.5.4 Synthesis, bounded implementation, and review ............. gates/leaf-1.5.4-function-cleanup.md
    - 1.5.4a Owner boundary and typed reply loop ..................... gates/leaf-1.5.4a-owner-boundary.md
    - 1.5.4b Approval and cancellation idempotency .................. gates/leaf-1.5.4b-approval-safety.md
    - 1.5.4c Dead surface and operator-script cleanup ................ gates/leaf-1.5.4c-mechanical-cleanup.md
    - 1.5.4d Notification fan-out and retry semantics ............... gates/leaf-1.5.4d-notifications.md
    - 1.5.4e Client authority and health truth ...................... gates/leaf-1.5.4e-client-trust.md
    - 1.5.4f Full verification and fresh review ..................... gates/leaf-1.5.4f-final-review.md

- 2026-08-28 Phase 1.5 opened after Assaf requested a final all-function-files cleanup sweep; baseline inventory is 164 function-bearing production/script files and 1,622 definition lines, partitioned 23 + 73 + 68 with no overlap or omission.
- 2026-08-28 Assaf resumed AWS authorization successfully: production is healthy on ECS task mia:28 with 1/1 running, completed rollout, and rollback enabled. The deployed image matches old HEAD ae432f6, not the current worktree; deployment waits for Phase 1.5 and a fresh full gate, with mia:28 retained as rollback.
- 2026-08-28 Phase 1.5 audit coverage parent-verified: 23 API/graph/service + 73 domain/brain + 68 infra/integration/script files = 164 rows and 164 unique current function-bearing files, with zero missing, extra, or duplicate paths. Three leaf ledgers are 4/4 met.
- 2026-08-28 synthesis accepted two implementation waves: owner boundary, approval/idempotency, and mechanical dead-surface cleanup first; notifications and client-authority/health alignment second; then full gates and a clean-room HEAVY review.
- 2026-08-28 implementation wave 1 parent-verified on the merged tree: owner boundary 68 tests, approval safety 170 tests, and mechanical cleanup 286 tests all pass; their leaf ledgers are complete. Notification/client-authority integration is in progress.
- 2026-08-28 implementation wave 2 parent-verified after ambiguity/authority/health corrections: 108 focused tests pass. The first clean-room HEAVY review failed on six concrete items and the second rereview found two deeper adversarial transitions; all eight are now repaired. Parent final checks pass 181 cross-cutting Sheets/HANDOFF/E2E tests, the full 2,419-test suite, Ruff, origin binding, 273/273 deterministic evals, exact 164-file inventory reconciliation, and diff-check. Strict production/script C901 and definition lines both remain at their 37/1,622 baselines while the named dead/duplicate surfaces are removed. The absent pre-cleanup physical-line baseline is explicitly not claimed. Another clean-room HEAVY review is pending.
- 2026-08-28 the third clean-room HEAVY review found Sheets pre-claim ordering, ordinary per-recipient recovery, and hot-handoff kill-order defects. All three are repaired and parent-verified by a 282-test combined suite. The current complete tree passes 2,424 tests, Ruff, origin binding, 273/273 deterministic evals, diff-check, and exact 164-row audit reconciliation. Current measured totals are 1,626 definitions, 42,054 physical lines, 37,373 nonblank lines, and 37 strict C901 findings. A fourth fresh HEAVY review is pending before AWS deployment.
- 2026-08-28 the fourth clean-room HEAVY review failed on negated/subset Sheets writes, legacy-claim migration resends, and empty returning-session notifications. Two disjoint MID repair leaves closed all three plus natural HE/EN negation symmetry. Parent checks pass a 287-test combined suite, the full 2,429-test tree, Ruff, origin binding, 273/273 evals, diff-check, and exact 164-row reconciliation. Current metrics are 1,630 definitions, 42,144 physical lines, 37,453 nonblank lines, and 37 C901 findings. A fifth fresh HEAVY review is required before AWS deployment.
- 2026-08-28 the fifth clean-room HEAVY review failed on two P1 Sheets transitions: multiple complete targets still permitted model target selection, and a whitespace-only quoted cell normalized to an empty provider write. A separate MID repair leaf now requires one unquoted allowlisted id plus one unquoted bounded A1 target and rejects trim-empty cells before port construction or claiming. Parent checks pass 98 focused Sheets/principal tests, the 289-test combined suite, the full 2,431-test tree, Ruff, origin binding, 273/273 evals, diff-check, and exact 164-row reconciliation. Current metrics are 1,632 definitions, 42,202 physical lines, 37,505 nonblank lines, and 37 C901 findings. A sixth fresh HEAVY review is required before AWS deployment.
- 2026-08-28 the sixth clean-room HEAVY review failed on bare `not append`/`not update` and spaced tab-name collapse that permitted suffix target selection. A separate MID repair leaf now treats standalone and bounded modifier-bearing English negatives as denial-only, keeps quoted cell data inert, and extracts exactly one complete bounded A1 target including its full spaced tab prefix. Parent checks pass 99 focused Sheets/principal tests, the exact 290-test 19-file suite, the full 2,432-test tree, Ruff, origin binding, 273/273 evals, diff-check, and exact 164-row reconciliation. Current metrics are 1,631 definitions, 42,198 physical lines, 37,503 nonblank lines, and 37 C901 findings. A seventh fresh HEAVY review is required before AWS deployment.
- 2026-08-28 the seventh clean-room HEAVY review failed on a finite-adverb Sheets-negation bypass, non-audio Telegram media reaching STT, and order-dependent Gmail recovery evidence. A separate MID repair plus parent adversarial follow-up replaced the negation grammar with a simpler turn-level fail-closed rule, added normalized audio MIME enforcement, and isolated fake draft resources. Parent checks pass 149 focused repair tests, both Gmail orders, the exact 305-test 19-file suite, the full 2,447-test tree with 1,928 warnings, Ruff, origin binding, 273/273 evals, diff-check, and exact 164-row reconciliation. Current metrics are 1,633 definitions, 42,223 physical lines, 37,524 nonblank lines, and 36 C901 findings. An eighth fresh HEAVY review is required before AWS deployment.
- 2026-08-28 the eighth clean-room HEAVY review failed on punctuation/maqaf Hebrew negation, voice effects before dedupe, alternate-port byte validation, and same-process Gmail repetition. A separate MID repair plus parent follow-ups close all four with canonical pre-media webhook claims, exact verified preclaim reuse, shared byte/MIME validation, Hebrew-letter boundaries, fresh test resources, and a safe one-time failure contract for malformed alternate-port return shapes. Parent checks pass the exact 321-test 19-file suite, both Telegram duplicate routes plus Gmail recovery twice in one process, the full 2,463-test tree with 1,955 warnings, Ruff, origin binding, 273/273 evals, diff-check, and exact 164-row reconciliation. Current metrics are 1,634 definitions, 42,251 physical lines, 37,550 nonblank lines, and 36 C901 findings. AWS login is authenticated; a ninth fresh HEAVY review is still required before commit or deployment.
- 2026-08-28 the ninth clean-room HEAVY review failed on two P1 Sheets bindings: pointed/cantillated Hebrew negators and secondary complete A1 targets without repeated introducers reached claims and fake-provider mutation. A separate MID repair plus parent parser follow-up now strips Unicode combining marks only after quoted literals are masked and binds one exact introduced range while rejecting every remaining bare or bang-qualified A1 token; validator-legal lowercase/mixed spaced tabs remain accepted. Parent checks pass 101 focused Sheets/principal tests, the exact 322-test 19-file suite, the full 2,464-test tree with 1,955 warnings, Ruff, origin binding, 273/273 evals, diff-check, and exact 164-row reconciliation. Current metrics are 1,635 definitions, 42,270 physical lines, 37,567 nonblank lines, and 36 C901 findings. A tenth fresh HEAVY review is required before commit or deployment.
- 2026-08-28 the tenth clean-room HEAVY review failed on one P1 Unicode edge: U+034F is an `Mn` mark with combining class zero, so combining-class filtering missed marked standalone `לא`/`אל`. The repair now removes every Unicode `M*` category plus visually inert `Cf` format controls after quoted literals are masked; stored/provider text is unchanged. Parent checks again pass 101 focused Sheets/principal tests, the exact 322-test 19-file suite, the full 2,464-test tree with 1,955 warnings, Ruff, origin binding, 273/273 evals, diff-check, and exact 164-row reconciliation. Current metrics are 1,635 definitions, 42,274 physical lines, 37,571 nonblank lines, and 36 C901 findings. An eleventh fresh HEAVY review is required before commit or deployment.
- 2026-08-28 the eleventh clean-room HEAVY review failed on two P1 Sheets bindings: whole-turn NFKC let canonically equivalent but non-exact quoted values mutate, and chained target introducers were accepted. The repair keeps quoted JSON literals as raw decoded codepoints, rejects adjacent mixed EN/HE introducer chains, and a parent follow-up fails malformed non-string cells before trimming plus closes mixed-case chains. Parent checks pass 102 focused Sheets/principal tests, the exact 323-test 19-file suite, the full 2,465-test tree with 1,955 warnings, Ruff, origin binding, 273/273 evals, diff-check, and exact 164-row reconciliation. Current metrics are 1,635 definitions, 42,281 physical lines, 37,578 nonblank lines, and 36 C901 findings. A twelfth fresh HEAVY review is required before commit or deployment.
- 2026-08-28 the twelfth clean-room HEAVY review failed on two P1 target-binding gaps and one P2 valid-input denial: punctuation/parenthesis-separated introducer chains reached mutation, lowercase secondary A1 targets evaded rejection, and mixed-case English introducers were denied. The bounded repair now treats English introducers case-insensitively, rejects punctuation/newline-separated repeated EN/HE chains, and scans every remaining bare or bang-qualified A1 target case-insensitively. Parent verification passes 18 owner-live tests, the exact 324-test 19-file suite, the full 2,466-test tree, whole-tree Ruff, 273/273 evals, and diff-check. Current metrics are 164 function files, 1,635 definitions, 42,283 physical lines, 37,580 nonblank lines, and 36 C901 findings. A thirteenth fresh HEAVY review is required before commit or deployment.
- 2026-08-28 the thirteenth clean-room HEAVY review failed on two P1 Sheets authority gaps: `:`, `!`, or `-` between repeated introducers still reached mutation, and residual absolute/whole-column/whole-row A1 references were not rejected. The bounded repair now treats every non-word separator as an introducer-chain separator and uses one residual A1 grammar covering relative, absolute/mixed, whole-column, and whole-row references while leaving quoted JSON strings inert. Parent verification passes 18 owner-live tests, the exact 324-test suite, the full 2,466-test tree, whole-tree Ruff, origin binding, 273/273 evals, and diff-check. Current metrics are 164 function files, 1,635 definitions, 42,288 physical lines, 37,584 nonblank lines, and 36 C901 findings. A fourteenth fresh HEAVY review is required before commit or deployment.
- 2026-08-28 the fourteenth clean-room HEAVY review failed on one P1 underscore-separated introducer bypass and one P2 false denial for opaque allowlisted IDs ending in A1-like text. The repair treats LOW LINE as an introducer separator without matching `at` inside longer alphanumeric words, masks exactly one complete raw ID occurrence outside the selected target, and rejects duplicate A1-like ID occurrences rather than hiding them. Parent verification passes 20 owner-live tests, the exact 326-test 19-file suite, the full 2,467-test tree, whole-tree Ruff, origin binding, 273/273 evals, and diff-check. Current metrics are 164 function files, 1,637 definitions, 42,312 physical lines, 37,604 nonblank lines, and 36 C901 findings. A fifteenth fresh HEAVY review is required before commit or deployment.
- 2026-08-30 the fifteenth clean-room HEAVY review failed on marked/format-controlled secondary A1 references, malformed or unquoted extra cells, an overlapping allowlisted-ID false denial, and stale read-only prompt wording. The bounded repair strips `M*`/`Cf` only from the residual security view, validates every quoted candidate as a JSON string, rejects explicit unquoted scalar cells, masks the selected target before allowlisted-ID counting, and documents ADR-042's exact Sheets-write exception. Parent verification passes 22 owner-live tests, the exact 326-test 19-file suite, the full 2,470-test tree, whole-tree Ruff, origin binding, 273/273 evals, and diff-check. Current metrics are 164 function files, 1,638 definitions, 42,369 physical lines, 37,657 nonblank lines, and 36 C901 findings. A sixteenth fresh HEAVY review is required before commit or deployment.
- 2026-08-30 the sixteenth clean-room HEAVY review failed on one P1 complete-cell binding gap: 59 unquoted JSON scalar/container variants could be dropped while a quoted subset reached port construction, the durable claim, an idempotency row, and the fake provider. The bounded repair scans non-string JSON candidates only in explicit EN/HE write/list positions after masking the exact selected ID, target, and quoted strings; the parent follow-up also fails malformed container openers closed. Parent verification passes 23 owner-live tests, the exact 329-test 19-file suite, the full 2,471-test tree, whole-tree Ruff, origin binding, 273/273 evals, and diff-check. Current metrics are 164 function files, 1,638 definitions, 42,392 physical lines, 37,679 nonblank lines, and 36 C901 findings. A seventeenth fresh HEAVY review is required before commit or deployment.
- 2026-08-30 the seventeenth clean-room HEAVY review failed on two P1 Sheets bindings: punctuation/brackets after EN/HE introducers hid 24 extra-cell variants, and Counter comparison allowed order/grid-layout changes. The repair accepts bounded non-word separator runs in the residual scanner and requires a rectangular payload whose exact A1 dimensions and row-major sequence match the owner-stated literals. Parent verification passes 24 owner-live tests, the exact 330-test 19-file suite, the full 2,472-test tree, whole-tree Ruff, origin binding, 273/273 evals, and diff-check. Current metrics are 164 function files, 1,640 definitions, 42,426 physical lines, 37,707 nonblank lines, and 36 C901 findings. An eighteenth fresh HEAVY review is required before commit or deployment.
- 2026-08-30 the eighteenth clean-room HEAVY review failed on one P1 malformed-cell class: 1,227 unmatched closing-container variants were invisible to the residual candidate scanner and reached all four counted effects. The repair treats `]`/`}` symmetrically with openers and also rejects bounded JSON-like pseudo-cells, while quoted forms stay inert. Parent verification passes 24 owner-live tests, the exact 330-test 19-file suite, the full 2,472-test tree, whole-tree Ruff, origin binding, 273/273 evals, and diff-check. Current metrics are 164 function files, 1,640 definitions, 42,429 physical lines, 37,710 nonblank lines, and 36 C901 findings. A nineteenth fresh HEAVY review is required before commit or deployment.
- 2026-08-30 the nineteenth clean-room HEAVY review failed on one P1 structural class: 1,000 arbitrary unquoted bare-token variants could be omitted while quoted cells still reached all four counted effects. The repair removes the finite blacklist and accepts only four complete positive mutation-clause shapes (English/Hebrew, values-first/target-first) after replacing exact quoted strings, the selected ID, and the selected target with distinct private-use sentinels. Follow-ups removed an ID-side wildcard and made raw readable or private-use sentinel collisions fail closed while preserving quoted lookalikes as exact data. Parent verification passes 24 owner-live tests, the exact 330-test 19-file suite, the full 2,472-test tree, whole-tree Ruff, origin binding, 273/273 evals, and diff-check. Current metrics are 164 function files, 1,642 definitions, 42,459 physical lines, 37,736 nonblank lines, and 36 C901 findings. A twentieth fresh HEAVY review is required before commit or deployment.
- 2026-08-30 the twentieth clean-room HEAVY review failed on one P1 and one P2 Sheets transition: a later valid same-operation suffix could discard an earlier unauthorized mutation clause/readable sentinel, and Hebrew target-first denied a valid write when the spreadsheet ID equalled the A1 target. The bounded repair requires exactly one requested EN/HE operation verb, rejects raw readable/private sentinels outside JSON literals across the whole turn, and evaluates both semantic role assignments for equal ID/target tokens. Parent verification passes 25 owner-live tests, the exact 331-test 19-file suite, the full 2,473-test tree, whole-tree Ruff, origin binding, 273/273 evals, and diff-check. Current metrics are 164 function files, 1,645 definitions, 42,507 physical lines, 37,778 nonblank lines, and 36 C901 findings. A twenty-first fresh HEAVY review is required before commit or deployment.
- 2026-08-30 the twenty-first clean-room HEAVY review failed on two P1 Unicode security-view gaps: 64 M*/Cf/full-width earlier operation variants and nine obscured readable-sentinel variants evaded the raw counter/guard, with all 73 crossing the four counted effects. The bounded repair masks JSON strings, applies NFKD compatibility normalization, removes every M*/Cf character, and casefolds only an intent/security view used for operation, negation, sentinel, and positive-clause checks; raw IDs, targets, literals, payloads, and provider data remain exact. Parent verification passes 26 owner-live tests, the exact 332-test 19-file suite, the full 2,474-test tree, whole-tree Ruff, origin binding, 273/273 evals, and diff-check. Current metrics are 164 function files, 1,646 definitions, 42,529 physical lines, 37,797 nonblank lines, and 36 C901 findings. A twenty-second fresh HEAVY review is required before commit or deployment.
- 2026-08-30 the twenty-second clean-room HEAVY review failed on one P1 whole-turn authority defect: 24 mixed-script, sentinel-confusable, split-operation, and multilingual mutation-bearing prefixes were discarded while a later valid suffix crossed all four effects. The structural repair removes suffix-only matching and full-matches the complete security-view request using only bare, `Please`, exact `Please record this now:`, or tested `אלופה` prefaces plus one operation and one complete bound clause. Parent verification passes 27 owner-live tests, the exact 333-test 19-file suite, the full 2,475-test tree, whole-tree Ruff, origin binding, 273/273 evals, and diff-check. Current metrics are 164 function files, 1,646 definitions, 42,537 physical lines, 37,805 nonblank lines, and 36 C901 findings. A twenty-third fresh HEAVY review is required before commit or deployment.
- 2026-08-30 the twenty-third clean-room HEAVY review passes with no unresolved P0/P1/P2/P3. It independently reproduces the persistent 126-case and 42-case matrices and adds a 168-case whole-turn probe: 246 combined denials are effect-free and 90 positives replay with one durable row/provider mutation. Owner-live 27, exact 333, full 2,475, whole-tree Ruff, origin binding, 273/273 evals, diff-check, and exact 164/164 inventory all pass. Phase 1.5 cleanup gates and root G7 are accepted; root live Telegram/provider G2/G3 and AWS deployment remain pending.

## Phase 1.6 contract: live reliability closure

- Scope: repair content-free Telegram voice failure evidence, truthful and retry-safe
  website-to-WhatsApp owner notification, Composio account binding plus valid no-data
  parsing, and the user's exact allowlisted Google Sheet.
- Architecture: one production OwnerGraph agent remains. Development sub-agents own
  disjoint files only; no production swarm, TTS, WhatsApp brain, or Sheets system of
  record is introduced.
- Finish line: `gates/leaf-1.6-live-reliability.md` plus root G2, G3, and G8 require
  current-tree tests, fresh HEAVY review, an exact-SHA deployment, and live acceptance.
- 2026-08-31 sanitized pre-deployment evidence in
  `gates/evidence/phase16-live-predeploy.md` records that the configured Composio user
  did not own an active account; `mia/prod` was rebound to the sole user owning all
  eight required ACTIVE toolkits while preserving all other fields and the previous
  secret version.
- 2026-08-31 the same sanitized evidence records the exact production-bound Sheet ID
  and successful bounded read of `10 Mia Activity!Z236`. Writes remain gated to a fresh
  authenticated owner Telegram request; current-tree parser behavior is not claimed
  live until the next exact-SHA deployment and re-probe.

## Phase 1.7 contract: direct handoff and owner-authorized Composio breadth

- Scope: repair the website direct-sales/human-handoff state boundary, prove truthful
  website-to-Telegram delivery across graph and WhatsApp-click paths, and let the one
  authenticated OwnerGraph agent discover the complete tool surface of ACTIVE Composio
  toolkits without injecting a raw catalog into every model turn.
- Safety: ClientGraph never receives owner tools. Numeric Telegram identity, `Principal`,
  capability/risk policy, approval, kill switch, idempotency, audit logging, R5 denial,
  and channel one-sender rules remain code-enforced. Composio OAuth selects connected
  toolkits but does not replace Mia's execution policy.
- Reproduction: the exact Hebrew sequence `אפשר להגיע לאסף?` then `יאללה` must produce
  an actual handoff and CTA instead of returning to discovery. A generated sentence
  cannot advertise a handoff unless deterministic graph state agrees.
- Delivery: graph-selected HANDOFF and WhatsApp CTA click both notify the numerically
  allowlisted Telegram owner independently of `MIA_WHATSAPP_HANDOFF_SEND`; confirmed or
  ambiguous recipient outcomes are not duplicated, definite failures remain retryable,
  and visitor copy never claims an unaccepted delivery.
- Orchestration: three disjoint MID cleanup workers own client intent, notification
  delivery, and Composio breadth. The HEAVY driver integrates them after a barrier and a
  fresh HEAVY reviewer with no build context must be able to fail the result.
- Finish line: focused regressions, whole-tree Ruff, full pytest, deterministic evals,
  diff-check, and independent review pass locally. Production remains explicitly open
  until an exact-SHA deployment and controlled live website-to-Telegram acceptance are
  authorized and observed.

- 2026-08-31 Assaf selected ADOPT after reviewing the execution prompt. Three disjoint
  implementation/cleanup leaves were dispatched; no deployment or live side effect is
  authorized by this phase.
- 2026-08-31 Phase 1.7 local implementation is complete after three MID cleanup leaves
  and an adversarial fresh HEAVY review. The current tree passes all 2,623 collected
  tests, whole-tree Ruff, origin binding, 273/273 deterministic evals, JavaScript syntax,
  and diff-check. The reviewer reports RELEASE PASS with no unresolved P0-P2 blocker.
  Exact-SHA deployment, migration application, and controlled live acceptance remain
  intentionally open.

## Phase 1.8 contract: Mia number-one-assistant release

- Scope: make the configured Mia spreadsheet her self-maintained business workspace;
  close owner Gmail/Calendar/LinkedIn approval writes, broad connection auditing,
  Instagram analytics fallback, and website WhatsApp-to-Telegram notification; then
  merge and deploy the exact verified commit.
- Spreadsheet ownership: Mia owns the fixed CRM tabs and records every business-domain
  movement represented by the application (lead, source, follow-up, meeting, deal,
  content, KPI, and Mia activity). Assaf never has to maintain the workbook. PostgreSQL
  remains the durable ledger and source of truth; the Sheet is Mia's managed operating
  view, not an authority that can erase or rewrite internal state.
- Approval safety: Gmail send, Calendar create/move, and non-destructive LinkedIn writes
  require exact owner approval, a durable pre-provider claim, idempotency, and an
  auditable terminal or pending-review outcome. Ambiguous writes never auto-replay.
- Performance: CRM structure maintenance is background/one-off and never blocks a
  visitor or owner request. Instagram fallback is bounded and stops on provider,
  authorization, transport, or rate-limit failure.
- Release: the complete current tree must pass focused checks, whole-tree Ruff, full
  pytest, diff-check, and a fresh HEAVY review able to fail the release. Only the exact
  reviewed commit may be pushed, merged, and deployed; production health and task/image
  identity are verified after rollout.
- Gate: `gates/leaf-1.8-number-one-assistant-release.md`.

- 2026-09-01 Phase 1.8 opened after Assaf clarified that Mia fully manages her configured
  spreadsheet and asked for push, merge, deploy, and a concise list of remaining open
  work. Three earlier P1 review blockers are repaired; current-tree parent verification,
  a fresh release review, and exact-commit deployment are pending.
- 2026-09-01 Phase 1.8 local parent gate passes: managed CRM 98, approval crash-safety 5,
  website/Instagram/owner edge 76, focused Gmail callback/crash 16, full tree 2,668,
  whole-tree Ruff, origin binding, and diff-check all pass. The first full run exposed
  one stale retry expectation; the current contract blocks ambiguous Gmail send replay
  and its strengthened regression plus the complete rerun are green. Fresh HEAVY review
  is next; no push or deployment has occurred yet.
- 2026-09-01 first fresh HEAVY Phase 1.8 review BLOCKED release on four reproduced
  defects: OAuth-style 400 continued Instagram fallback, LinkedIn proposals lacked the
  immediate approval keyboard, the aggregate audit consumed booked-meeting notifications,
  and LinkedIn's 255-character approval field rejected practical post bodies. Three
  disjoint repair leaves are active; release review, push, and deployment remain gated.
- 2026-09-01 all four review blockers are repaired. Parent verification now passes all
  2,675 tests, whole-tree Ruff, origin binding, and diff-check. The aggregate audit's
  non-consumption regression identifies the exact notification row and is independent of
  shared-suite ordering. A new fresh HEAVY release review is the next gate.
- 2026-09-01 the next HEAVY review found and reproduced two additional P1 release
  blockers: cross-turn LinkedIn approval-button binding and a production PostgreSQL
  `approvals.resource_id` width mismatch. Both are repaired with exact ID propagation and
  an additive PostgreSQL widening migration. Parent verification passes 2,680 tests,
  whole-tree Ruff, origin binding, and diff-check; a second fresh HEAVY reviewer reports
  `VERDICT: PASS` with no unresolved P0-P2. Exact-commit release is next.
