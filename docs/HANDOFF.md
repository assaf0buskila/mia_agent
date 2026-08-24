# Handoff

**Date:** 2026-08-24
**Load:** `docs/PROJECT_MAP.md` → `AGENTS.md` → `docs/ARCHITECTURE.md` → `docs/BRAIN_ARCHITECTURE.md` → `docs/DECISIONS.md` → current tree.

Package manager **uv**. Python `>=3.12`. PowerShell: `;` not `&&`. Do not inspect `.env`.
Production keys: AWS Secrets Manager `mia/prod`.

## Current

Live Fargate is **mia:20** (task `mia:22`). ADR-031 phrasing fix is on. Digest match
`sha256:ee4fab125c515ac1a6a8001b44b33400e1678817b2bf3cd54529054974beb25d`. Owner agent
`gpt-5.6-luna`, fallback `gpt-5.6-terra`. Meeting-first on. `MIA_GMAIL_SEND=false`.
No new migration. No knowledge re-ingest. Rollback: image `mia:19` / task `mia:21`, or blank
`MIA_OWNER_AGENT_MODEL`.

Prove in Telegram: `היי מיה` → one line hello; `תבדקי את המייל` / `can you look at my emails`
→ inbox, not the hello.

v1 channels (ADR-017) unchanged. The owner Telegram console runs an allowlisted
read-only tool loop with long-term memory and website knowledge when
`MIA_OWNER_AGENT_MODEL` is set; empty keeps the `owner_telegram_v2` classifier, which is how
the test suite runs. Writes and approvals never reach the model. Sales prompt pin is
`sales_reply_v8` (ADR-028). WhatsApp stays the website fallback exit, not the default.

## Brain deploy (done 2026-08-24)

Migrate (`mia:17`), service on `mia:18`, `mia-ingest-knowledge`, webhook re-register. Rollback: image `mia:15`, or blank `MIA_OWNER_AGENT_MODEL` to fall back to the classifier.

## Do not redo

Adapter map ADR-015, WhatsApp send split ADR-016, first AWS host ADR-014, Region eu-north-1
ADR-019, brain architecture ADR-026 (portable vectors over pgvector; HTML over MarkdownV2;
transcription over audio-in chat).

## Telegram slice (2026-08-24, deployed via luna then ADR-030)

Owner agent runs with `gpt-5.6-luna` (fallback `gpt-5.6-terra`). Inbox tools and
lead-by-name shipped on **mia:19**. Analysis of the earlier silent classifier fallback:
`docs/TELEGRAM_SLICE_REPORT.md`.

## Next

1. Assaf live-tests Telegram: `היי מיה`, `תבדקי את המייל`, a name/headline lookup.
2. WhatsApp remains deferred — needs a dedicated number and proven Cloud API inbound.
3. Do not enable Gmail send, WhatsApp send, Meta writes, IG auto-reply, TTS, or dump the Composio catalog.

Historical slice notes: `docs/archive/HANDOFF.md`.
