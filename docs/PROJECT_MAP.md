# Mia project map

Read this first. Then `docs/ARCHITECTURE.md` and `AGENTS.md`. Do not load `docs/archive/`.

## What Mia does (v1)

AssafWeb’s production AI Growth & Sales Operator.

- **Customers** talk on the AssafWeb **website** (Ask Mia widget). Progressive sales discovery. Website may offer WhatsApp continuation after real buying context; Assaf takes that chat himself until official Cloud API inbound (ADR-024).
- Assaf talks on **Telegram** (numeric allowlist). Voice in, text out. Owner console: briefs, approvals, takeover, calendar, analytics, operator snapshot on unmatched requests. Receives a briefing when a site visitor clicks WhatsApp. Replies are paraphrased over typed Python results (`owner_telegram_v2`); the model does not pick tools.
- **WhatsApp** is gated. Mia does not reply. Click-to-chat is Assaf's human inbox.
- **Gmail** is read / summarize / draft. Send is approval-gated and off.
- Brain is **LangGraph** + deterministic sales rules. **Postgres** is the system of record. Sheets is a mirror.

## Active integrations

| Job | Adapter |
| --- | --- |
| Website chat | FastAPI `app/api/website.py` + widget `app/web/ask_mia.js` |
| Telegram owner | Bot API webhook `app/api/telegram.py` |
| WhatsApp inbound | Meta Cloud HMAC `app/api/whatsapp.py` |
| WhatsApp send | Composio `WHATSAPP_SEND_MESSAGE` when `MIA_WHATSAPP_SENDER=composio` (else Graph) |
| Gmail ingest | Composio webhook `app/api/composio.py` |
| Calendar / Sheets / Meta ads read / LinkedIn profile / GSC / GA4 | Composio typed ports (ids discovered at request time) |
| LinkedIn member analytics | Typed port kept; no Composio member tool (org share-stats only) |
| Research search | Firecrawl |
| Voice STT | OpenAI transcribe (Telegram + allowed WhatsApp + website widget) |
| Host | ECS Fargate + RDS + Secrets Manager `mia/prod` + ALB `https://mia.assafweb.com` (`eu-north-1`) |

## Disabled / deferred (keep code unless noted)

| Item | Why it stays or went |
| --- | --- |
| Instagram **sales inbox** | Not v1 (ADR-017). Webhook + insights adapters remain (ADR-015 analytics). `MIA_AUTO_REPLY_INSTAGRAM=false`. Shadow owns prospect send. |
| ManyChat | **Unmounted.** Not a v1 channel. Leftover `MIA_MANYCHAT_INGEST_TOKEN` may still exist in AWS — do not delete the secret; code ignores it. |
| Gmail send / Meta writes / follow-up send | Named flags default false. R4 approval / R5 deny. |
| Dynamic tool discovery / browser automation | Flags exist, unused. |
| `AWS_RUNTIME` capability | Host is live; `app.infra` (Lambda/SQS/WAF/AgentCore) is not. |
| Make / Apify | Not wired. |

## Important directories

| Path | Look here for |
| --- | --- |
| `app/api/` | HTTP ingress (website, Telegram, WhatsApp, Instagram, Composio) |
| `app/brain/` | Long-term memory, website knowledge, retrieval, extraction (ADR-026) |
| `app/tools/registries/` | Allowlisted owner tool registry for the agent loop |
| `app/domain/` | Sales rules, owner tasks, approvals, identity, events |
| `app/graph/` | LangGraph orchestrator, owner agent loop, canned replies |
| `app/integrations/` | Typed ports (Composio / Meta / Firecrawl / Telegram) |
| `app/db/` | SQLAlchemy models + `LeadStore` |
| `app/evals/` | Graph Lab fixture datasets (local) |
| `migrations/` | SQL for existing DBs (`uv run mia-migrate`) |
| `deploy/` | Dockerfile, ECS/ALB examples. `deploy/local/` is gitignored stamped JSON |
| `tests/` | Unit + in-process e2e. Do not weaken to pass cleanup |

## Production architecture (one screen)

```
assafweb.com  --widget-->  ALB  -->  Fargate (FastAPI + LangGraph)
Assaf Telegram            -->  same process
Meta WhatsApp HMAC        -->  same process
Composio Gmail            -->  same process
                              |
                              +--> RDS Postgres (SoR)
                              +--> Secrets Manager mia/prod
```

## Configuration (do not inspect `.env`)

Names live in `.env.example` and `app/core/config.py`. Production SECRET keys: AWS Secrets Manager `mia/prod`. ECS injects `MIA_*`. Extra JSON keys are ignored (`extra="ignore"`).

| Kind | Names |
| --- | --- |
| Active runtime | `MIA_ENV`, `MIA_KILL_SWITCH`, `MIA_AUTOMATION_MODE`, `MIA_DEMO_MODE`, `MIA_PUBLIC_BASE_URL`, `MIA_CORS_ORIGINS`, `MIA_DATABASE_URL` |
| Channel secrets | WhatsApp Meta verify/HMAC/token/phone; Telegram token/webhook/owner ids; Instagram verify/HMAC/token/account; Composio key/user/webhook |
| Sender switches (one each) | `MIA_WHATSAPP_SENDER` = `direct` \| `composio`; `MIA_INSTAGRAM_SENDER` = `direct` \| `composio` |
| Wired write | `MIA_CALENDAR_WRITE` |
| Fail-closed flags (exist, default false) | `MIA_WHATSAPP_HANDOFF_SEND`, `MIA_GMAIL_SEND`, `MIA_META_WRITE`, `MIA_AUTO_REPLY_INSTAGRAM` |
| Leftover optional IDs | `MIA_GSC_SITE_URL`, `MIA_GA4_PROPERTY_ID`, `MIA_SHEETS_SPREADSHEET_ID`, `MIA_LINKEDIN_ACCESS_TOKEN` — Composio lists these; do not add them for go-live |
| Required extra (not Composio) | `MIA_FIRECRAWL_API_KEY` for research |
| Deprecated leftover | `MIA_MANYCHAT_INGEST_TOKEN` may still be in `mia/prod` and ECS injection. **Do not delete the AWS secret.** Code does not read it. |

R4 approval and R5 deny are not env knobs.

## Where to look before changing

1. `AGENTS.md` — invariants and refuse list
2. This file + `docs/ARCHITECTURE.md`
3. `docs/DECISIONS.md` — ADRs (015 adapter map, 016 WhatsApp send split, 017 channels, 019 region, 021 docs/ManyChat)
4. `docs/PRD.md` — short living contract
5. `app/core/capabilities.py` — wiring status
6. `.env.example` — env names (never inspect `.env`)

Bible: `Mia_AI_Growth_Sales_Operator_PRD_Build_Bible_v1.1.docx`. If markdown disagrees with the docx, stop and ask Assaf.
