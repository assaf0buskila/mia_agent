# FDE operating layer — gap report

**Date:** 2026-08-21  
**Status:** audit only. No application code in this turn.  
**Trigger:** Assaf chat — add an FDE operating layer (policies, modes, tables, tests, metrics). Do **not** ingest video/transcript as RAG. Do **not** rebuild Mia.  
**Related:** Bible / `docs/PRD.md`, `MIA_FINAL_MILE_PLAYBOOK.md`, `MIA_PRE_PRODUCTION_ARCHITECTURE_ADJUSTMENTS.md`, `docs/PRE_PRODUCTION_GAP_REPORT.md`  
**Suite at inspection:** `uv run pytest` **1293 passed** (after unit 4)

## How to read

This layer **wraps** the existing sales brain, identity, tools, calendar, campaigns, and outbound. It does not replace them.

| Label | Meaning |
| --- | --- |
| Exists — reuse | Equivalent already in code; extend, do not duplicate |
| Partial | Real code, wrong contract for FDE |
| Missing | No equivalent |
| Gated | Bible / `AGENTS.md` forbids enabling execute/send/activation in this layer |

Do not add a video, translation, or transcript corpus to RAG. FDE knowledge becomes code, states, tables, tests, and metrics.

---

## Verdict on the idea

**Strong, with required shape changes.** An operating layer that forces fail-closed behavior, shadow comparison, and replay is the right next production investment. Dumping FDE notes into RAG would be a fake equivalent.

Do **not** invent a parallel brain. `select_next_action` stays deterministic code. LLM paraphrase stays behind `SalesReplyPort` + humanity lint. Identity stays phone allowlist, never an LLM.

---

## Item map (requested → current files)

### 1. ExecutionMode + ActionPolicy

**Status: alive (registry only; not wired into graph)** — `app/domain/policies/execution_policy.py` + tests; wraps `RiskLevel`; lookup only; graph/inbound/outbound unchanged

| Requested | Current | File |
| --- | --- | --- |
| `ExecutionMode` (deterministic / ai_automatic / ai_with_review / human_only) | Missing | — |
| `ActionPolicy` (capability, confidence floor, approval, fail-closed, retries) | Missing as a registry | — |
| Fixed rules in code | `select_next_action`, extract, identity, campaign analyze | `app/domain/sales.py`, `app/domain/extract.py`, `app/api/inbound.py` owner_ids |
| Complex judgment to model | Sales **paraphrase only** | `app/integrations/sales_reply.py` |
| Expensive mistakes to a human | R3 approval, R4 approval, R5 deny | `app/core/risk.py` |
| Capability list | Wiring registry, not execution policy | `app/core/capabilities.py` |

`RiskLevel` R0–R5 + `PolicyDecision` AUTO/APPROVAL/DENY + `assert_allowed` is the live write gate. `GraphState.approval_required` exists and is **never routed on** (`app/graph/state.py`, `app/graph/orchestrator.py`).

Example mapping (do not invent a numeric lead score — SalesState already is the score):

| Action | Mode to pin | Evidence today |
| --- | --- | --- |
| NBA / “lead score” | `DETERMINISTIC` | `select_next_action` |
| Intent / extract | `DETERMINISTIC` | `extract_sales_signals` — not an LLM |
| Sales reply compose | `AI_AUTOMATIC` inside lint+canned fallback | `OpenAISalesReplyPort` |
| Low-confidence reply | `AI_WITH_REVIEW` | Missing — lint fail → canned, not human review |
| Meta budget change | `HUMAN_ONLY` / approval | R4 hard-coded; no write adapter |
| Custom quote | `HUMAN_ONLY` / approval | R3 `proposal_handoff` persist-only |
| Data deletion | `HUMAN_ONLY` | R5 deny |
| Identify Assaf | `DETERMINISTIC` | `MIA_WHATSAPP_OWNER_PHONES` exact set |

**Do not replace `RiskLevel`.** Policy looks up capability → `ExecutionMode`, then still calls `assert_allowed` for the R-level. Flags must not override R4/R5 (`docs/PRE_PRODUCTION_GAP_REPORT.md` Adjustment R).

**Proposed path:** `app/domain/policies/execution_policy.py`  
**Do not add:** `app/services/` (repo has no services layer; keep domain + inbound).

---

### 2. AgentDecision + risk_gate

