# Capability ownership matrix

**Date:** 2026-08-22  
**Status:** living Phase 1 doc (Adjustment A). Not application config.  
**Runtime source of truth for wiring:** `app/core/capabilities.py`  
**Adapter inventory:** `docs/PROVIDER_MATRIX.md`  
**Operator runbook:** `docs/RUNBOOK.md`  
**Gap labels:** `docs/PRE_PRODUCTION_GAP_REPORT.md`

This file names **one execution owner** per capability as the code works today. It does not change owners. Rows that disagree with `MIA_PRE_PRODUCTION_ARCHITECTURE_ADJUSTMENTS.md` are marked **intent vs code** so Assaf can choose KEEP or ADOPT without silent drift.

## Rules

- Postgres is the system of record. Google Sheets is a mirror.
- LangGraph owns sales reasoning. ManyChat, Make, and n8n do not.
- Composio supplies tools behind typed adapters for **ADR-015 jobs**. It does not own business state.
- Graph state is serializable domain data only (`app/graph/state.py`).
- One Instagram sender per conversation (`MIA_INSTAGRAM_SENDER`). Never dual-send ManyChat + Graph + Composio.
- R4 Meta writes stay approval-gated. R5 stays deny. Those are not env knobs.

## FDE execution policy

| Capability | System of record | Execution owner | Backup or fallback | Must not own |
| --- | --- | --- | --- | --- |
| Execution policy registry | Code registry in Postgres-free lookup | `app/domain/policies/execution_policy.py` | Unknown capability → `HUMAN_ONLY` + R5 | Sales NBA (`select_next_action`); graph routing; write gates (`assert_allowed` stays authoritative) |
| Decision route lookup | Code pure functions | `app/domain/policies/decision.py` (`route_decision` / `risk_gate`) | `requires_human` / confidence floor / approval flags | MessagePort send; `assert_allowed` (write gate stays in `risk.py`) |
| FDE shadow mode | Postgres `shadow_decisions` | `app/domain/shadow.py` + prospect inbound skip in `app/api/inbound.py` | Owner acks still send; tests default `auto_approved` | Website HTTP reply gate; calendar write; follow-up send; HYBRID routing |
| FDE owner correction | Postgres `owner_corrections` | `app/domain/feedback.py` + owner PREFERENCE inbound wire in `app/api/inbound.py` | Propose-only `owner_instructions` still runs | Instruction activation; prompt rewrite; remember-ask Hebrew UX |
| FDE business value | Postgres `canonical_events` (`business_value`) | `app/domain/value.py` (kinds qualified/booked/recovered/handoff; R1 persist; count helper) | Existing timeline events (`HANDOFF`, `MEETING_BOOKED`, `FOLLOW_UP`, `QUALIFICATION_UPDATED`) | Weekly KPI `COUNTABLE_EVENT_TYPES`; deal ILS inference; minutes_saved |
| FDE node failure policy | Code registry in Postgres-free lookup | `app/domain/policies/failure_policy.py` (`failure_policy_for`; pins match ad-hoc adapter fail-closed) | Unknown node → fail_closed + omit; `ToolOutcome` statuses unchanged | Adapter retry loops; graph routing; owner notify on tool fail |
| FDE human takeover | Postgres `leads.human_takeover` | `app/domain/takeover.py` + owner inbound apply + prospect skip in `app/core/outbound.py` | Graph + `ai_runs` still run; owner acks still send | `conversation_killed`; auto-resume; website HTTP gate; follow-up send |
| FDE idempotency | Postgres `idempotency_records` + existing webhook/canonical unique keys | `app/domain/idempotency.py` (`IdempotencyStore` Protocol); `LeadStore.claim_webhook` / `claim_operation` | First-write canonical rows; webhook envelope `channel`+`envelope_kind` first-write-wins; calendar private booking key; stale `received` reclaim (300s); Sheets `{inbound_id}:sheets:{tab}` (not lead); follow-up `{inbound_id}:followup` (not lead); cancellation `{inbound_id}:cancellation` (not lead); persist-path suite `tests/unit/test_idempotency_persist_paths.py` | Powertools decorator; SQS redelivery; raw webhook body |
| Named write flags | Env / `app/core/config.py` | `app/core/write_flags.py` (`named_write_may_auto`, `write_flag_enabled`) | All default false; tests set `MIA_CALENDAR_WRITE=true` | R4/R5 override; kill switch; graph routing |
| Preloaded tool pins | Code registry (no Postgres) | `app/tools/registries/mia_preloaded_tools.py` (`PRELOADED_TOOLS`, `preloaded_tool`) | Adapter module constants remain execution source | Composio catalog discovery; customer-graph tool binding; Drive/Gmail send/Meta write pins |
| Freshness policy | Code registry (no Postgres) | `app/domain/policies/freshness.py` (`freshness_pin`, `stamp_freshness`, `overlay_stale`) | Unknown fact → fail-closed `live_only`/`none`; versioned knowledge → `unverified`; wired stamps on `tool_runs.freshness`: `campaign_metrics`, `calendar_availability`, `gmail_results`, `opt_out_status`, `campaign_budget_status`, `conversation_ownership`, `owner_permissions`, `lead_recent_messages`, `website_session_events`, `instagram_content_metrics`, `linkedin_content_metrics`, `linkedin_profile`, `research_snippets`, `gsc_search_metrics`, `ga4_traffic_metrics`, `seo_audit_snapshot` | Cache layer; RAG; GraphState stamps; customer ack text |
| Ingress correlation | Postgres `canonical_events.correlation_id` + `tool_runs.correlation_id` + `payload_version` | `app/domain/events.py` stamp helpers + persist stamp in `LeadStore.save_canonical_event` / `persist_tool_outcome`; ingress wire in `app/api/inbound.py`, `app/api/website.py`, `app/api/composio.py`, graph `_persist_canonical_event` | Prospect/website reuse `ai_runs.run_id`; owner `cor_*` per item; IG ownership + Gmail `gmail_fetch` share inbound id; due-scan and website session-create sheets_mirror empty; `payload_version` allowlist `"1"` | SQS envelope; parallel `InboundEvent` type; correlation or version in payload; `business_id` tenant; `webhook_events.correlation_id` |

