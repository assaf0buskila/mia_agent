# Performance budget

**Date:** 2026-08-21  
**Status:** Phase 1 target contract (Adjustment I). Not measured in production.  
**Related:** `docs/PRE_PRODUCTION_GAP_REPORT.md`, `docs/RUNTIME_DECISION_PLAN.md`

Do not treat these numbers as current SLOs. No P50/P95 harness exists yet. `ai_runs.tokens_*` and `cost_usd` are stored as 0 this slice (`app/domain/ai_runs.py`).

## Targets

| Workflow | Target | Current path (honest) |
| --- | ---: | --- |
| Webhook acknowledgement | under 500 ms | **Not met by design.** WhatsApp/Instagram/Composio/ManyChat handlers verify, claim, run graph/tools, then HTTP 200 (`app/api/whatsapp.py`, `inbound.py`). Ack includes work. |
| Simple message route | P95 under 4 s | Unmeasured. Website + inbound sales graph is one node (`app/graph/orchestrator.py`) plus canned or one LLM compose. |
| Message with one business lookup | P95 under 6 s | Unmeasured. Calendar offer / Sheets mirror / research can add HTTP. |
| Calendar slot proposal | P95 under 7 s | Unmeasured. `CalendarPort.find_free_slots` on `OFFER_MEETING`. |
| Owner daily summary from prepared metrics | P95 under 8 s | Unmeasured. Counts from Postgres; no Meta on daily brief path. |
| Complex campaign analysis | async if over 10 s | **Sync today** on owner analytics ack (`app/domain/campaigns.py`). |
| Deep research | async with task state | **Sync today** (`ResearchPort.search` on owner ack or meeting brief). |
| External browser task | async | **Not implemented** (gated). |

## Hot-path rules (customer sales)

Must avoid: dynamic Composio discovery, browser, multi-agent debate, extra verifier model, loading all tools/knowledge, large full-history prompts.

Today: NBA is code (`select_next_action`); customer graph has no Composio tools; compose is canned or one Chat Completions call with fallback; extract is deterministic. Matches the avoid-list except sequential LLM is at most primary + one fallback.

Use: compact `GraphState`, deterministic scoring, env model ids (not a typed router yet), async enrichment **later**.

## Instrumentation (partial)

Every graph run should capture: total/node/queue/model/tool/db latency, tokens, cost, retries, cache hit, selected model, selected tool.

Present today: `ai_runs` (run_id, graph_version, model label, next_action, kill_switch, wall-clock `latency_ms` around `graph.invoke`; tokens from compose on live path; `cost_usd` 0); `tool_runs` (tool/status/result_count; `latency_ms` from port wall-clock on research/meta/linkedin/calendar enrich + STT, and sales-tab / session-tab / campaign-tab / content-tab Sheets upserts after claim; denied-before-call enrich stays 0). Still missing: node/queue/db timers, retries, cache hit, P50/P95 harness.

## How to close the gap

1. Record timings on `ai_runs` / `tool_runs` without logging PII.
2. Split webhook ack from processing only after Assaf approves ingress (Phase 2/5) — that is the only way to hit 500 ms ack.
3. Move campaign analysis and research off the owner HTTP request when they exceed 10 s (async task row).
4. Do not add a verifier model on the customer hot path to “improve quality.”