**Status: alive (pure functions; not wired into graph)** — `app/domain/policies/decision.py` + tests; wraps NBA `next_action` + reply; `route_decision` / `risk_gate` reads `ActionPolicy` from unit 1; lookup only; graph still `START → sales_next_action → END`

| Requested field | Closest today | Gap |
| --- | --- | --- |
| `action` | `NextAction` / `state["next_action"]` | Exists |
| `customer_message` | `state["reply"]` | Exists as a string, not a decision object |
| `confidence` | `AgentDecision.confidence` + `ai_runs.decision_confidence="1.0"` (deterministic NBA pin) | Not an LLM score; not a graph gate |
| `evidence_ids` | Canonical `run_id`, tool `provider_event_id` | Not attached to the decision |
| `uncertainty_reasons` | Humanity `reasons` tuple on lint fail | Not persisted; fail → canned |
| `requires_human` | `SalesState.owner_required` → NBA `handoff` | No graph interrupt |
| `approval_required` | GraphState field always false; R3 row on handoff | Not a gate node |
| `next_state` | SalesState in Postgres | Not on the decision |

`route_decision` as specified is **Missing**. Closest: `decide()` / `assert_allowed()` in `app/core/risk.py`, then inbound **always sends** the reply if R2 AUTO (`app/core/outbound.py`).

LangGraph today:

```text
START → sales_next_action → END
```

Requested:

```text
AI decision → risk_gate → execute | ask_clarification | approval | human_handoff
```

`ask_clarification` is in the diagram but **not** in the sample `route_decision()`. Owner Understanding Check already exists (`app/domain/owner_tasks.py`). Prospect path has no clarification interrupt.

**Reuse:** wrap `next_action` + canned/LLM reply into `AgentDecision`. Do not add a second NBA.

**Confidence for deterministic NBA:** pin `1.0` when `select_next_action` ran; pin below floor only when LLM compose is used and lint fails or the model is empty (already fail-closed to canned). Do not ask an LLM to self-score.

---

### 3. Shadow / AutomationMode

**Status: alive (prospect MessagePort only; website HTTP still replies; HYBRID not wired)**

| Mode | Current equivalent | File |
| --- | --- | --- |
| `OFF` | Kill switch blocks gated writes; empty credentials disable ports | `MIA_KILL_SWITCH`, `app/core/config.py` |
| `DRAFT_ONLY` | Follow-up **draft** on due-scan; never send | `app/domain/followups.py`, `followup_voice.py` |
| `SHADOW` | Real graph on real leads; skip prospect MessagePort; persist `shadow_decisions` | `MIA_AUTOMATION_MODE`, `app/domain/shadow.py`, `app/api/inbound.py` |
| `HYBRID` | Missing (not wired this slice) | — |
| `AUTO_APPROVED` | R2 customer message AUTO in approved scope | `app/core/outbound.py` |
| Demo | Synthetic UTMs; not real-lead shadow | `app/core/demo.py`, `app/api/demo.py` |

`SHADOW` must run the real graph on **real** leads, persist the proposal, **skip MessagePort / calendar write / follow-up send**, then later compare to Assaf’s actual action.

Exit criteria listed by Assaf (100% forbidden-action block, no invented claims, handoff accuracy, sales quality, min real conversations, no duplicate actions, latency, measured cost) are **not coded**. Graph Lab scores fixtures, not live shadow rows.

**Proposed table:** `shadow_decisions` (Postgres). Do not store PII beyond existing truncated `message_in` ids. Proposed reply may be stored (it is Mia output, not a lead secret) but must not appear in logs/traces.

**Named write flags** from the pre-production report (`MIA_CALENDAR_WRITE`, `MIA_AUTO_FOLLOWUP`, …) stay **separate** from `AutomationMode`. Three layers:

1. `MIA_KILL_SWITCH` — emergency stop  
2. Named write flags — capability enablement (R4/R5 still hard)  
3. `AutomationMode` — how far a permitted action may go (shadow vs send)

---

### 4. Owner correction capture

**Status: alive (persist-only; no remember-ask; activation gated)**

| Requested | Current | File |
| --- | --- | --- |
| `OwnerCorrection` / `CorrectionScope` | **alive** — `owner_corrections` rows; scope `this_turn` \| `remember` from phrase classify | `app/domain/feedback.py` |
| “רק הפעם או לזכור?” | **Out of scope this slice** | — |
| Durable preference / behavior_rule / correction | Propose-only rows; never active | `app/domain/learning.py`, `owner_instructions` |
| Instruction kinds on propose | `preference` / `behavior_rule` / `correction`; `fact` deferred | same |
| Activation after approve | **Gated** | `AGENTS.md`, HANDOFF |