## Required control-file rows

| Capability | System of record | Execution owner | Backup or fallback | Must not own | Intent vs code |
| --- | --- | --- | --- | --- | --- |
| Instagram entry trigger | Meta webhook and/or ManyChat External Request | `MIA_INSTAGRAM_SENDER=direct` → `app/api/instagram.py` + `app/integrations/instagram.py`. ManyChat ingest → `app/api/manychat.py` | Manual Instagram inbox | LangGraph must not poll Instagram | **Intent vs code:** adjustment wants ManyChat as default entry. Code default is direct Graph. Operator Meta routing is external. |
| Instagram conversation routing | Meta conversation routing (operator) + `MIA_INSTAGRAM_SENDER` | Config chooses **one** sender: `direct` (Graph send), `manychat` (Dynamic Block v2, Graph send off), or `composio` (`INSTAGRAM_SEND_TEXT_MESSAGE`). Default remains `direct`. | Human inbox. Owner human takeover + resume phrases on `leads.human_takeover` | Two apps replying together | **Partial.** Dual-send is a code/policy rule, not a Meta API proof. Takeover skips prospect MessagePort; resume is explicit owner phrase only. Do not flip production env to `composio` until staging send is tested. |
| Sales reasoning | Postgres `lead_sales_state` | LangGraph `app/graph/orchestrator.py` + `app/domain/sales.py` | Canned `reply_for` / `CannedSalesReplyPort`; graph `handoff` | ManyChat, Make, n8n, Base44 | Matches adjustment. |
| Lead state | Postgres `leads`, identities, sales, events | Domain `app/db/store.py` / `app/domain/*` | Canonical event log (replay not automated) | Google Sheets | Matches adjustment. |
| Calendar truth | Google Calendar (provider) + Postgres `meetings` | Read: `CalendarPort` (`app/integrations/calendar.py`). Write/readback: `CalendarBookingPort` (`app/integrations/calendar_booking.py`). Local cancel request: `app/domain/meeting_changes.py`. Named flag `MIA_CALENDAR_WRITE` gates create/PATCH only (`app/core/write_flags.py`) | Offered slots in Postgres when provider unavailable; kill switch static copy | Model memory; Sheets tab 04 as truth | `calendar_availability` freshness stamp on offer + owner read → `tool_runs.freshness` audit (ack unchanged). Create/reschedule PATCH gated by `MIA_CALENDAR_WRITE` (default false); live OAuth still required. Provider delete denied (ADR-013). Booking HTTP ≥400 / transport raises `AdapterHttpError`; domain classifies and retries (create/PATCH still verify). |
| Campaign truth | Meta Ads account | `MetaAdsPort` (`app/integrations/meta_ads.py`) on owner analytics ack | Omit missing metrics; never zero-fill; Postgres `campaign_recommendations` is analysis snapshot, not Meta | Google Sheet | `campaign_metrics` freshness stamp wired on enrich ack; persisted on `tool_runs.freshness` (audit only; ack unchanged). |
| Campaign write | Meta Ads | **None in code.** R4 `app/core/risk.py` | Manual Ads Manager | Free-form Composio discovery; Sheets | Intent: controlled direct adapter later. Not built. |
| Tool authentication | Env after ECS inject from Secrets Manager `mia/prod` (never git, logs, traces, prompts, GraphState). Laptop `.env` only. | **Auth-owner matrix below.** One execution adapter per job (ADR-015 map; ADR-007 rubric). Shared vendor keys (Composio, OpenAI, Meta IG) are credential pools, not extra execution owners. | Disabled/fake port when that job’s credentials are empty; provider reconnect is operator | Multiple IG senders; catalog dump into the model; a second adapter silently using another job’s token | Matrix is this file. No `app/auth` service. Production box is ADR-014 SM; app still reads `Settings()` from env. |
| Owner instructions | Postgres `owner_instructions` | `app/domain/learning.py` (status `proposed` only; kinds `preference` / `behavior_rule` / `correction` classified on propose) | Instruction disabled / unused in prompts | System prompt append of raw owner text | Activation gated; `fact` not this slice. |
| Owner corrections | Postgres `owner_corrections` | `app/domain/feedback.py` (status `logged` only; scope `this_turn`/`remember`; R1 persist on CORRECTION kind inbound) | Propose-only instruction row still written | Activation; prompt rewrite; remember-ask UX this slice |
| Business report view | Postgres-derived | `SheetsPort` (`app/integrations/sheets.py`) tabs 01–10 | Owner WhatsApp scorecards (`owner_briefs` / `owner_weeklies`) | Sheet as CRM | Matches adjustment. |
| Runtime | First live host ADR-014 (Fargate + RDS + SM). AgentCore/Lambda-graph not chosen. | FastAPI `app.main` on Fargate in production; laptop uvicorn for dev. `AWS_RUNTIME` **specified**, `app.infra` missing | New ECS task revision; `docs/RUNBOOK.md` §9 | Duplicate workers / two brains | Production process owner: ECS service `mia`. Do not mark `AWS_RUNTIME` alive until ALB+RDS run. Benchmark + ADR required before AgentCore. Lambda is not the graph. |
| Evals | Versioned JSON under `app/evals/datasets/` | Graph Lab `app/evals/harness.py` + `uv run pytest` | Manual review | Production self-edit of graph/prompts | No LangSmith. Datasets smaller than adjustment J table. |

