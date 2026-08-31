# Mia wiring map

Who calls whom. Not a second bible. Living specs stay `PRODUCT.md` / `ARCHITECTURE.md` / `DECISIONS.md`.

Generated against master `39d1ef3` plus this file-tree PR. Inventory of `app/`, `tests/`, `scripts/`, `deploy/`, `docs/`.

## Runtime (the only paths that matter)

```text
Website visitor
  Ask Mia widget  app/web/ask_mia.js
       │
       ▼
  HTTP            app/api/website.py
       │          origin-bind + rate limit (fail closed)
       │          kill switch → 503 on chat and voice
       ▼
  channel         app/channels/website.py
       ▼
  ClientGraph     app/agents/client/graph.py
       │          retrieve → sales_turn or skip → complete_turn
       ▼
  sales NBA       app/graph/orchestrator.py   (strangler; still the inner turn)
       ▼
  store           app/db/store.py  Postgres SoR
       │
       ├─ HANDOFF ──► app/services/notifications.py
       │                 Telegram send must be ok:true
       │                 before visitor copy claims a transfer
       └─ widget close / inactivity (mia-due-scan) / meeting
            → app/services/finalization.py → owner Telegram ping

Assaf (numeric Telegram id only)
  webhook         app/api/telegram.py
       ▼
  owner turn      app/api/owner.py  process_owner_texts
       ▼
  channel state   app/channels/telegram.py
       ▼
  OwnerGraph      app/agents/owner/graph.py
       │          retrieve → respond
       ▼
  owner agent     app/graph/owner_agent.py   (tools behind respond)
       ▼
  capabilities    app/capabilities/*  → policy → adapters
                  mail.read, calendar.get_schedule, leads.get_recent,
                  memory.search, knowledge.search, research.search,
                  linkedin.get_profile, search_console.query, analytics.get_traffic,
                  sheets.read, sheets.update, sheets.append

WhatsApp (human inbox until Cloud API inbound)
  HMAC inbound    app/api/whatsapp.py → app/api/inbound.py → ClientGraph
                  persist NBA only; Mia does not reply
  website click   briefing to Telegram; visitor send gated
                  (MIA_WHATSAPP_HANDOFF_SEND)

Gmail ingest      app/api/composio.py → inbound ClientGraph (prospect)
                  owner mail via capability/tools, not this webhook

Instagram         app/api/instagram.py → inbound persist; not a sales inbox
```

Store is `LeadStore` + `BrainStore`. Graphs never hold SDK clients or secrets.

## Top-level modules

| Path | What it is for | Load-bearing? |
| --- | --- | --- |
| `app/main.py` | FastAPI app, `/health`, mounts routers | runtime |
| `app/api/` | HTTP ingress: website, telegram, whatsapp, instagram, composio, demo | runtime |
| `app/api/owner.py` | Shared owner turn (Telegram + mixed inbound tests) | runtime |
| `app/channels/` | Transport → graph state. Website used by API; Telegram used by `run_owner_turn` | runtime |
| `app/agents/` | OwnerGraph + ClientGraph (VNext entry) | runtime |
| `app/graph/` | Inner sales orchestrator, owner agent loop, canned replies | runtime (strangler) |
| `app/capabilities/` | Named tools + Python policy. Graphs must not see Composio slugs | runtime |
| `app/core/capabilities.py` | `/health` inventory map (different job than `app/capabilities/`) | runtime |
| `app/core/` | config, risk, origin-bind helper, kill switch, logging | runtime |
| `app/brain/` | memory, knowledge, embeddings, retrieval | runtime, fail-closed without model ids |
| `app/domain/` | sales NBA, owner tasks, approvals, handoff, identity | runtime |
| `app/integrations/` | Typed ports (Composio / Meta / STT / Telegram / …) | runtime |
| `app/db/` | SQLAlchemy + LeadStore | runtime |
| `app/services/` | finalization ping, owner notify, conversation facts | runtime |
| `app/tools/registries/` | Owner-agent allowlist; bounded Sheets updates/appends remain policy-controlled | runtime |
| `app/web/` | Ask Mia widget + marks | runtime |
| `app/workers/` | CLI entry points (`pyproject` scripts): due-scan, reconcile, migrate, ingest, telegram webhook | runtime / ops |
| `app/evals/` | Local Graph Lab datasets + harness | CI + `scripts/eval_diff.py` |
| `tests/` | Unit + in-process e2e. A capability is alive only if a test proves it | CI |
| `scripts/assert_origin_bind.py` | Fail-closed origin-bind gate | CI + deploy |
| `scripts/deploy_ecs_revision.py` / `run_ecs_migration.py` | Operator cut. Not auto | ops |
| `scripts/probe_*.py` | Live/local probes. Not CI | ops leftover-but-useful |
| `deploy/` | Dockerfile + ECS/ALB example JSON | deploy |
| `docs/` living | PRODUCT, ARCHITECTURE, DECISIONS, this file, RUNBOOK, PRODUCTION_BUILD, BRAIN_ARCHITECTURE, BUILD_STATUS | docs |
| `docs/archive/` | Dated slice reports and old maps. Do not load unless researching | leftover |