Do **not** auto-activate a business rule from a reply edit. Owner PREFERENCE inbound with `InstructionKind.CORRECTION` persists a logged correction event **and** still calls `propose_owner_instruction` (status `proposed`). Activation remains gated; no prompt rewrite.

**Path:** `app/domain/feedback.py` + `owner_corrections` table + minimal inbound wire in `app/api/inbound.py`.

---

### 5. Historical replay / gold set

**Status: alive** (synthetic Bible-shaped gold + hidden-truth scorer; no private transcripts)

| Requested | Current | File |
| --- | --- | --- |
| `mia_sales_gold.jsonl` with `hidden_truth` | **alive** — 20 synthetic cases | `app/evals/datasets/mia_sales_gold.jsonl` |
| `scripts/replay_sales_cases.py` | Missing as CLI | — |
| `tests/eval/test_sales_regression.py` | Unit evals under `tests/unit/test_evals.py` + `tests/unit/test_sales_gold.py` | `app/evals/harness.py` |
| Discover real problem / premature pitch / ROI lie / wrong meeting | Exact-match action + substring + lint + hidden-truth flags | `sales_v1.json` (11), `buyers_v1.json` (12 personas), `writing_v1.json` (31), `mia_sales_gold.jsonl` (20) |
| Promote only if replay improves | Missing version compare | Graph Lab does not mutate prompts |

**Reuse** `app/evals/harness.py`. Add a **new** dataset; do not replace `sales_v1`. First 20 gold cases can be synthetic-but-Bible-shaped until Assaf supplies sanitized real transcripts (real leads are not in the repo; demo never holds private lead data).

Hidden-truth scoring (must not pitch, must ask workflow, must not invent ROI) is new. Do not add an LLM judge.

---

### 6. Business value events

**Status: alive** (count events only; ILS empty; no deal won/minutes)

| Requested metric | Current | File |
| --- | --- | --- |
| Qualified conversations | `BUSINESS_VALUE` kind `qualified` on fit→`good` transition + SalesState | `app/domain/value.py`, `app/graph/orchestrator.py` |
| Meetings | `BUSINESS_VALUE` kind `booked` after `MEETING_BOOKED` + owner briefs | `app/domain/calendar_booking.py`, `events.py` |
| Follow-up recovered | `BUSINESS_VALUE` kind `recovered` after `FOLLOW_UP` recovered | `app/domain/followups.py` |
| Human handoff | `BUSINESS_VALUE` kind `handoff` on graph handoff + existing `HANDOFF` | `app/graph/orchestrator.py` |
| Deal won | **Missing** (stages `meeting_offered` / `proposal` only; values always `""`) | `app/domain/deals.py` |
| Minutes saved / ILS | Missing — `estimated_value_ils` always `""` on value events | `app/domain/value.py` |
| AI cost per meeting | `ai_runs.cost_usd` **always 0** | `app/domain/ai_runs.py` |
| Success as messages/tool calls | Intentionally not the product KPI | — |

**Hard constraint:** Bible / current deals contract **forbids inferring** `expected_value` / `closed_value`. `estimated_value_ils` on a value event must stay empty unless Assaf later supplies a structured source. `business_value` is **not** in `COUNTABLE_EVENT_TYPES` (weekly KPI unchanged).

Allowlisted canonical type `business_value` with idempotency `{lead_id}:value:{kind}`; payload `kind` + empty `estimated_value_ils` only. R1 `business_value_persist`; `count_business_value` requires `lead_id` and filters by event type + kind.

---

### 7. Node failure policy

**Status: alive (registry only; not wired into adapters)**

| Node | Today on failure | File |
| --- | --- | --- |
| Instagram / Meta insights | Omit snapshot; never zero-fill | `app/integrations/meta_ads.py`, `campaigns.py` |
| Calendar read | Static/canned slots; no invented times | `app/integrations/calendar.py`, kill switch path |
| Calendar write | Lookup + verify; timeout recovery on reschedule | `app/domain/calendar_booking.py`, `meeting_changes.py` |
| Research | Continue without pretending research ran | `app/domain/briefs.py`, `research.py` |
| Transcription | Empty transcript; optional fallback model | `app/integrations/transcribe.py` |
| Meta write | No adapter (gated) | `app/core/risk.py` R4 |
| LLM malformed | Fallback model then canned | `app/integrations/sales_reply.py` |
| Tool status | `ok\|denied\|empty\|error` | `app/domain/tools.py` |