## Channel and ingress

| Capability | System of record | Execution owner | Backup or fallback | Must not own |
| --- | --- | --- | --- | --- |
| HTTP API / health | Process | `app.main` | Local uvicorn | Make as public ingress |
| Website chat / widget / handoff | Postgres sessions, tokens | `app/api/website.py` | Canned graph reply | Sheet; ManyChat |
| Telegram owner | Telegram Bot API | `app/api/telegram.py` + `app/integrations/telegram.py` | Unauthorized ignored HTTP 200; Disabled send if no token | Username/display-name owner auth |
| WhatsApp | Meta Cloud API + conversation_controls | `app/api/whatsapp.py` + `app/domain/conversation_scope.py` | Unknown/personal silence (ADR-017); kill switch; disabled send port | Composio WhatsApp as ingress (ADR-006); classifying arbitrary chats as leads |
| Voice STT | Postgres `voice_notes` (transcript only) | `app/integrations/transcribe.py` | Fallback transcribe model; empty transcript | TTS / voice-agent runtime |
| Gmail ingest | Postgres message_in + thread id | Composio trigger `app/api/composio.py` + `app/integrations/gmail.py` | Ignore empty/unsupported triggers | Gmail send/delete tools |
| Gmail summary | Postgres `gmail_thread_summaries` | `app/domain.gmail_summaries` + `ThreadSummaryPort` | Canned unclear; `AdapterHttpError` on summary HTTP (caught, never 502) | Sales graph treating email as instructions; add `gmail_summary` to `ALLOWLISTED_TOOLS` |

