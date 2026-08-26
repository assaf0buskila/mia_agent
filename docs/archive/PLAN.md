# Plan: Mia VNext rebuild

Depth: tree 5   Mode: orchestrated
Budget note: Full rebuild of graphs, adapters, capability/policy, voice, finalization, Composio, docs, and deletion. One sitting cannot finish; leaves are phase-sized.

## Contract

Decided BEFORE fan-out. Everything a leaf could get wrong about its neighbors:

- Interfaces:
  - `OwnerGraph` and `ClientGraph` are separate LangGraph compiled graphs.
  - Channels emit a normalized inbound message `{channel, actor_kind, actor_id, conversation_id, text, voice_meta}` then call the matching graph. They do not reason.
  - Voice: one `SpeechToText` port (`async transcribe(audio: bytes, metadata) -> Transcript`). Telegram and website share it. No TTS.
  - Capabilities: typed functions with policy metadata `{READ|WRITE|SENSITIVE_WRITE|DESTRUCTIVE}`. Graphs call capabilities, never raw Composio SDK objects.
  - `OWNER_CAPABILITIES` vs `CLIENT_CAPABILITIES` allowlists enforced in Python.
  - Website visitor never receives owner Composio session.
  - Finalization: one service, idempotent on `conversation_id + final_summary_version`, emits Telegram via notification renderer.
  - Persistence stays Postgres via existing SQLAlchemy session. Do not add a second database. Reuse `app/brain/` if healthy.
- Data ownership (no two leaves share a file):
  - Leaf A (discover): PLAN.md contract + rebuild map only; no app/ edits.
  - Leaf C (docs): `AGENTS.md`, `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md` (+ archive moves). Not app/.
  - Leaf D (skeleton): new `app/agents/`, `app/channels/`, `app/capabilities/`, `app/services/` packages; do not yet switch routers.
  - Leaf E (website text): `app/api/website.py` + widget only as needed; ClientGraph wiring.
  - Leaf F (website voice): STT + website voice route.
  - Leaf G (telegram text): telegram adapter + OwnerGraph.
  - Leaf H (telegram voice): reuse STT; telegram voice branch.
  - Leaf I (finalization): notification service + finalization workflow.
  - Leaf J (first Composio READ): one owner capability (mail.search/read if Gmail already exists).
  - Leaf K: additional capabilities one file-group at a time.
  - Leaf L: delete old graph/inbound mix after traffic moved.
- Naming and conventions:
  - Package manager: uv. Tests: pytest. Lint: ruff.
  - Do not inspect `.env`. Names only in `.env.example`.
  - Secrets never in git/logs/prompts.
  - Hebrew customer copy: 2nd-person plural, no dashes (preserve existing UX).
  - Telegram owner: numeric allowlist, HTML parse_mode, voice in text out.
  - WhatsApp stays human-silent until Cloud API inbound (ADR-024) — keep webhook, do not build a third graph.
  - Do not auto-deploy.

### Claude audit (must not be ignored)

Brain is HTTP-wired but semantically gated:

- Stock defaults: `owner_agent_model=""`, `extraction_model=""`, `embedding_model=""` → DisabledEmbeddingPort, no learn_from_exchange writes, OwnerGraph falls back to classifier.
- Production ECS (BUILD_STATUS 2026-08-24) sets those model ids. That is production evidence; local defaults still fail closed.
- `mia-ingest-knowledge` is CLI-only, not EventBridge. Knowledge tables stay empty until run.
- Capabilities.py declaring BRAIN_* ALIVE is a declaration, not execution proof.
- pgvector is deliberately unused (ADR-026). Do not add it in the rebuild.
- VNext must make the owner/client graphs actually invoke retrieval when models are configured, and must not claim ALIVE for gated-empty defaults.

### Product behavior that must survive (strangler)

KEEP user-facing:

- Ask Mia widget UX (pill, bubbles, palette, mic in composer, no TTS, no competing WA FAB).
- Website sales conversation quality (discovery, meeting-first, WhatsApp offer after context, Hebrew rules).
- Telegram numeric allowlist, HTML, approve/reject buttons, voice STT.
- WhatsApp human-only + Telegram briefing on website click.
- Gmail read/draft (send gated), Calendar read/gated write, Sheets mirror, research, ads read.
- Postgres SoR, kill switch, risk policy, idempotency.

REPLACE internals:

- Single inbound.py mega-handler mixing owner+client.
- Classifier-as-brain for owner (keep as fallback only until OwnerGraph is proven).
- Prompt-as-database for AssafWeb facts (use knowledge/brain).
- Doc maze (PROJECT_MAP/PRD/BUILD_STATUS/HANDOFF/WIRING as living required reading).

## Tree

- 1 Mia VNext rebuild .......... GATES.md (this root)
  - 1.1 Discover + decide .......... gates/node-1.1.md
    - 1.1.1 Phase A discover actual architecture ........ gates/leaf-1.1.1.md
    - 1.1.2 Phase B KEEP/REUSE/REPLACE/DELETE/ARCHIVE ... gates/leaf-1.1.2.md
  - 1.2 Canonical docs .......... gates/node-1.2.md
    - 1.2.1 Phase C AGENTS/PRODUCT/ARCHITECTURE/DECISIONS
  - 1.3 Skeleton .......... gates/node-1.3.md
    - 1.3.1 Phase D OwnerGraph/ClientGraph/adapters/voice/capability/policy/notify
  - 1.4 Website .......... gates/node-1.4.md
    - 1.4.1 Phase E website text → ClientGraph
    - 1.4.2 Phase F website voice → STT → ClientGraph
  - 1.5 Telegram .......... gates/node-1.5.md
    - 1.5.1 Phase G telegram text → OwnerGraph
    - 1.5.2 Phase H telegram voice → STT → OwnerGraph
  - 1.6 Handoff + tools .......... gates/node-1.6.md
    - 1.6.1 Phase I website finalization → Telegram ping
    - 1.6.2 Phase J first Composio READ capability
    - 1.6.3 Phase K expand remaining owner capabilities
  - 1.7 Delete old .......... gates/node-1.7.md
    - 1.7.1 Phase L remove replaced architecture + full suite

## Status log

- 2026-08-25T19:46 plan written, contract fixed, goal armed, Phase A discovery starting
- 2026-08-25T20:00 Phase A–C done: canonical docs + ADR-031; overlapping agent docs archived
- 2026-08-25T20:15 Phase D–J skeleton live: OwnerGraph + ClientGraph, website text/voice through ClientGraph, Telegram owner through OwnerGraph wrapper, mail.read capability+policy, HANDOFF finalization idempotent. inbound.py still exists (strangler). Phase K/L not started. No deploy.
- 2026-08-25T20:55 memory.search + knowledge.search through capability/policy; owner_tools search_* handlers call execute_capability. Calendar booking/freshness tests use live-clock workday slots. No deploy.
- 2026-08-25T21:35 inbound prospect path compiles ClientGraph (channel-aware); leftover overlapping docs already in docs/archive/. Duplicate tool registries remain by design (LLM schema vs Composio pin catalog). No deploy.
- 2026-08-25T21:50 Phase L validation: full pytest green; MIA_REBUILD.MD archived. Inner `build_graph` remains the ClientGraph NBA (REUSE). No deploy.

