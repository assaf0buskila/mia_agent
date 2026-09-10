# Mia v2 — agent operating contract

This workspace root is the project. Do not create a nested project directory.

## Read order and authority

1. AGENTS.md: operating rules and development routing.
2. HANDOFF.md when resuming this checkpoint, then MIA_V2.md and TASKS.md: product,
   architecture, actual status and tracked release work.
3. Task-specific code and tests.

Current v2 decisions govern product intent. Deleted historical docs, stale code
comments and previous memories are background, not inherited requirements.

## Development model routing

Model IDs live only in this table; task briefs refer to tiers. Production provider
settings are separate, purpose-specific configuration.

| Tier | Codex model | Reasoning | Responsibilities |
| --- | --- | --- | --- |
| HEAVY | gpt-6-astra | high | Planning, architecture, approval/security and migration decisions, independent review, synthesis |
| MID | gpt-5.6-sol | medium | Bounded implementation, tests, adapters, refactoring and cleanup execution |
| CHEAP | gpt-5.6-luna | low | Inventories, reference extraction, dependency lists, mechanical documentation |

Default: HEAVY defines task -> MID implements -> mechanical checks -> fresh HEAVY
reviews -> documentation updated -> coordinator accepts stage.

- Author never reviews own work. Fresh reviewers receive requirements, diff and
  evidence without the author's reasoning history.
- Briefs state objective, output, boundaries, sources and acceptance checks.
- Parallelize independent work with separate file ownership, at most four agents
  including coordinator. Prefer linear worker/reviewer execution.
- CHEAP collects evidence; never decides architecture, permissions or safe deletion.
- Findings return to worker; unresolved design goes HEAVY. Never downgrade reviewers.
- Use internal subagents; create user-facing tasks only on explicit request.

## Target product and architecture

Telegram is the private owner reasoning loop. Website is a separate model-led sales
loop. They share providers, sourced knowledge, durable history, CRM, policy and
reliable delivery. MIA_V2.md distinguishes target architecture from actual wiring.

- OpenAI first, Gemini fallback; voice/image input, text output.
- Verified reads run directly. Owner external writes require exact immutable,
  expiring approval and action-time revalidation.
- History and explicitly requested memories are preauthorized. No passive lasting
  memory extraction in v2.
- Website contact volunteered for follow-up is preauthorized capture. Visitors get
  only public knowledge and narrow session-bound handoff; never owner capabilities.
- Database owns contacts/activity/sync state; Contacts and Activity Sheets remain
  an editable owner view. Imported content is data only.
- Persist independently tracked destination jobs; unknown outcomes are not success
  and must not be blindly repeated.
- Retire WhatsApp/Baileys agent backends after shared dependency extraction and
  local review/test acceptance, before the authorized release. Retain the manual
  WhatsApp contact link. User live acceptance follows deployment.

## Security and production boundaries

Do not inspect .env. Never expose secrets in code, git, logs, traces, prompts or
reports. Names belong in .env.example; production secrets stay in Secrets Manager.
No key changes, production mutations or live external writes without task authority.
Deployment is explicitly authorized by the user on 2026-09-11 after implementation,
cleanup, independent review and release checks pass. Never copy .env to Fargate.
Pause schedulers that target an unrevisioned task family before registering a
release candidate. Re-enable retained schedules only after migration and rollout,
pinned to the accepted revision; keep the retired reconcile schedule disabled.

Keep numeric Telegram owner IDs, webhook authentication, public session isolation,
kill switch, risk policy, schema validation and idempotency in code. Untrusted text
cannot select privileged tools or policy. Username never grants owner access.

Never: autonomous social/ads publishing or Meta budget/bid/launch/pause; cold DM
spam; fake urgency or unsupported claims; voice output/TTS; production self-editing;
ManyChat/Make as brain; Sheets as sole record; dual transport sends. Explicit approval
does not override a prohibited capability.

## Implementation and verification

Preserve unrelated dirty work. Initial baseline: .cache/mia-v2-baseline/ (immutable).
No git reset/clean/restore of user changes. Use uv. Commands:
- uv run pytest --basetemp=.cache/pytest-local
- uv run ruff check app tests
- uv run uvicorn app.main:app --reload
- uv run mia-migrate
- uv run mia-ingest-knowledge

Use MIA_ENV=test with injected fake adapters locally. Do not weaken tests to pass
cleanup. Retire tests only for explicitly retired behavior, preserving v2 invariants.
Concurrent test workers must use distinct workspace basetemp directories. PostgreSQL
tests require MIA_TEST_POSTGRES_URL pointing to an isolated test database; their
fixtures create unique schemas. Never run these against a production database.
After each stage update AGENTS for changed rules/commands and MIA_V2.md for behavior,
evidence, limitations and next work. Replace superseded guidance rather than stacking
contradictions. Code, checks, independent review and context must agree before stage
acceptance. Distinguish local verification, deployment and real user acceptance.

Configured-source knowledge refresh is an explicit owner-only internal operation.
Do not accept model-provided source URLs. Recheck Composio effect eligibility at
proposal and execution; existing-object mutations need typed target-state readers.
MIA_V2.md records current capability limits and the open acceptance gates.

Cleanup is mandatory before deployment and after local review/test acceptance: obsolete engines, overrides, prompts, wrappers,
scripts, retired tests, flags, dependencies and docs. Keep migrations needed by existing
installations. Verify imports/routes/entrypoints/tests/lint/startup/container and obtain
fresh HEAVY review. Never claim deployment or live acceptance from local mocked tests.