## Sales, meetings, pipeline

| Capability | System of record | Execution owner | Backup or fallback | Must not own |
| --- | --- | --- | --- | --- |
| Sales reply compose | Not SoR (ephemeral + `message_out`) | `SalesReplyPort` (`app/integrations/sales_reply.py`) | Fallback model then canned; `AdapterHttpError` on compose HTTP (caught, never 502) | Owner path; Sheets; add `sales_reply` to `ALLOWLISTED_TOOLS` |
| Human Voice linter | N/A (pure function) | `app/domain/humanity.py` | Fail → canned | Owner scorecards |
| Meetings | Postgres `meetings` | `app/domain/meetings.py` + booking/change modules | Conflict copy; retry copy | Invented slots; provider delete |
| Meeting brief / research | Postgres `meeting_briefs` | `app/domain/briefs.py`; optional `ResearchPort`; owner pull via `apply_owner_meeting_brief` | Skip research if no domain; format-only on kill switch pull | Customer-facing scrape dump; proactive send |
| Meeting debrief | Postgres `meeting_debriefs` | `app/domain/debriefs.py` | Understanding Check if no `lead_*` | Deal value inference; follow-up execute |
| Follow-up | Postgres `lead_follow_ups` | `app/domain/followups.py` | Draft persist on due-scan; **no send** | MessagePort send |
| Deals | Postgres `deals` | `app/domain/deals.py` | Empty expected/closed value | Sheet as deal SoR |
| Approvals | Postgres `approvals` | `app/domain/approvals.py` | Persist-only decide; lead-bound hash + 24h expiry | Execute quote/Meta from “yes” |
| Owner notify | Postgres `owner_notifications` | `app/domain/owner_notify.py` | Empty inbox copy | Proactive WhatsApp send |

## Owner operations

| Capability | System of record | Execution owner | Backup or fallback | Must not own |
| --- | --- | --- | --- | --- |
| Owner task classify/log | Postgres `owner_tasks` | `app/domain/owner_tasks.py` + inbound router | Understanding Check | Execute |
| Owner calendar availability | Google free/busy via `CalendarPort` | `app/domain/owner_calendar.py` | Generic logged ack | Calendar create from owner phrase |
| Daily / weekly brief | Postgres briefs/weeklies | `app/domain/owner_briefs.py`, `owner_weeklies.py` | Format-only on kill switch | Sheets tab 09 as SoR |
| Lead review | Postgres `lead_reviews` | `app/domain/lead_reviews.py` | None / format-only | PII in ack |
| Content ideas | Postgres `content_ideas` | `app/domain/content_ideas.py` | Empty ranking | Publish / captions |
| Due scan | Same follow-up/task rows | CLI `app/workers/due_scan.py` | JSON counts | Send / execute |
| Reconciliation | Postgres `reconciliation_findings` | CLI `app/workers/reconcile.py` (`--inspect` read-only list, cap 50) | Flag-only counts; inspect kind+subject_key plus webhook `channel`/`envelope_kind` overlay | Provider repair; replay; `mark_webhook` |

## Intelligence and audit