`NodeFailurePolicy` registry in `app/domain/policies/failure_policy.py` pins timeout, retries, fail_closed, fallback token, and notify_owner per allowlisted tool/node name (plus `sales_reply` and `meta_write` aliases). Lookup only — adapters unchanged; `tool_runs.latency_ms` stays 0. Reuses `ToolOutcome` statuses conceptually; does not replace `ToolOutcome`. Capability `fde_failure_policy` ALIVE.

**Reuse** `ToolOutcome`. Add a registry keyed by existing tool/node names. Do not add LangGraph subgraphs for this (pre-production Better-Way: KEEP one sales node).

---

### 8. Graph / prompt / policy version on every run

**Status: Partial** — `policy_version=fde_v1` pinned on new `ai_runs` rows via `POLICY_VERSION` in `app/domain/policies/execution_policy.py`; `prompt_version=sales_reply_v1` pinned beside the sales-reply system prompt (domain duplicates the pin; frozen hash in tests); allowlisted `automation_mode` from settings on website + prospect inbound (`sanitize_automation_mode`; first write wins; owner/Graph Lab excluded); `decision_confidence="1.0"` pinned from `DETERMINISTIC_NBA_CONFIDENCE` (no persist parameter; no LLM self-score). Still missing: `cost_usd` (still 0; tokens wired). Storing `hybrid` is audit-only — HYBRID send is not wired.

Owner path and Graph Lab **do not** write `ai_runs`.

---

## What already satisfies FDE without new types

- Deterministic NBA and identity (never LLM)  
- Humanity lint as a cheap uncertainty detector (claims, questions, AI phrases)  
- Kill switch + R4/R5 hard gates  
- Follow-up draft-only  
- Propose-only owner instructions  
- Graph Lab exact-match eval (local, no self-edit)  
- Calendar verify-before-book  
- Insights omit missing metrics  

Build the layer **on** these. Do not reimplement them.

---

## Overlap with pre-production gap report

Assaf also asked to apply `docs/PRE_PRODUCTION_GAP_REPORT.md`. Complementary, not duplicate:

| Pre-prod Phase 2 | FDE item |
| --- | --- |
| Named write flags | Separate from `AutomationMode` |
| Human takeover | Required before `HUMAN_ONLY` / `human_handoff` is real (today `conversation_killed` is sales `stop`) |
| Approval expiry + resource hash | Required before `AI_WITH_REVIEW` execute |
| `IdempotencyStore` | Required before shadow vs live writes can be compared safely |
| Ingress `correlation_id` | Use as `AgentDecision.evidence_ids` / shadow `run_id` |

Do not implement Lambda/SQS/AgentCore/`app.infra`, instruction **activation**, follow-up **send**, Gmail **send**, Meta **writes**, or dynamic Composio discovery as part of FDE. First live host is ADR-014 (Fargate injects SM); that is not an FDE item.

---

## Better-Way (shape, not a rebuild)

| Topic | Bible / current | Proposed | Recommendation |
| --- | --- | --- | --- |
| Risk | `RiskLevel` + `assert_allowed` | Keep; policy wraps it | **KEEP** risk. **ADOPT** `ExecutionMode` as an extra axis |
| Graph | One node `sales_next_action` | Full subgraph split | **KEEP** one sales node; **DEFER** `risk_gate` as a second graph node — unit 2 pure-function tests do not require it |
| Decision object | `next_action` + `reply` | Parallel `AgentDecision.customer_message` as a second reply | **ADOPT** `AgentDecision` that **wraps** those fields |
| Shadow vs demo | Demo = synthetic | Shadow = real leads, no send | **ADOPT** distinct `AutomationMode`; demo stays fail-closed in prod |
| Value ILS | Deal values always `""` | `estimated_value_ils` on events | **KEEP** empty ILS until a structured source exists |
| Lead score | SalesState fit/pain/missing_fields | New numeric scorer | **KEEP** SalesState; policy labels it deterministic |
| Intent LLM | Deterministic extract | `AI_AUTOMATIC` intent | **KEEP** extract in code |
| Corrections → prompts | Propose-only | Auto-append | **KEEP** gated activation |
| Services folder / RAG video | Not in tree | New service + RAG dump | **REJECT** |

