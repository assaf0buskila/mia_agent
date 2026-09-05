# ADR-023 Model routing: deterministic decisions, model paraphrases

- **Status:** proposed
- **Date:** 2026-08-23
- **Assaf:** unset

**Context**
Phase 6/7 asked which model runs which task, chosen on measured quality rather than brand. Reading the code found three model call sites (sales reply paraphrase, Gmail thread summary, transcription) and no runtime task-to-model router. Everything that decides, permits, scores or routes is pure Python. The eval harness is fully deterministic: it never calls a real model and measures no latency, tokens or cost, so no per-model scores exist to select on.

**Decision**
Keep the current split and write it down: deterministic tasks never call a model; the sales reply is a paraphrase of deterministic canonical copy, never a free author; each model port keeps its own primary/fallback chain with no cross-task router. Full reasoning and the explicit list of what has not been measured are in `docs/MODEL_ROUTING_DECISION.md`. Do not claim a model comparison until a live-port harness mode with latency, token and cost capture exists. Owner Telegram phrasing was later added as the same paraphrase pattern (ADR-025); classification and tools stay in Python.

**Consequences**
A model outage degrades phrasing, never routing or permissions. Sales judgment is testable for free, which is why the eval datasets are worth trusting. A new owner intent costs a phrase list and a test rather than a prompt. The cost of this shape is that `ai_runs.cost_usd` stays a placeholder until a price table exists. Selecting or changing a production model id remains an operator action with no scoring behind it.

**Alternatives considered**
Let a model choose the next sales action — rejected; it moves the deal-losing decision into the untestable layer. Add a model composer for Telegram owner replies — deferred at the time; taken in ADR-025 after Assaf asked for conversation reasoning. Build the task-class router now — rejected; routing configuration with no scoring data behind it is invented surface. Publish candidate model scores from the existing harness — rejected; the harness scores canned copy, so those numbers would describe nothing.