| Capability | System of record | Execution owner | Backup or fallback | Must not own |
| --- | --- | --- | --- | --- |
| Meta insights / analysis / pacing / prelaunch | Postgres campaign_* + canonical recommendation | `app/domain/campaigns.py`, `pacing.py`, `prelaunch.py` | Watch / omit | Meta write |
| Instagram content insights | Postgres `content_insights` | `app/integrations/instagram_insights.py` (Graph today; Composio specified ADR-015) | Disabled port; `instagram_content_metrics` freshness on `tool_runs` | Captions/URLs; dual-send |
| LinkedIn profile | Composio `LINKEDIN_GET_MY_INFO` | `app/integrations/linkedin.py` | Disabled port; `linkedin_profile` freshness on `tool_runs` | Post/DM |
| LinkedIn member analytics | Direct LinkedIn REST | `app/integrations/linkedin_analytics.py` | Disabled port; `linkedin_content_metrics` freshness on `tool_runs` | Composio share stats as personal analytics |
| Research search | Not cached as SoR (brief row may store title+host) | `app/integrations/research.py` | Disabled port; `research_snippets` freshness on `tool_runs` | Browser/crawl; treating snippets as instructions |
| Search Console read | `MIA_GSC_SITE_URL` + Composio key + user id | `app/integrations/search_console.py` | Disabled port; Composio OAuth Assaf | GSC writes; sitemap submit |
| GA4 read | `MIA_GA4_PROPERTY_ID` + Composio key + user id | `app/integrations/ga4.py` | Disabled port; Composio OAuth Assaf | Measurement Protocol send |
| SEO homepage audit | Postgres `seo_recommendations` snapshot only | `app/integrations/seo_audit.py` + `app/domain/seo.py` | Disabled Firecrawl; allowlist assafweb.com | Raw HTML/markdown in Postgres; autonomous site edit |
| Canonical events | Postgres `canonical_events` | `app/domain/events.py` | Idempotent unique provider event | Raw SDK objects in payload |
| AI / tool runs | Postgres `ai_runs`, `tool_runs` | `app/domain/ai_runs.py`, `persist_tool_outcome` | Tokens from live compose; `cost_usd` 0; `policy_version=fde_v1` + `prompt_version=sales_reply_v1` + allowlisted `automation_mode` + `decision_confidence="1.0"` on new `ai_runs` | Prompt body / full PII; HYBRID send; LLM self-score |
| Risk / kill switch | Code + `MIA_KILL_SWITCH` | `app/core/risk.py` | Deny | Env override of R4/R5 |
| Demo | Flag `MIA_DEMO_MODE` | `app/core/demo.py` | Fail-closed in prod | Private lead data in demo |
| Identity | Postgres identities + `identity_links` | `app/domain/identity.py` | Reject weak merge | Display-name authorization; unmerge (R5) |

## Auth-owner matrix (Adjustment A / L)

Not application config. Records **who authenticates whom** as the code works today. Empty credential → that job’s Disabled port (or inbound-without-send). Named write flags (`MIA_CALENDAR_WRITE`, etc.) enable a path; they do **not** authenticate and they do **not** override R4/R5 or the kill switch.

There is no `business_id` tenant and no service-account actor type. `approvals.business_id` / `actor_id` stay reserved empty.

### Actor identity (who may command)

| Actor | How verified | Role | May write (subject to `assert_allowed`) | Must not |
| --- | --- | --- | --- | --- |
| Public lead | Channel identity: website session, WhatsApp/IG `from`, Gmail sender email | Prospect | R0–R2 in approved scope (sales reply, calendar create/PATCH when flag on) | Owner tasks; Meta writes; instruction activation |
| Owner | Exact `MIA_WHATSAPP_OWNER_PHONES` set **or** Telegram numeric `MIA_TELEGRAM_OWNER_USER_IDS`; `item["from"] in owner_ids` in `app/api/inbound.py` | Owner | R0 reads + R1 persist (tasks, briefs, approvals persist, takeover flag). Never inferred from name/text/style | Spoof via “I am Assaf”, forwarded text, Gmail display-name, Telegram username (`tests/unit/test_adversarial_identity.py`, `tests/unit/test_comm_operating_model.py`) |
| Website browser | Origin/CORS (`MIA_CORS_ORIGINS`); no HMAC | Prospect session | Session/message/events/handoff HTTP | Owner classify; webhook claim |
| Operator / service account | **Not modeled** | — | — | Treating Composio or Meta app as an owner phone |

### Ingress verification (request is from the provider)

| Ingress | Secret | Verify | Owner module | Fallback | Must not |
| --- | --- | --- | --- | --- | --- |
| Telegram owner webhook | `MIA_TELEGRAM_WEBHOOK_SECRET` + `MIA_TELEGRAM_OWNER_USER_IDS` | Timing-safe secret header + numeric allowlist (`app/core/webhooks.py` `verify_telegram_secret`) | `app/api/telegram.py` | Unauthorized → 200 ignored; secret mismatch → 401 | Username / “I am Assaf” owner |
| WhatsApp webhook | `MIA_WHATSAPP_APP_SECRET` + `MIA_WHATSAPP_VERIFY_TOKEN` | Meta HMAC (`app/core/webhooks.py`) | `app/api/whatsapp.py` | Reject unsigned POST | Composio WhatsApp as ingress |
| Instagram Graph webhook | `MIA_INSTAGRAM_APP_SECRET` + `MIA_INSTAGRAM_VERIFY_TOKEN` | Meta HMAC | `app/api/instagram.py` | Reject unsigned POST | Dual Graph + ManyChat send |
| ManyChat External Request | `MIA_MANYCHAT_INGEST_TOKEN` | Timing-safe Bearer (no request HMAC — ManyChat does not document one) | `app/api/manychat.py` / `app/integrations/manychat.py` | 401 | Composio `MANY_CHAT` toolkit |
| Composio Gmail trigger | `MIA_COMPOSIO_WEBHOOK_SECRET` | Composio HMAC | `app/api/composio.py` | Reject unsigned | Execute Gmail send/delete from trigger |
| Website session API | `MIA_CORS_ORIGINS` | Origin allowlist | `app/api/website.py` | CORS reject | Treating widget POST as owner |

