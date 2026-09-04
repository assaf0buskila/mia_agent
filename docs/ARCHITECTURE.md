# Mia architecture

Two simple loops, shared core, thin channels. Product: `docs/PRODUCT.md`.
Decisions: `docs/DECISIONS.md`. Operations: `docs/OPERATIONS.md`.

```text
                   MIA
                    │
          ┌─────────┴──────────┐
          │                    │
     OWNER LOOP            SITE LOOP
     Telegram              Website
     Dude talk             glass widget.js
     full Composio         few tools, product first
     text + voice          identity before ping
          │                    │
          └─────────┬──────────┘
                    │
            Shared Mia Core
            (identity, STT, brain, Contacts CRM)
                    │
                House Composio
```

## Surfaces

| Surface | Users | Must not |
| --- | --- | --- |
| Owner loop | Assaf on Telegram | Invent metrics; invent lead IDs; ask for a Sheet URL; sell to Assaf |
| Site loop | Website visitors | Write CRM, ping, or offer WhatsApp without phone or email; invent prices; run owner tools |

## Who calls whom

This is the only call map. Nothing else in `docs/` restates it.

```text
Website visitor
  widget.js       app/web/ask_mia.js
                  glass Hebrew RTL, served at /v1/website/widget.js
       │
       ▼
  HTTP            app/api/website.py
                  origin-bind, session / message / voice / handoff
       ▼
  site loop       app/surfaces/site.py         run_site_turn
                  app/surfaces/site_policy.py  deterministic next action
                  app/surfaces/site_reply.py   shared sales reply port
                  identify-then-sell; CRM + Telegram ping only after phone or email
       ▼
  Contacts CRM    app/surfaces/crm.py
                  spreadsheet 1HW8mnc9GFXraS6oG5VIxFcJvZq9gMDJBFRxY2mpVOhI
                  Contacts + Activity only

Assaf (numeric Telegram id only)
  webhook         app/api/telegram.py
                  allowlist + webhook secret, HTML, voice download, callbacks
       ▼
  owner loop      app/surfaces/owner.py        run_owner_loop / answer_owner
                  Dude talk
       ▼
  owner agent     app/graph/owner_agent.py
                  crm_search / crm_upsert / house Composio reads
       ▼
  house ports     bind_owner_house_ports(settings)
                  Sheets, Gmail, Instagram, LinkedIn, GA, GSC, Calendar

WhatsApp inbound (human inbox — ADR-024)
  webhook         Meta webhook + HMAC (ADR-016)
       ▼
  ClientGraph     app/graph/orchestrator.py + app/agents/client/graph.py
                  also the finalization path for the due-scan job
```

The LangGraph files are live, not leftovers: `app/graph/orchestrator.py` and
`app/agents/client/graph.py` are the reasoning path for WhatsApp inbound and for
due-scan finalization. The **website does not use ClientGraph** — it runs the
deterministic `site_policy` and phrases each turn through the shared sales reply port.
WhatsApp customer send stays Assaf (ADR-024). Website visitors cannot run owner tools
(ADR-041). No invented metrics.

## Channels

Channels adapt transport. They do not reason.

| Surface | Adapter job | Then |
| --- | --- | --- |
| Telegram | Webhook, numeric allowlist, HTML, voice download, callbacks | `run_owner_loop` |
| Website | Session/message/voice/handoff HTTP + glass `widget.js` | `run_site_turn` + `site_policy` |
| WhatsApp | Human inbox (ADR-024) | Assaf, after a site ping |

Telegram access: `MIA_TELEGRAM_OWNER_USER_IDS` numeric only. Webhook secret check stays.

## CRM

Locked spreadsheet `1HW8mnc9GFXraS6oG5VIxFcJvZq9gMDJBFRxY2mpVOhI`. Live tabs Contacts
and Activity only. Archive tabs are gone. Writers live in `app/surfaces/crm.py`. Owner
tools `crm_search` / `crm_upsert` always use that ID. Empty `MIA_SHEETS_SPREADSHEET_ID`
still resolves to the locked ID. The Composio archive mirrors are deleted (ADR-052).
Telegram binds house Composio from `MIA_COMPOSIO_USER_ID` — if `/health` says
connected, tools must run.

