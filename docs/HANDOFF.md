# Handoff

**Date:** 2026-08-23
**Load:** `docs/PROJECT_MAP.md` → `AGENTS.md` → `docs/ARCHITECTURE.md` → `docs/BRAIN_ARCHITECTURE.md` → `docs/DECISIONS.md` → current tree.

Package manager **uv**. Python `>=3.12`. PowerShell: `;` not `&&`. Do not inspect `.env`.
Production keys: AWS Secrets Manager `mia/prod`.

## Current

Live Fargate is still **mia:15**. The brain slice (ADR-026) is **in the tree, not deployed**.

v1 channels (ADR-017) unchanged. The owner Telegram console now runs an allowlisted
read-only tool loop with long-term memory and website knowledge when
`MIA_OWNER_AGENT_MODEL` is set; empty keeps the `owner_telegram_v2` classifier, which is how
the test suite runs. Writes and approvals never reach the model. Sales prompt pin is still
`sales_reply_v6`. WhatsApp was out of scope for this slice and is unchanged.

## Before deploying the brain slice

1. **Append `docs/brain.env.example` to `.env.example`** — I could not edit it (the
   workspace blocks `.env*`). One test fails until this is done.
2. Add the same names to `mia/prod` and the ECS task definition. Only the non-secret ones
   are new; the API keys already exist.
3. Run `mia-migrate` as a one-off task **before** switching the service —
   `20260823_brain_memory.sql` creates six tables. It is additive and idempotent.
4. Run `uv run mia-ingest-knowledge` once so the knowledge base is not empty.
5. Verify `GET /health` → `brain` shows `ready: true` for the features you configured, and
   `corpus.knowledge_chunks > 0`.
6. If a Telegram webhook was ever registered with `allowed_updates=["message"]`, re-register
   it — that value is sticky server-side and silently drops every button press. Verify with
   `getWebhookInfo`.

Rollback: image `mia:15`, or simply blank `MIA_OWNER_AGENT_MODEL` to fall back to the
classifier without redeploying.

## Do not redo

Adapter map ADR-015, WhatsApp send split ADR-016, first AWS host ADR-014, Region eu-north-1
ADR-019, brain architecture ADR-026 (portable vectors over pgvector; HTML over MarkdownV2;
transcription over audio-in chat).

## Next

1. Assaf appends the env block, deploys, and live-tests the Telegram console.
2. WhatsApp remains deferred — needs a dedicated number and proven Cloud API inbound.
3. Open medium finding unchanged: `_notify_telegram` notifies only `sorted(owner_ids)[0]`.

Historical slice notes: `docs/archive/HANDOFF.md`.