### Tool credentials (one execution owner per job)

Shared vendor keys are listed once, then each **job** names the adapter that may use them.

| Job | Credentials (env) | Execution owner | Empty / reconnect fallback | Must not own |
| --- | --- | --- | --- | --- |
| Postgres SoR | `MIA_DATABASE_URL` | `app/db/session.py` + `LeadStore` | In-memory SQLite default (tests); live local file SQLite | Sheets as CRM |
| Telegram owner send + voice download | `MIA_TELEGRAM_BOT_TOKEN` | `app/integrations/telegram.py` (`TelegramPort`; allowlisted `api.telegram.org`) | Disabled send; empty transcript | Treating Telegram as a customer channel |
| WhatsApp Cloud send + media download | Send: Graph token+phone id when `MIA_WHATSAPP_SENDER=direct`; Composio key+user+phone id when sender=`composio`. Media download: Graph token (inbound STT). | `app/integrations/whatsapp.py` (`WhatsAppCloudPort` or `ComposioWhatsAppPort`) | Inbound still works; send Disabled | Dual Meta+Composio send; `WHATSAPP_SEND_TEMPLATE_MESSAGE`; catch send HTTP in `send_inbound_reply` (breaks 502 rollback) |
| Instagram DM send | `MIA_INSTAGRAM_ACCESS_TOKEN` + `MIA_INSTAGRAM_ACCOUNT_ID` when sender=`direct`; Composio key+user when sender=`composio` | `app/integrations/instagram.py` (`InstagramCloudPort` or `ComposioInstagramPort`) | Disabled send; ManyChat ingest still works | Send when sender=`manychat`; dual-send |
| Instagram organic insights | Graph token + account id when sender=`direct`; Composio key+user when sender=`composio` or Graph empty | `app/integrations/instagram_insights.py` | `DisabledInstagramInsightsPort` | Captions/URLs |
| ManyChat ingest (not send) | `MIA_MANYCHAT_INGEST_TOKEN` | `app/api/manychat.py` | Ignore / 401 | Public API send; dual-send |
| Gmail inbound + empty-body hydrate | `MIA_COMPOSIO_API_KEY` + `MIA_COMPOSIO_USER_ID` + webhook secret | `app/integrations/gmail.py` + `app/api/composio.py` | Skip hydrate; ignore unsigned | `GMAIL_SEND_*` / delete tools |
| Calendar free/busy / create / PATCH / GET | Composio key + user id; write path also `MIA_CALENDAR_WRITE` | `CalendarPort` / `CalendarBookingPort` | Disabled ports; static/canned slots | Provider delete; full PUT; attendees |
| Sheets mirror upsert | Composio key + user id + `MIA_SHEETS_SPREADSHEET_ID` | `app/integrations/sheets.py` | `DisabledSheetsPort`; HTTP/transport → `AdapterHttpError` then `mirror_*` skip | Read sheet back into SalesState |
| Meta Ads insights / pacing | Composio key + user id + `MIA_META_ADS_ACCOUNT_ID` | `app/integrations/meta_ads.py` | `DisabledMetaAdsPort`; omit missing metrics | Create/update/pause (`MIA_META_WRITE` unwired; R4 stays approval) |
| LinkedIn profile | Composio key + user id | `app/integrations/linkedin.py` | `DisabledLinkedInPort` | Posts / DM / share-stats as personal analytics |
| LinkedIn member post analytics | `MIA_LINKEDIN_ACCESS_TOKEN` (direct; `r_member_postAnalytics`) | `app/integrations/linkedin_analytics.py` | `DisabledLinkedInAnalyticsPort` | Composio org share-stats; OAuth callback this slice |
| Firecrawl public search | `MIA_FIRECRAWL_API_KEY` | `app/integrations/research.py` | `DisabledResearchPort` | Browser/crawl; snippets as instructions |
| Sales reply paraphrase | `MIA_OPENAI_API_KEY` + `MIA_SALES_MODEL` / `MIA_SALES_FALLBACK_MODEL` | `OpenAISalesReplyPort` | Canned; kill switch forces canned | Hard-coded production model id |
| Voice STT | `MIA_OPENAI_API_KEY` + `MIA_OPENAI_TRANSCRIBE_MODEL` (optional fallback) + WhatsApp media token | `app/integrations/transcribe.py` | Empty transcript; no TTS | Voice-agent runtime |
| Owner phone allowlist | `MIA_WHATSAPP_OWNER_PHONES` | `Settings.whatsapp_owner_phone_set` + inbound `owner_ids` | All WhatsApp is prospect | Display-name / “I am Assaf” authorization |

