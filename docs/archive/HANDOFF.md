# Handoff

**Date:** 2026-08-24
**Load:** `docs/PROJECT_MAP.md` → `AGENTS.md` → `docs/ARCHITECTURE.md` → `docs/BRAIN_ARCHITECTURE.md` → `docs/DECISIONS.md` → current tree.

Package manager **uv**. Python `>=3.12`. PowerShell: `;` not `&&`. Do not inspect `.env`.
Production keys: AWS Secrets Manager `mia/prod`.

## Current

Live Fargate is **mia:18** (task `mia:20`). ADR-028/029 deployed: migrate ran before the service moved. Owner agent model is `gpt-5.6-luna`. Meeting-first is on. Assaf still needs to send `מה קרה היום?` to prove the funnel/engine brief lines.

**Apify research (ADR-030, not deployed).** Typed `ResearchPort` can use pinned `apify/google-search-scraper` when Firecrawl is empty. Add `MIA_APIFY_TOKEN` to `mia/prod` (empty is fine) before shipping a task definition that injects it.

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

## Telegram slice (2026-08-24, NOT deployed)

**The owner agent has never actually run in production.** `/health` says `ready` because it
only checks the model string is non-empty. Live replies were the pre-brain keyword
classifier — proven by Mia quoting the `owner_telegram_v2` fallback prompt verbatim.
`gpt-5.6-terra` is real and GA; the documented mechanism that fits is a project
model-permissions allowlist. Full analysis and deploy steps: `docs/TELEGRAM_SLICE_REPORT.md`.

Fixed here: `MIA_OWNER_AGENT_FALLBACK_MODEL` was documented but ignored (only `chain[0]` was
built); the fallback to the classifier was silent (now `log_owner_agent` prints model +
status every turn); leads now carry a human headline from the prospect's own words; the
handoff briefing is ~510 chars with the transcript in an expandable quote.

Needs migration `20260824_lead_sales_state_headline.sql` before the image serves traffic.

## Next

1. Set `MIA_OWNER_AGENT_MODEL=gpt-5.6-luna` (known-good), fallback `gpt-5.6-terra`.
2. Run `scripts/probe_owner_agent.py` to confirm which models the key can call.
3. Assaf live-tests Telegram: a free question (not a keyword), one Approve button, one voice note.
2. WhatsApp remains deferred — needs a dedicated number and proven Cloud API inbound.
3. Open medium finding unchanged: `_notify_telegram` notifies only `sorted(owner_ids)[0]`.

Historical slice notes: `docs/archive/HANDOFF.md`.
