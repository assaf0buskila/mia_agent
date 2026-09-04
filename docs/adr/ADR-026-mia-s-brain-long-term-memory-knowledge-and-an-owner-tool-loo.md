# ADR-026 Mia's brain: long-term memory, knowledge, and an owner tool loop

- **Status:** accepted
- **Date:** 2026-08-23
- **Assaf:** ADOPT (chat: A1 full agentic loop, B-now website brain, C2 handoff card; make her know me, remember, learn from the website and Telegram, understand voice. WhatsApp explicitly out of scope.)

**Context**
The owner console was a keyword switchboard: a phrase table picked one of ~25 task types, a 700-line `if` chain ran one Python function, and the model only rephrased the result. Off-list phrasing fell through to a generic digest, two requests in one message were impossible, and nothing survived the conversation. Mia had no long-term memory at all — `app/domain/memory.py` was a 24-turn transcript read model over `canonical_events`. Separately, owner voice notes were failing silently in production: the transcription adapter sent `response_format=verbose_json` with `gpt-transcribe`, a whisper-1-only format, and the caller swallowed the error into "לא תפסתי את ההקלטה".

**Decision**
Add `app/brain/`: a memory store with four kinds (episodic, semantic, working, preference), website/business knowledge ingestion, hybrid retrieval, and extraction/consolidation. Add an owner tool-calling loop over an allowlisted read-only registry. Keep every write and approval intent on the existing deterministic path (`DETERMINISTIC_TASK_TYPES`). Store embeddings as base64 float32 in a portable `TEXT` column and do exact cosine in Python — not pgvector, whose SQLAlchemy type is PostgreSQL-only and would make the test suite exercise a different retrieval path than production. Fix transcription to pick parameters per model family. Move Telegram to `parse_mode=HTML` with inline approve/reject buttons and `callback_query` handling.

**Consequences**
Mia answers freely, chains several reads in one turn, and remembers across conversations. Nothing about the safety architecture moved: the allowlist is enforced server-side on the returned tool name, writes still go through `app/core/risk.py` and `app/domain/approvals.py`, and the model never sees a Composio catalog. Every layer degrades independently — no model keys means the old classifier answers, which is how the 2000-test suite runs. New tables are additive and create identically on SQLite and Postgres. Gated actions finally have a completion path (one-tap approval) instead of a dead end. `MIA_OWNER_AGENT_MODEL` is the on switch; it ships empty.

**Alternatives considered**
Hybrid router keeping the classifier as a fast path — rejected; Assaf picked A1, and the classifier's single-task ceiling was the actual complaint. pgvector with an HNSW index — rejected at this scale; pgvector documents exact search as perfect-recall, and its own IVFFlat sizing rule yields a degenerate `lists=3` here. Routing raw audio to an audio-in chat model instead of transcription — rejected; roughly an order of magnitude more expensive per minute and it loses the documented Hebrew language/keyword hints. MarkdownV2 for Telegram — rejected; 18 escape characters under three context-dependent rules, and every id, email and decimal Mia interpolates is a landmine there. Firecrawl crawl as the primary knowledge source — rejected; the site already publishes `llms.txt`/`llms-full.txt`/`pricing.md`, which is cleaner, free, and owner-maintained. Firecrawl stays as the fallback.
