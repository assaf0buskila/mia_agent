# ADR-036 VNext two graphs + canonical docs

> Renumbered from ADR-031 in the `mia:20` merge (see ADR-034).

- **Status:** accepted
- **Date:** 2026-08-25
- **Assaf:** ADOPT (chat: `/goal` rebuild per `MIA_REBUILD.MD`)

**Context**
The live app is one FastAPI process with a one-node sales LangGraph and a custom owner tool loop inside `process_inbound_texts`. Documentation required agents to load PROJECT_MAP, PRD, BUILD_STATUS, HANDOFF, and more. Brain code is on the HTTP path but semantic memory is gated on empty default model ids. There is no conversation-finalization → Telegram summary (only WhatsApp-click briefing).

**Decision**
1. Two compiled LangGraph entry points: `OwnerGraph` (Telegram) and `ClientGraph` (website). Shared core; separate state, prompts, tools, permissions.
2. Channels stay thin adapters. Capability layer + Python policy sit in front of Composio. Website visitors cannot execute owner capabilities even under prompt injection.
3. Canonical agent reading: `AGENTS.md`, `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`. Operator files `docs/RUNBOOK.md` and `docs/PRODUCTION_BUILD.md` remain for humans/tests, not required agent reading. ADR-021’s living-doc list is superseded; ManyChat stays unmounted.
4. Reuse `app/brain/`, STT, Postgres, risk policy. Strangle `inbound.py` — do not delete until the new path is tested.
5. Add explicit website conversation finalization with idempotent Telegram notify.
6. Do not auto-deploy. Do not add pgvector. Do not declare brain embeddings/extraction ALIVE when model ids are empty.

**Consequences**
Owner and client reasoning no longer share one inbound mega-handler. Prospect Meta/Gmail inbound compiles ClientGraph (same NBA, not a third graph). New integrations plug in as capability + policy + adapter + graph allowlist. Old classifier remains fallback until OwnerGraph is proven. Production still needs `MIA_OWNER_AGENT_MODEL` / `MIA_EXTRACTION_MODEL` / `MIA_EMBEDDING_MODEL` plus a one-off `mia-ingest-knowledge` for a non-empty corpus.

**Alternatives considered**
Keep one ReAct loop for both users — rejected (rebuild §36). Rewrite brain onto pgvector — rejected (ADR-026). Delete WhatsApp/Gmail webhooks in this slice — rejected; preserve production contracts until replaced.
