# Handoff

**Date:** 2026-08-24
**Load:** `docs/PROJECT_MAP.md` → `AGENTS.md` → `docs/ARCHITECTURE.md` → `docs/BRAIN_ARCHITECTURE.md` → `docs/DECISIONS.md` → current tree.

Package manager **uv**. Python `>=3.12`. PowerShell: `;` not `&&`. Do not inspect `.env`.
Production keys: AWS Secrets Manager `mia/prod`.

## Current

Live Fargate is **mia:16** (task `mia:18`). Brain slice (ADR-026) is deployed: migrate, ingest, and Telegram webhook re-register already ran. `/health` → `brain` ready; `knowledge_chunks=31`. Assaf still needs to send a real Telegram message to prove OpenAI accepts `gpt-5.6-terra`.

v1 channels (ADR-017) unchanged. The owner Telegram console now runs an allowlisted
read-only tool loop with long-term memory and website knowledge when
`MIA_OWNER_AGENT_MODEL` is set; empty keeps the `owner_telegram_v2` classifier, which is how
the test suite runs. Writes and approvals never reach the model. Sales prompt pin is still
`sales_reply_v6`. WhatsApp was out of scope for this slice and is unchanged.

## Brain deploy (done 2026-08-24)

Migrate (`mia:17`), service on `mia:18`, `mia-ingest-knowledge`, webhook re-register. Rollback: image `mia:15`, or blank `MIA_OWNER_AGENT_MODEL` to fall back to the classifier.

## Do not redo

Adapter map ADR-015, WhatsApp send split ADR-016, first AWS host ADR-014, Region eu-north-1
ADR-019, brain architecture ADR-026 (portable vectors over pgvector; HTML over MarkdownV2;
transcription over audio-in chat).

## Next

1. Assaf live-tests Telegram: a free question (not a keyword) and one Approve button.
2. WhatsApp remains deferred — needs a dedicated number and proven Cloud API inbound.
3. Open medium finding unchanged: `_notify_telegram` notifies only `sorted(owner_ids)[0]`.

Historical slice notes: `docs/archive/HANDOFF.md`.