## Website UX

Glass Hebrew widget `app/web/ask_mia.js` at
`https://mia.assafweb.com/v1/website/widget.js`. Answer first. Identity before ping.
WhatsApp only after phone or email. Origin-bind stays.

## Two-state tools

Owner Telegram gets the house Composio set. Visitor site gets published facts only,
looked up directly in `app/api/website.py`; the `app/tools/registries/visitor_tools.py`
registry is written but wired to nothing. Calendar writes go through
`app/domain/meetings/write_gate.py`: meeting near Tel Aviv, 09:00–17:00 Asia/Jerusalem,
empty slot — else ask Assaf (ADR-050).

## Brain

Everything Mia knows beyond the current turn lives under `app/brain/`. **Every
retrieval constant lives there too**, in the module that uses it, not in this document.
Read the code for a number; read this for the shape.

| Layer | Where | What it holds |
| --- | --- | --- |
| Owner profile | `app/brain/context.py` | A small curated set of identity / background / communication / preference facts, always in front of the model, never retrieval-ranked |
| Long-term memory | `app/brain/store.py`, `app/brain/schemas.py` | Episodic, semantic, working and preference memories with category, importance and status (ADR-026) |
| Conversation history | `app/domain/memory.py` | A read model rebuilt from `canonical_events` message_in / message_out rows. Text only, bounded turns, no second system of record |
| Knowledge | `app/brain/knowledge.py` | Chunked published AssafWeb facts, ingested from `llms.txt`, `llms-full.txt` and `pricing.md` by `mia-ingest-knowledge` |
| Extraction | `app/brain/extraction.py` | Turns a finished exchange into memory candidates, and consolidates contradictions |

**Embeddings** (`app/brain/embeddings.py`) call OpenAI, or Gemini through the
OpenAI-compatible endpoint. Vectors are stored as base64 float32 in a portable `TEXT`
column and compared with exact cosine in Python (`app/brain/vectors.py`) — not pgvector,
so the test suite exercises the same retrieval path as production (ADR-026).

**Retrieval** (`app/brain/retrieval.py`) is two stages, both in Python so one code path
serves SQLite and PostgreSQL:

1. **Candidate generation.** An exact-cosine list and a **BM25** keyword list over the
   same rows, fused with **Reciprocal Rank Fusion** at the documented constant
   `RRF_K = 60`.
2. **Memory re-rank.** Relevance + recency + importance, each min-max normalized across
   the candidate set. Memories only — recency and poignancy are meaningless for a
   scraped web page, so knowledge chunks stop at stage 1.

**Context budget.** `assemble_owner_context` in `app/brain/context.py` is the single
entry point. It caps the always-on profile as a fraction of the total character budget
so retrieval for the current question always has room, splits the rest between knowledge
and memory (memory wins ties, because it is owner-specific and knowledge is public),
deduplicates, and fits the result to budget.

**Provenance.** Every retrieved line is rendered with its source
(`RetrievedItem.provenance`), so the model can be told which facts it may state.
Memories are retired rather than deleted, so anything already cited keeps its source.

**Degradation is three steps and never an error:** embeddings present → hybrid semantic
+ keyword; embeddings absent → keyword only; brain disabled or empty → an empty context
that callers treat as "no extra knowledge". `MIA_OWNER_AGENT_MODEL` is the on switch for
the owner tool loop and ships empty.

**Memory is not current state.** Memory is the useful past. Current state is a separate
durable store per surface — `lead_sales_state` for a sales lead, `website_session_state`
for a website session. Mia does not rebuild "how many times have I asked for a phone
number" by scanning a transcript; she reads a stored field. Retrieved memory ranks and
informs; it never decides what is true now.

Untrusted text stays data. A visitor turn, a scraped page or an email body can be
retrieved and shown to the model, and can never select a tool or change a prompt.

## Runtime

ECS Fargate + RDS + Secrets Manager `mia/prod` + ALB `https://mia.assafweb.com` in
**eu-north-1** (ADR-014, ADR-019). Do not copy `.env` onto Fargate. Do not auto-deploy
from a rebuild. Operator steps: `docs/OPERATIONS.md`.