**Shared pools (not extra owners):** `MIA_COMPOSIO_API_KEY` + `MIA_COMPOSIO_USER_ID` serve Gmail, Calendar, Sheets, Meta insights, LinkedIn profile, Instagram send/insights when sender=`composio`, and WhatsApp send when `MIA_WHATSAPP_SENDER=composio`. `MIA_OPENAI_API_KEY` serves STT and sales compose. `MIA_INSTAGRAM_ACCESS_TOKEN` serves Graph webhook/send/insights until Composio IG ports land. Rotation is operator laptop `.env` or Secrets Manager `mia/prod` (ADR-014) then new Fargate deploy — one pool, many jobs; do not give a second adapter a copy of a token it does not own in this table.

**Still missing (not this file):** per-capability kill, `business_id` multi-tenant, operator vs service-account actors, Meta Conversation Routing proof, Lambda/SQS/WAF/AgentCore.

## Composio vs direct (ADR-015)

One owner per **job**. Rubric remains ADR-007.

| Job | Owner adapter | Must not |
| --- | --- | --- |
| Gmail inbound + hydrate | Composio Gmail pin | Send/delete |
| Calendar free/busy, create, patch, get | Composio Calendar pins | Full PUT; provider delete |
| Sheets upsert | Composio Sheets pin | Read sheet back into CRM |
| Meta insights | Composio Meta Ads pin | Create/update/pause |
| LinkedIn profile | Composio LinkedIn pin | Posts |
| LinkedIn personal post stats | Direct REST | Org share-stats tool |
| Telegram owner | Direct Bot API | Username owner auth; treating Telegram as a customer inbox |
| WhatsApp inbound + send | Inbound: Direct Meta Cloud. Send: Graph or Composio `WHATSAPP_SEND_MESSAGE` (ADR-016). Product: website-handoff continuation only (ADR-017) | Composio WhatsApp as ingress; dual send |
| Instagram inbound | Direct Meta webhook | Composio as ingress |
| Instagram send + insights | Graph today when sender=`direct`; Composio `20260819_00` when sender=`composio` | Dual-send with ManyChat |
| ManyChat | Direct External Request (ingest) | Composio ManyChat toolkit (deprecated); second sender |
| Research | Direct Firecrawl search | Apify env until adapter exists; Playwright this slice |

## Acceptance check (Adjustment A)

| Criterion | Today |
| --- | --- |
| Every major capability has one execution owner | Named above. First live process owner is the ECS service (ADR-014). `AWS_RUNTIME` / AgentCore still specified, not alive. |
| Tool authentication has one owner per job | Written matrix (actor + ingress + credentials). Shared Composio/OpenAI/IG keys are pools, not extra owners. Production box is Secrets Manager `mia/prod`. |
| No important state split across uncontrolled platforms | Postgres SoR. Sheets mirror. |
| No channel duplicate automated replies | Policy + sender config. Meta routing unproven. |
| Google Sheets is not SoR | Enforced in adapters and tests. |
| ManyChat does not own sales reasoning | Ingest only; sales graph never runs ManyChat as brain. |
| Composio does not own business state | Typed ports + Postgres. |
| LangGraph does not store SDK objects | `GraphState` contract. |

## Out of scope for this file

Application code, feature flags, `IdempotencyStore`, AgentCore, subgraphs. Sister Phase 1 docs: `docs/PERFORMANCE_BUDGET.md`, `docs/RUNTIME_DECISION_PLAN.md`, `docs/MODEL_BENCHMARK_PLAN.md`, `docs/EXTERNAL_SETUP_CHECKLIST.md`, `docs/PRODUCTION_BUILD.md`.