Until Assaf says otherwise on a row above, implement the Recommendation column.

---

## Smallest implementation sequence

One controlled unit at a time. Parent reviews, `uv run ruff check app tests`, `uv run pytest`, update this file + `docs/PRD.md` + `docs/BUILD_STATUS.md` + `docs/HANDOFF.md`.

| # | Unit | In | Out of scope |
| --- | --- | --- | --- |
| 1 | `ExecutionMode` + `ActionPolicy` registry for **existing** capabilities | `app/domain/policies/execution_policy.py`, tests, capability wiring | Graph change, flags, send |
| 2 | `AgentDecision` + `route_decision` / `risk_gate` as pure functions | `app/domain/policies/decision.py`, tests against NBA+handoff+R3 | Graph node |
| 3 | Persist `policy_version` on `ai_runs` | model + store + tests | Prompt dump | **alive** — `POLICY_VERSION=fde_v1` on insert; first write wins |
| 4 | `AutomationMode` config default `SHADOW` for prospect **outbound send only** | config + inbound skip `MessagePort` when shadow; persist `shadow_decisions` | Calendar write, follow-up send (already off) | **alive** — prospect MessagePort skip under SHADOW; website HTTP still replies; HYBRID not wired |
| 5 | Owner correction event persist-only | `app/domain/feedback.py` + table | Activation, prompt rewrite | **alive** — `owner_corrections` logged rows; scope classify; propose still runs; no remember-ask; activation gated |
| 6 | `mia_sales_gold.jsonl` (20 Bible-shaped cases) + harness scorer + `tests/unit/test_sales_gold.py` | eval dataset | model judge, private leads | **alive** — 20 synthetic cases; hidden-truth scorer in `run_gold_eval`; all 20 pass |
| 7 | Business value **count** events (qualified / booked / recovered / handoff) | allowlisted types; no ILS | Deal won, minutes, shekels | **alive** — `business_value` canonical; `{lead_id}:value:{kind}`; ILS always `""`; not in weekly KPI |
| 8 | `NodeFailurePolicy` registry for current tools | `app/domain/policies/failure_policy.py` | New retry framework / AWS | **alive** — lookup registry; pins match ad-hoc fail-closed behavior; not wired into adapters |
| 9 | Wire `risk_gate` as second LangGraph node **if** unit 2 tests demand it | `app/graph/orchestrator.py` | Subgraph swarm | **DEFER** — unit 2 tests pass without graph node; KEEP one sales node |
| 10 | Human takeover state (pre-prod + FDE `human_handoff`) | distinct from `conversation_killed` | Auto-resume | **alive** — `leads.human_takeover`; owner exclusive phrases + `lead_*`; R1 persist; prospect MessagePort skip; graph + `ai_runs` still run; follow-up send-readiness denies `human_takeover`; website HTTP unchanged; no resume phrase |

Units 1–3 need no new provider credentials. Unit 4 is the first behavior change customers could notice (website/WhatsApp replies stop sending while graph still runs). Defaulting production to SHADOW is the point of the layer; call that out in `.env.example`.

---

## First code unit (next turn)

**Create `app/domain/policies/execution_policy.py` only** (plus tests + living docs).

Purpose: pin each live capability to an `ExecutionMode` and fail-closed defaults, wrapping `RiskLevel`.  
Dependencies: `app/core/capabilities.py`, `app/core/risk.py`.  
Acceptance: registry covers current `CapabilityId`s that perform work; identity and Meta write / data delete are `HUMAN_ONLY` or deterministic as in the table; tests prove lookup and that R4/R5 are unchanged.  
Out of scope: graph node, shadow table, send skip, RAG, new scores.

---

## Explicitly not this layer

- Video / translation / FDE notes as RAG  
- Nested `mia/` directory  
- Rebuilding LangGraph sales NBA  
- Instruction activation  
- Meta/LinkedIn writes, Gmail send, follow-up send, calendar provider delete  
- Lambda / SQS / WAF / AgentCore / `app.infra` (first live host is ADR-014; not this layer)  
- Inferring deal ILS or lead scores  
- Make/n8n on the sales path  
