# Model and transcription benchmark plan

**Date:** 2026-08-21  
**Status:** Phase 1 plan (Adjustment J + K). Not a routing decision.  
**Eventual decision doc:** `docs/MODEL_ROUTING_DECISION.md` — write only after the frozen set is scored.  
**Related:** `app/evals/`, `docs/PROVIDER_MATRIX.md`, `AGENTS.md` (Cursor models ≠ Mia runtime models)

## Rules

- Do not hard-code a production brain in application code. Runtime ids stay env/eval (`MIA_SALES_MODEL`, `MIA_OPENAI_TRANSCRIBE_MODEL`).
- Cursor build-time models (Grok / Fable / GPT / Composer) are not Mia’s router.
- Do not select by brand. Score Hebrew, sales judgment, tool-call validity, latency, and cost.
- No TTS. No voice-agent runtime.

## Typed task classes (target router)

**Registry (lookup only):** `app/domain/policies/task_classes.py` — `TaskClass` StrEnum, frozen `TaskClassPin`, `task_class_pin()`. Not a live router; write `docs/MODEL_ROUTING_DECISION.md` only after scoring.

| Class | Current owner | Benchmark later against |
| --- | --- | --- |
| route | Deterministic inbound/owner classify (`app/domain/owner_tasks.py`) | Optional small model only if classify gaps are proven |
| extract | Code `app/domain/extract.py` | Keep code unless evals fail |
| transcribe | `OpenAITranscribePort` | ≥2 STT providers |
| normal sales conversation | `OpenAISalesReplyPort` / canned | OpenAI + any other key already in project |
| sales reframe / objection | Same compose port; NBA is code | Same |
| campaign interpretation | Code `app/domain/campaigns.py` | LLM only if code recs fail eval |
| deep research | Firecrawl search, no LLM synthesize | Out of hot path |
| summarization | `ThreadSummaryPort` | Existing OpenAI-or-canned |
| message humanity review | Deterministic linter 3/4/6/9 | Lightweight rewrite model only if Assaf ADOPTs |
| safety verification | Risk policy code | Adversarial suite, not a model |

Routing inputs when a router exists: task type, complexity, risk, latency budget, context size, tools, language, cost limit, fallback status.

## Frozen set (minimum counts)

| Bucket | Required | Current Graph Lab |
| --- | ---: | --- |
| Routing | 20 | **`routing_v1`** 20 isolated cases (`run_routing_eval`; Hebrew+English; `classify_owner_task` only; no NBA/reply/judge) |
| Extraction | 30 | **`extract_v1`** 30 isolated cases (`run_extract_eval`; Hebrew+English; `_sales_field_matches`; no NBA/reply/judge) |
| Sales conversation turns | 50 | **`sales_v1`** 50 one-shot NBA+reply cases (`run_sales_eval`; 50/50) |
| Objection | 20 | **`objection_v1`** 20 cases (`run_objection_eval`; extract→NBA→reply+lint; Hebrew+English; no judge) |
| Calendar tasks | 20 | **`calendar_v1`** 20 isolated cases (`run_calendar_eval`; ADR-012 `carve_policy_slots` only; no NBA/reply/judge) |
| Campaign interpretation | 20 | **`campaign_v1`** 20 isolated cases (`run_campaign_eval`; `analyze_insights` + `format_recommendation_line` only; no NBA/reply/judge) |
| Owner voice-note transcripts | 20 | No frozen audio set |
| Humanity linter | 20 | `writing_v1` ~31 + `test_humanity.py` — closest to complete |
| Safety | 20 | **`safety_v1`** 20 cases (`run_safety_eval`; 12 sales extract→NBA→reply+lint+forbidden + 8 `sanitize_snippets`; no judge) |

Hebrew and English required in sales/writing sets.

## Transcription benchmark (Adjustment K)

Two providers, sanitized real notes: Hebrew, English, mixed, technical terms, names, noise, short commands, long instructions.

Metrics: WER/word accuracy, important-entity accuracy, latency, cost, confidence, file-format, failure behavior.

Store on transcript row: provider, model, language, confidence, duration, cost (always 0), retention (`text_only` on save). Audio stays out of logs; minimize retention.

## Compare only models actually available

Today in repo config: OpenAI Chat Completions + GPT Transcribe. Grok/Gemini/Claude are **not** wired. Do not add providers in the benchmark harness until Assaf wants that cost. Score empty/`canned` as the baseline.

## Decision gate

After scoring: write `docs/MODEL_ROUTING_DECISION.md` with KEEP (env ids + canned fallback) or ADOPT a typed router. Humanity rewrite remains a Better-Way, not a silent add.
