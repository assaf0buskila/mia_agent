# AGENTS.md — how to work on Mia

Read this, then `HANDOFF.md` (current state), then `MIA_V2.md` (what exists), then
`TASKS.md` (what is open). When code and a doc disagree, the code is right — fix the doc.

## Boundaries — not negotiable

- Never read `.env`. Secrets live in AWS Secrets Manager; only names go in `.env.example`.
- No production mutation, key change or live external write without the user's say-so.
- Owner access is by numeric Telegram ID only. A username never grants access.
- Website visitors get public knowledge and their own session — never owner tools,
  private memory, or another visitor's history. Holding a phone or email grants nothing.
- The server alone decides whether a contact was captured or delivered. The model may
  never assert it, and may never supply or substitute a contact value.
- Every owner external write is an exact, immutable, expiring proposal, executed only
  after approval and re-validation of the current target. Prohibited actions stay
  prohibited even when approved.
- Never: TTS or voice output, autonomous social or ads actions, cold DM, fake urgency,
  production self-editing, Sheets as the sole record, dual-transport sends.
- Log reason codes and booleans. Never log visitor text or provider payloads.

## Working in this repo

- Several agent sessions write this tree concurrently. Work in your own git worktree.
  Never bare `git stash`. Never reset, clean or restore someone else's changes.
- Verify with:
  `MIA_ENV=test uv run pytest --basetemp=.cache/<unique>`,
  `uv run ruff check app tests`, `node tests/unit/widget_behavior.test.js`.
  Redirect pytest output to a file — piping through `tail` loses the summary line.
- Do not weaken a test to make a change pass. Retire a test only with the behaviour it
  covers, and say so in the commit.
- A "verified dead" list inherited from a previous session is a list of hypotheses.
  Before deleting anything, re-grep for callers **and** for dotted-string references —
  `monkeypatch.setattr("a.b.c", …)`, capability `port="…"` — which import-greps miss.
- Deploy only from a clean checkout at the exact CI-green SHA. Procedure in `README.md`.
- Do not infer live model, voice or delivery quality from mocked tests. Prove it against
  production and read the reason-code logs.

## Model routing for development

Model IDs live only here; briefs refer to tiers. Production runtime models are separate
`MIA_*` settings.

| Tier | Model | Use for |
|---|---|---|
| HEAVY | gpt-6-astra | design, security and approval decisions, independent review |
| MID | gpt-5.6-sol | bounded implementation and tests |
| CHEAP | gpt-5.6-luna | inventories and mechanical extraction — never decides |

The author never reviews their own work. At most four agents in parallel, each with
its own files.
