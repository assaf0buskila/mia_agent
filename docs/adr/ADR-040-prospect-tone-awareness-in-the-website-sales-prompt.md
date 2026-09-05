# ADR-040 Prospect tone awareness in the website sales prompt

- **Status:** accepted
- **Date:** 2026-08-26
- **Assaf:** ADOPT (chat: "keep it, make sure it wire correct and help mia use")

**Context**
The same cleanup pass introduced `app/domain/emotion.py`: 192 lines of deterministic keyword matching over Hebrew and English that infers one of eight prospect tones (frustrated, overwhelmed, stressed, skeptical, excited, tired, worried, uncertain), with carry-forward from the previous turn when the current message is a short non-substantive answer. No LLM call. It was wired into the live customer sales prompt and took the `PROMPT_VERSION` string `v8` — which production's shipped answer-then-ask contract (ADR-028) already owned. Two different `v8` prompts existed, and the frozen prompt hash pinned in `tests/unit/test_ai_runs.py` disagreed on both sides.

**Decision**
Keep the feature and wire it properly. One prompt version, **`sales_reply_v9`**, carries BOTH contracts: production's answer-then-ask over `PUBLISHED ASSAFWEB FACTS`, and a prospect-tone block. `ReplyContext` carries both `knowledge` and `emotional_cues`; `app/graph/orchestrator.py` passes both. Tone changes **delivery only** — when a visitor asks a direct question while sounding frustrated, Mia still answers the question first. The tone block is omitted entirely when no cues are detected, so neutral messages get no invented empathy. The frozen prompt hash is recomputed from the merged prompt, never copied from either side.

**Consequences**
Live Hebrew customer copy changes: Mia acknowledges tone before continuing. Because detection is deterministic keyword matching, it is testable and cheap, but it will miss paraphrases and can false-positive on quoted text — it must never be the basis of a business decision, only of phrasing. `sales_reply_v8` is retired on both lineages; anything citing `v8` is stale.

**Alternatives considered**
Drop it — rejected by Assaf; the behavior is wanted. Ship it behind a default-off flag — rejected; a flag that is never turned on is dead code, and Assaf asked for Mia to actually use it. Keep two prompt versions — impossible; one string, one prompt. Infer tone with a model call — rejected; a second untrusted inference per turn for a phrasing hint, against §7 ("do not create LLM calls for deterministic work").
