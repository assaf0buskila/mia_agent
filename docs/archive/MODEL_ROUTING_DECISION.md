# MODEL ROUTING DECISION

Which model runs which task, and why. Companion to ADR-023.

Reviewed 2026-08-23 by reading the code, not the config comments. Model ids live in
env / Secrets Manager and are deliberately absent from this file.

## Task inventory

Every task Mia performs, and what actually executes it today.

| Task | Executor | Model call? |
| --- | --- | --- |
| Sales signal extraction | `app/domain/extract.py` | No |
| Next-best-action selection | `app/domain/sales.py` | No |
| Reply copy selection (HE/EN) | `app/graph/replies.py` | No |
| Owner task classification | `app/domain/owner_tasks.py` | No |
| Owner follow-up reference resolution | `app/domain/owner_followups.py` | No |
| Owner reads (approvals, website conversations, lead review) | `app/domain/owner_reads.py` and the `apply_owner_*` handlers | No |
| Human-voice lint | `app/domain/humanity.py` | No |
| Campaign interpretation | `app/domain/campaigns.py` | No |
| Calendar slot policy | `app/domain/meeting_availability.py` | No |
| Website / WhatsApp sales reply | `app/integrations/sales_reply.py` | Yes |
| Owner Telegram phrasing | `app/integrations/owner_reply.py` | Yes |
| Gmail thread summary | `app/integrations/thread_summary.py` | Yes |
| Voice note transcription | `app/integrations/transcribe.py` | Yes |

Four model call sites. Everything that decides, permits, scores or routes is pure
Python.

## Decision 1: deterministic tasks never call a model

Classification, extraction, next-action selection and permission checks stay in
code. A model is not more accurate at "does this text contain a lead id", it is
slower, it costs money per turn, and it cannot be unit tested to a fixed answer.
This also means a model outage degrades phrasing, never routing.

Consequence: adding a new owner intent means adding a phrase list and a test, not
a prompt. That is intentional friction — it keeps the audit trail exact.

## Decision 2: the sales reply is a paraphrase, not a free author

`select_next_action` picks the move and `reply_for` writes a canonical version of
it. The model receives that canonical copy as `FALLBACK_PHRASING`, reasons
silently about the conversion turn (`sales_reply_v5`), and rewrites the message
in the prospect's language. It does not choose the move.

This is the load-bearing choice in the whole system. It means:

- Sales judgment is testable without spending a token. The eval datasets score the
  deterministic layer, which is the layer that can lose a deal.
- A weak model produces stiff phrasing, not a wrong strategy.
- Hallucinated ROI numbers, invented pricing and early pitches are structurally
  hard, because the intent the model is paraphrasing does not contain them.

Guard rails on the model output, in `OpenAISalesReplyPort.compose`:

1. Kill switch on → canned, no HTTP.
2. Output failing `lint_customer_reply` → try the next model.
3. Output repeating Mia's previous turn → try the next model.
4. All attempts exhausted → canned.
5. Orchestrator re-checks for repetition after compose returns.

## Decision 3: the owner channel paraphrases typed results

Telegram owner replies are still built from typed reads and templates. A phrasing
pass (`owner_telegram_v2`) rewrites the RESULT the way the sales port rewrites
canned copy. Classification, follow-up resolution, approvals and Composio calls
stay in Python. The model does not receive a tool catalog (ADR-025).

Rationale: Assaf asked for conversation reasoning on Telegram, not a webhook
ack. What "מה מחכה לאישור?" must still return is the real list. If the paraphrase
drops a lead id, invents a tool name, or the kill switch is on, the canned RESULT
is sent instead.

The Phase 5 goal (different requests answer differently) remains a routing and
data problem, proven by `tests/unit/test_owner_distinct_replies.py`. Fluency is
now the paraphraser's job.

## Decision 4: fallback chains per port, no cross-task router

Each of the four model ports has its own primary and fallback chain from env:

- Sales reply: OpenAI primary → OpenAI fallback → Gemini (OpenAI-compatible) → canned.
- Owner Telegram: shares the sales OpenAI/Gemini chain → canned.
- Gmail summary: shares the sales OpenAI chain → canned.
- Transcription: its own primary → fallback → disabled.

There is no runtime table mapping task class to model. `app/domain/policies/task_classes.py`
is a lookup registry for ownership, explicitly not a live router. Building a router
before there is scoring data to route on would be inventing configuration surface
we cannot justify.

## What has not been measured

Stated plainly so nobody reads this file as a benchmark result.

The eval harness (`app/evals/`) is fully deterministic. It scores the extraction,
next-action, canned-copy, objection, safety and writing layers. It does not call a
real model, and it does not measure latency, tokens or cost. `persist_ai_run`
records `latency_ms`, `tokens_in` and `tokens_out` from live traffic, and writes
`cost_usd=0` — that is a placeholder, not a measurement.

Therefore this document makes **no claim** about which candidate model is best on
Hebrew quality, English quality, sales judgment, latency or cost. No per-model
scores exist. Selecting a model on brand or on vibes is exactly what the goal
forbids, and quoting invented numbers would be worse.

To produce a real comparison, the following is required and is not yet built:

1. A harness mode that calls the live ports with candidate model ids, instead of
   scoring `reply_for` output.
2. Per-attempt capture of latency, prompt tokens and completion tokens.
3. A price table to turn tokens into cost. None is hard-coded today, deliberately.
4. Lint pass rate measured on model output rather than on canned copy.
5. For transcription: a held-out set of owner voice notes with reference
   transcripts. None exists.

Until that runs, the model ids in Secrets Manager stand as the operator's choice,
and the architecture above is what limits the damage a bad choice can do.

## Health surface

`/health` exposes `sales_llm` and `sales_gemini` booleans from
`Settings.sales_llm_ready()` and `Settings.sales_gemini_ready()`. They report
whether a key and a model id are both present. They do not report whether the
provider is currently answering, and must not be read as a liveness signal for the
model.

`ai_runs.model` records the model the compose path would reach for first, or
`canned`. As of 2026-08-23 it accounts for a Gemini-only deployment; before that
fix such a deployment was logged as `canned` while it was in fact paraphrasing.
