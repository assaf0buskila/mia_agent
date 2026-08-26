# Mia architecture (VNext)

Two LangGraph entry points, shared core, thin channels. Decisions: `docs/DECISIONS.md`. Product: `docs/PRODUCT.md`.

```text
                   MIA
                    │
          ┌─────────┴──────────┐
          │                    │
     OWNER GRAPH          CLIENT GRAPH
     Telegram              Website
    text + voice          text + voice
          │                    │
          └─────────┬──────────┘
                    │
            Shared Mia Core
            (state, memory, STT, logging)
                    │
              Capability Layer
                    │
                 Policy
                    │
                Composio / direct ports
                    │
             External Systems
```

## Graphs

| Graph | Users | Tools | Must not |
| --- | --- | --- | --- |
| OwnerGraph | Assaf on Telegram | Allowlisted owner capabilities | Run client sales NBA; dump Composio catalog |
| ClientGraph | Website visitors | Approved knowledge, meeting/handoff, lead capture | Execute owner capabilities or see other visitors |

Deterministic code owns identity, NBA, scoring, permissions, idempotency. Models paraphrase and, on the owner side, choose among pinned tools. Untrusted text is data.

Graph state is serializable domain data only. No SDK clients, no secrets.

## Channels

Channels adapt transport. They do not reason.

| Surface | Adapter job | Then |
| --- | --- | --- |
| Telegram | Webhook, numeric allowlist, HTML, voice download, callbacks | OwnerGraph |
| Website | Session/message/voice/handoff HTTP + widget | ClientGraph |
| WhatsApp | Meta HMAC inbound; silent send (ADR-024) | ClientGraph for NBA persist only. Telegram briefing on website click. |
| Gmail ingest | Composio webhook | ClientGraph for prospect NBA; owner mail via capability |
| Instagram | Meta HMAC (not a sales inbox) | ClientGraph persist; insights as owner READ |

Telegram access: `MIA_TELEGRAM_OWNER_USER_IDS` numeric only. Owner inbound replies send `parse_mode=HTML` (body escaped) and attach `approval_keyboard` on pending-approval reads. Callbacks already edit the same message after a tap. Telegram HTTP calls `process_owner_texts` (`app/api/owner.py`).

## Voice

One `TranscriptionPort` (`app/integrations/transcribe.py`). Website and Telegram share it. Provider is replaceable; credentials stay in env. No TTS.

## Memory and knowledge

Reuse `app/brain/` (ADR-026). Postgres tables `brain_*`. Embeddings are base64 float32 in TEXT; similarity is Python cosine, not pgvector.

Semantic memory/extraction/embeddings **do not run** unless model ids are configured (`MIA_OWNER_AGENT_MODEL`, `MIA_EXTRACTION_MODEL`, `MIA_EMBEDDING_MODEL`). Empty defaults fail closed. Knowledge ingest is `uv run mia-ingest-knowledge` (CLI, not EventBridge). Website visitors cannot write owner memory.

## Capability and policy

Graphs call named capabilities (`mail.read`, `calendar.get_schedule`, `leads.get_recent`, `memory.search`, `knowledge.search`, `research.search`, …), never raw Composio tool slugs. Live owner calendar, hot-lead lists, memory search, knowledge search, and research search go through `execute_capability`.

Each capability has `{READ, WRITE, SENSITIVE_WRITE, DESTRUCTIVE}`, allowed graphs, confirmation, retry, and idempotency. Enforcement is Python (`app/core/risk.py` plus the capability registry). Prompt text cannot add a tool.

Client allowlist is dramatically narrower than owner. Website visitors never inherit the owner Composio session.

Existing risk map: R0 read AUTO, R1 low write AUTO, R2 customer message AUTO in approved scope, R3 commercial APPROVAL, R4 marketing APPROVAL, R5 destructive DENY. Kill switch denies before side effects. Website chat and voice both return 503 when it is on.

## Composio

Tool supplier behind adapters. Pin schemas. No catalog dump. Stable owner identity → one Composio user/session (`MIA_COMPOSIO_USER_ID`), not a new session per message.

Direct APIs remain where they win (Meta inbound HMAC, STT, LinkedIn member analytics).

## Persistence

Postgres is the system of record. Sheets is a human-readable mirror, never read back. Do not add another database.

## Cross-graph events

ClientGraph never pretends to be Owner Mia.

```text
ClientGraph → domain event → notification service → Telegram
```

Events: `WebsiteConversationCompleted`, `LeadNeedsHuman`, `MeetingBooked`, `HighIntentLeadDetected`. Renderer formats Hebrew. Idempotency on conversation id + version.

## Website UX contracts (do not break)

Widget `app/web/ask_mia.js` depends on:

- `GET /v1/website/widget.js`, `GET /v1/website/config`, `GET /v1/website/preview`
- `POST /v1/website/sessions`
- `POST /v1/website/sessions/{id}/messages`
- `POST /v1/website/sessions/{id}/voice`
- `POST /v1/website/sessions/{id}/handoff`
- `POST /v1/website/sessions/{id}/events`
- `POST /v1/website/sessions/{id}/end`

Preserve pill, bubbles, composer mic, palette, WhatsApp card. No TTS. No competing WhatsApp FAB.

## Runtime

ECS Fargate + RDS + Secrets Manager `mia/prod` + ALB `https://mia.assafweb.com` in **eu-north-1**. Do not copy `.env` onto Fargate. Do not auto-deploy from a rebuild.

## Current wiring

- Website: `app/api/website.py` → ClientGraph (`load_conversation` → `retrieve_knowledge` → `sales_turn` or skip on end/inactivity → `complete_turn`). Inner sales NBA is still `build_graph` in `app/graph/orchestrator.py` (REUSE until that node is inlined). Widget close, inactivity (`mia-due-scan`), and handoff finalize inside `complete_turn`.
- Telegram: `app/api/telegram.py` → `process_owner_texts` → OwnerGraph (`load_owner_context` → `retrieve_owner_knowledge` → `respond`) in `app/api/owner.py`. Owner mail/calendar/leads/research stay allowlisted tools behind `respond`, not extra nodes.
- Prospect Meta/Gmail: `app/api/inbound.py` → ClientGraph (same NBA, not a third graph). WhatsApp outbound stays human-gated (ADR-024). Mixed tests that pass owner ids still delegate to `process_owner_item`.
