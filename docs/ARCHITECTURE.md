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

Deterministic code owns identity, NBA, scoring, permissions, idempotency. Models paraphrase and,
on the owner side, choose among pinned tools plus on-demand Composio search/schema/read
meta-tools. Broad "check everything" requests route through one aggregate audit tool that runs
bounded per-surface checks and returns factual item statuses instead of inventing provider-call
limits. Untrusted text is data.

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
Telegram carries declared MIME and provider file metadata through download, derives an
extension-bearing transcription filename, and permits a generic CDN content type only when the
Telegram update or provider path independently proves an allowed audio format. Audio documents
enter the same path; ordinary documents never do.

## Memory and knowledge

Reuse `app/brain/` (ADR-026). Postgres tables `brain_*`. Embeddings are base64 float32 in TEXT; similarity is Python cosine, not pgvector.

Semantic memory/extraction/embeddings **do not run** unless model ids are configured (`MIA_OWNER_AGENT_MODEL`, `MIA_EXTRACTION_MODEL`, `MIA_EMBEDDING_MODEL`). Empty defaults fail closed. Knowledge ingest is `uv run mia-ingest-knowledge` (CLI, not EventBridge). Website visitors cannot write owner memory.

## Capability and policy

Graphs call named capabilities (`mail.read`, `calendar.get_schedule`, `leads.get_recent`,
`memory.search`, `knowledge.search`, `research.search`, `linkedin.get_profile`,
`search_console.query`, `analytics.get_traffic`, `sheets.read`, `sheets.update`, …), never raw
Composio tool slugs. Live owner calendar, hot-lead lists, mail read, LinkedIn profile, Search
Console, GA4 traffic, authorized Sheets access, memory search, knowledge search, and research
search go through `execute_capability`.

Each capability has `{READ, WRITE, SENSITIVE_WRITE, DESTRUCTIVE}`, allowed graphs, confirmation,
retry, and idempotency. Enforcement is Python (`app/core/risk.py` plus the capability registry)
using the channel-minted `Principal`; prompt text cannot add a tool. Sheets reads are `READ`.
Bounded value update/append outside Mia's CRM workspace is low-risk and requires an explicit
authenticated owner request plus an Assaf-configured/allowlisted spreadsheet ID. For the exact
`MIA_SHEETS_SPREADSHEET_ID`, the Sheets adapter may idempotently add the fixed CRM tabs, repair
their header rows, and upsert canonical projections without per-cell approval. It cannot discover
Drive files, create or delete a spreadsheet, clear business rows, generate formulas, or grant
client access. Structural repair runs only in the `mia-sheets-maintain` background/one-off worker
(and as best-effort maintenance after `mia-due-scan`), never inside a website or owner request.
For `sheets.read`, the same ID may arrive inside an exact HTTPS Google Sheets URL; it is extracted
locally and still checked against the allowlist. A missing read range becomes only `A1:J20` on the
first visible tab. Writes never receive a default target or range.

Client allowlist is dramatically narrower than owner. Website visitors never inherit the owner Composio session.

Existing risk map: R0 read AUTO, R1 low write AUTO, R2 customer message AUTO in approved scope, R3 commercial APPROVAL, R4 marketing APPROVAL, R5 destructive DENY. Kill switch denies before side effects. Website chat and voice both return 503 when it is on.

## Composio

Tool supplier behind adapters. Stable owner identity → one Composio user/session
(`MIA_COMPOSIO_USER_ID`), not a new session per message. The owner registry exposes only
small meta-tools: list/search only ACTIVE connected toolkits, fetch the selected tool's bounded
current schema, preflight common argument constraints locally, and execute conservatively
recognized deterministic reads. Catalog/tool/schema caches are bounded to the process; schemas
are supplied after selection, never dumped into every prompt. Unfamiliar actions and oversized
schemas fail closed.
Slug-based Python risk classification denies destructive actions and refuses every side effect or
unknown action until it has a named capability, approval, idempotency, and audit contract.

LinkedIn now has such a contract for exact non-destructive side effects: the proposed slug and
arguments are schema-checked and hash-bound to a Telegram approval, then the active tool, schema,
risk, expiry, kill switch, and idempotency are rechecked before execution. LinkedIn delete/remove,
revoke, message, DM, and InMail tools remain denied. Gmail send is not silent: cron, website
visitors, and catalog auto-execute cannot send. Owner Telegram may draft and, after Approve,
send via `GMAIL_SEND_DRAFT`. Calendar create/move uses its own typed approval contract.
Gmail delete-forever slugs and `GOOGLE_SEARCH_CONSOLE_DELETE_SITE` stay denied.

Direct APIs remain where they win (Meta inbound HMAC and STT). Instagram stays analytics-only;
when a provider rejects a mixed insight metric request, the adapter retries the metrics
individually and reports exactly which fields are unavailable.

## Persistence

Postgres is the system of record. Mia owns the layout and continuous operational projections in
the exact configured CRM spreadsheet, including `10 Mia Activity`; the adapter creates missing
fixed tabs and repairs headers in the background, using a schema-version marker to avoid repeat
writes. Request-path upserts never wait for structural repair. Other authorized Sheets receive policy-controlled
reads and bounded value update/appends only. No Sheet is read back as Mia's internal truth,
decision input, or recovery source. Do not add another database.

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
`GET /config` may expose only the server-configured `https://wa.me` URL. After session creation the
existing composer WhatsApp action is visible when that URL is valid; its click opens immediately
and independently posts the handoff notification. The owner card is a summary (workflow, stage,
next action, WhatsApp offered), not a visitor-text dump. The claim key is conversation/session
scoped: the graph and click dedupe within one session, but an old lead-level claim cannot suppress
a new conversation. Delivery logs contain only the outcome class, never visitor text. Never derive
a destination from model or visitor text.

## Runtime

ECS Fargate + RDS + Secrets Manager `mia/prod` + ALB `https://mia.assafweb.com` in **eu-north-1**. Do not copy `.env` onto Fargate. Do not auto-deploy from a rebuild.

## Current wiring

Short map: `docs/WIRING.md`.

- Website: `app/api/website.py` → `channels/website.py` → ClientGraph (`load_conversation` → `retrieve_knowledge` → `sales_turn` or skip on end/inactivity → `complete_turn`). Inner sales NBA is still `build_graph` in `app/graph/orchestrator.py` (REUSE until that node is inlined). Widget close, inactivity (`mia-due-scan`), and handoff finalize inside `complete_turn`.
- Telegram: `app/api/telegram.py` → `process_owner_texts` (`app/api/owner.py`) → `run_owner_turn` builds state via `channels/telegram.py` → OwnerGraph (`load_owner_context` → `retrieve_owner_knowledge` → `respond`). Owner mail/calendar/leads/research stay allowlisted tools behind `respond`, not extra nodes.
- Prospect Meta/Gmail: `app/api/inbound.py` → ClientGraph (same NBA, not a third graph). WhatsApp outbound stays human-gated (ADR-024). Mixed tests that pass owner ids still delegate to `process_owner_item`.