## Duplicate wires (kept on purpose, or fixed here)

| Claim | Reality |
| --- | --- |
| Two graphs | True. ClientGraph wraps `app/graph/orchestrator.py` until that node is inlined (ADR-036 strangler). Do not delete `app/graph/`. |
| Owner agent count | Exactly one bounded owner tool loop (ADR-031/032), never a production sub-agent swarm. Its default limit is `MIA_OWNER_AGENT_MAX_STEPS=8`. |
| Two capability lists | `app/capabilities/` is the policy layer. `app/core/capabilities.py` is the `/health` status map. Different jobs. |
| `mail.read` vs `gmail_read` | Was a real break: tests called `mail_handlers`, live `gmail_read` fetched the port directly. Live read now goes through `execute_capability`. `gmail_search` / `gmail_inbox` still format on the tool path (registry has `mail.search` with no live handler). |
| Sheets / KPI owner tools | Sheets access is only to Assaf-configured/allowlisted IDs: `sheets.read` is READ; `sheets.update` and `sheets.append` are bounded low-risk literal-value operations after an explicit authenticated owner request. Principal, policy, kill switch, and idempotency apply; no Drive discovery or Sheets read-back. GSC/GA4 use APIs and normalize AssafWeb traffic/users/sessions/conversions/pages plus clicks/impressions/CTR/position/queries. LinkedIn active reads are available; a schema-validated, exact post/comment/upload side effect is hash-bound to an expiring one-tap Telegram approval, while DM and destructive tools stay denied. Local focused tests prove these paths; no live provider response is recorded. |
| Cursor Composio vs Mia | Cursor plugin OAuth is a different Composio user unless `MIA_COMPOSIO_USER_ID` is that same UUID. `/health` `owner_integrations` is leftover config, not a live Composio ping. |
| Telegram voice | Local production-shaped tests cover numeric-owner webhook → media download → shared transcription port → OwnerGraph → escaped HTML reply. Source-derived readiness names are recorded, but no live bot voice note or provider call is proven. |
| `task_classes.py` | Lookup catalog, not a live router. Tests + `/health` mark it ALIVE meaning “the table exists.” |
| Website probes | `scripts/probe_website_flow.py` and `scripts/probe_live_website.py` send `Origin: https://www.assafweb.com` for every website POST, matching fail-closed widget writes. |
| Source-grep tests | Several `test_vnext_*` files lock imports by reading `.py` text. They prevent silent bypass; they do not prove behavior by themselves. Behavior tests sit beside them. |

## Do not delete

Ask Mia widget, Telegram owner, WhatsApp, origin-bind, kill switch, handoff-notify, deploy scripts, eval harness. Workers that look “unused” in the import graph are `pyproject` entry points.

## File cleanup vs production-ready

File cleanup is necessary and not sufficient.

This PR does not make Mia 100% production-ready. Still required, and **not done here**:

- Current ECS revision/service update. Do not deploy from this PR.
- Live read-only data from Sheets, GSC, GA4, or LinkedIn
- Telegram voice note end-to-end on the live bot
- Website meeting CREATE against live Calendar OAuth
- WhatsApp remains a human inbox (ADR-024); do not flip send to prove “ready”

Origin-bind fail-closed, website kill switch 503, owner Telegram fallback to the sales model, HANDOFF owner ping `ok:true` before transfer copy, Telegram voice routing, and Sheets/KPI policy behavior are proven locally. They are not a live cut.

## Current working-tree file counts

Measured 2026-08-28 with `rg --files` in the current working tree; these are inventory counts,
not a deployment claim or a comparison to a historical branch.

| Tree | Count |
| --- | --- |
| `app/*.py` | 176 |
| `tests/*.py` | 147 |
| `app/` + `tests/` + `scripts/` + `deploy/` + `docs/` files | 420 |
| living `docs/*.md` (not archive) | 8 |
| `docs/archive/*.md` | 32 |

These counts do not establish that every file is live, deployed, or independently reviewed.
