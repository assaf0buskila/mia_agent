# CLAUDE.md — Claude Code sessions on Mia

@AGENTS.md

`AGENTS.md` is the contract and wins on any conflict. This file adds only how a Claude Code
session runs the AssafWeb campaign finish. State lives in `HANDOFF.md` / `TASKS.md`; do not
start a parallel doc hierarchy.

- Plan: `docs/MIA_CAMPAIGN_FINISH_PLAN.md`. Briefs: `docs/MIA_CLAUDE_CODE_PROMPTS.md`.
  Read only the section for the current chunk ID. Never import either file whole.
- Goal: finish Mia, not rebuild her. End state is a clean, CI-green, reviewed SHA with a
  go/no-go (Prompt 4). Deploy is a separate approval (Prompt 5).

## Model per task

`AGENTS.md` tiers, as Claude Code aliases. Its gpt IDs are the Codex side of the same tiers.

| Tier | Claude Code | Use for |
|---|---|---|
| HEAVY | `opus` | C0 audits, chunk planning, approval/security/migration design, synthesis, go/no-go |
| MID | `sonnet` | one bounded subchunk + its tests; read-only verification of one code area |
| CHEAP | `haiku` | inventories, grep sweeps, doc-vs-code diffs. Never decides, never edits `app/` |
| REVIEW | Codex HEAVY, or a fresh `opus` session | independent review of every subchunk diff |

- Anything touching approvals, contact/consent, delivery claims, visitor isolation, send
  paths or migrations is HEAVY to design and HEAVY to review, even if MID implements it.
- Don't switch models mid-debug without a reason. Check the actual selected model with `/model`.

## Context

- The main session holds decisions and evidence, not file dumps. Fan-out reads go to
  subagents, and they return verdict + `file:line` + repro, never whole files.
- One subchunk per session. At the boundary, write SHA / commands / results / next chunk
  into `HANDOFF.md` + `TASKS.md`, commit, then resume fresh with Prompt 3.
- Reuse recorded evidence. Re-verify only what changed since the SHA it was recorded at.

## Subagents

- Read-only fan-out: at most 4 in parallel (AGENTS.md), disjoint areas, background.
- Write agents: only after the interface is frozen, each owns disjoint files in its own
  worktree. Default is one builder.
- No subagent for a small edit, or when the main session already has the context.
- Every subagent prompt carries: chunk ID + goal, owned files, invariants, out-of-scope,
  stop conditions (no provider writes / .env / deploy / merge), and exact return format.
- A subagent's "done" is a claim. The main session spot-checks the load-bearing evidence.

## Chunk protocol

`requirement → failing test → smallest change → focused tests → affected gates → fresh review
→ fix → re-review at final SHA → HANDOFF/TASKS → commit → PR` on `claude/mia-<chunk-id>-<slug>`.

- Order: C0 gates everything, then C1 before CRM-dependent flows, and C2 rendering frozen before card work.
- Two failed fix attempts → stop, write a minimal repro + diagnosis, re-plan.
- Stop at every gate and wait for Assaf. A plan is not consent to execute.
- Evidence states per capability: `CODE_CHECKED`, `LOCAL_TESTED`, `INTEGRATION_TESTED`,
  `STAGING_VERIFIED`, `DEPLOYED`, `PHONE_VERIFIED`, `BLOCKED`, `NOT_INCLUDED`. Never a %.
