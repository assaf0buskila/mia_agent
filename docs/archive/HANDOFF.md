# Next-agent handoff — Mia / Assaf Agent

**Date:** 2026-08-22  
**Last independently verified suite:** `uv run pytest` — **1919 passed** (2026-08-22, ADR-017). `uv run ruff check app tests` passed.  
**Active `/goal`:** Mia v1 communication operating model (ADR-017) is closed in code. Live host remains `https://mia.assafweb.com` (ADR-014). Operator must put Telegram keys in `mia/prod`, `setWebhook`, and run `mia-migrate` for `20260822_conversation_controls.sql`. `AWS_RUNTIME` stays specified.

This laptop: AWS CLI v2 **installed** (`aws-cli/2.36.29` via `AWSCLIV2-User.msi`). Credentials **missing** until browser `aws login` finishes. Gate: `deploy/assert-aws-identity.ps1` must exit 0 (12-digit Account) before the VPC wizard. Never paste keys, login URLs, or auth codes in chat. AWS is the *next operator step*, not this slice.

Release report: `docs/PRE_CLOUD_RELEASE_REPORT.md`.

Prior stretch: [FDE gold replay](97f276d1-7740-42a2-bf4c-7495f7c6109a).

---

## Load first (every turn)

1. `AGENTS.md` (operating rules; workspace root **is** the repo — no nested `mia/`)
2. This file
3. `docs/PRD.md` (living), `docs/BUILD_STATUS.md`, `docs/DECISIONS.md`, `docs/COMMUNICATION.md`, `docs/CAPABILITY_OWNERSHIP.md`, `docs/FDE_OPERATING_LAYER_GAP.md`
4. Current tree — inspect before trusting this note

Bible source: `Mia_AI_Growth_Sales_Operator_PRD_Build_Bible_v1.1.docx` in the workspace root. If markdown disagrees with the docx, the docx wins until Assaf accepts the markdown.

Package manager is **uv**. Python `>=3.12`. PowerShell: `;` not `&&`.

---

## Who / what

Mia is AssafWeb’s production AI Growth & Sales Operator, not a chatbot. Live site: https://www.assafweb.com/ (Hebrew). Mia herself is **text-only**.

Standing instruction: production adapters are **ADR-015**. ADR-007 still applies inside that map (no catalog dump; IG inbound Meta; LinkedIn member analytics direct). Do not re-ask Assaf Composio vs direct unless the choice changes safety, permissions, or one-sender rules.

Work pattern: Composer 2.5 implements → parent independently reviews, patches if needed, then `uv run ruff check app tests; uv run pytest`. Composer often double-spaces; densify. Codex (`codex-rescue`) was usage-limited until **25 Aug 2026, 16:27** — do not rely on it until then.

Planning/architecture: Grok 4.6, Fable 5, or GPT 5.6. Code execution: Composer 2.5 via sub-agents.

---

## Just finished (do not redo)

### v1 communication operating model — **alive** (2026-08-22, ADR-017)

Telegram is the private owner channel. Website is the primary customer sales channel. WhatsApp is verified website-handoff continuation only. Personal/unknown WhatsApp stays human-only. Hot leads stop selling and notify Telegram. Email send stays approval-gated. Transport unchanged (ADR-016: Meta inbound, one outbound sender). Do not add Instagram as a sales inbox. Do not flip production `MIA_AUTOMATION_MODE` out of shadow.

Key files: `docs/COMMUNICATION.md`, `app/api/telegram.py`, `app/domain/conversation_scope.py`, `app/domain/hot_handoff.py`, `tests/unit/test_comm_operating_model.py`, `migrations/20260822_conversation_controls.sql`.

### Pre-cloud application package — **alive** (2026-08-22)

Composio Instagram send + insights ports are wired (toolkit `20260819_00`; pins `INSTAGRAM_SEND_TEXT_MESSAGE`, `INSTAGRAM_GET_IG_USER_MEDIA`, `INSTAGRAM_GET_IG_MEDIA_INSIGHTS`). Default sender remains `direct`. In-process §23 E2E stories cover website funnel/handoff, Gmail ingest (no send), Sheets mirror, research-as-data, and campaign analysis (no Meta write). Security review tests lock CORS, redact, write-pin allowlist, R4/R5, and gated flags. Operator brief: `docs/PRE_CLOUD_RELEASE_REPORT.md`. Do **not** start AWS. Do **not** flip production sender to `composio` until staging send is tested. Do not invent `MODEL_ROUTING_DECISION.md` scores.

Key files: `app/integrations/instagram.py`, `app/integrations/instagram_insights.py`, `tests/e2e/test_preprod_stories.py`, `tests/unit/test_security_review.py`.

### Website + SEO (pre-AWS) — **alive** (2026-08-22)

Assaf ADOPT + widget path A. `form_started` in widget. GSC/GA4 read ports (Disabled when env empty). Firecrawl homepage audit allowlisted to assafweb.com. Owner WhatsApp SEO classify. `seo_recommendations` persist. `website_edit` approval persist-only (Cursor applies AssafWeb diffs after Assaf yes). Minimization report is recommendation-only. Migration `migrations/20260822_seo_recommendations.sql`. Live Composio GA4/GSC OAuth still operator. Form/FAB still bypass handoff (see `docs/WEBSITE_MINIMIZATION_REPORT.md`). SEO inspect does **not** block AWS first live.

Key files: `docs/WEBSITE_SEO_GAP_REPORT.md`, `app/domain/seo.py`, `app/integrations/search_console.py`, `app/integrations/ga4.py`, `app/integrations/seo_audit.py`.

### `/health/ready` schema check — **alive**

`database_ready()` still pings `SELECT 1`, then `schema_ready(engine)` requires every mapped table/column from `Base.metadata`. Missing column (the preview 500) → 503 `not_ready`. Body still `{"status":"not_ready"}` only — no table/column/DSN. Fix: `uv run mia-migrate`. `/health` unchanged.

Key files: `app/db/session.py`, `tests/unit/test_schema_ready.py`, `tests/unit/test_health.py`.

### Existing-DB column scan — **alive** (schema only)

After `company_domain`, live sqlite still lacked five mapped columns (`owner_tasks.due_ready`/`block_reason`, `webhook_events.claimed_at`, `lead_follow_ups.send_ready`/`block_reason`). `schema_ready` was False; LAN `GET /health/ready` was 503. `migrations/20260822_existing_db_scan_columns.sql` applied via `mia-migrate`. Live `schema_ready` True; `/health/ready` `{"status":"ok"}`. Website preview does not need these columns; due-scan, reconciliation, and owner-task rows do.

### `mia-migrate` — **alive** (schema only; never send)

Operator CLI applies `migrations/*.sql` in filename order onto the configured DB. Tracks `schema_migrations`. Sqlite skips Postgres-only `20260821_approval_campaign_resource.sql` without recording it. Duplicate column/table is treated as applied. JSON `applied`/`skipped`/`already`/`failed`; exit 1 on `failed`. Kill switch does not block. No DSN in stdout. Prod API lifespan skips `create_all` (schema is migrate-only so ALB `/health/live` binds without waiting). Dev/test still `init_db()` on boot. Existing sqlite: `migrations/20260822_lead_sales_state_company_domain.sql` then `migrations/20260822_existing_db_scan_columns.sql` (both applied locally). Duplicate column/table is treated as applied.

Key files: `app/db/migrate.py`, `app/workers/migrate.py`, `pyproject.toml`, `tests/unit/test_migrate.py`.

### Ask Mia local preview page — **alive**

`GET /v1/website/preview` is a same-origin white HTML page that loads `widget.js`. Operator can see the launcher without embedding on assafweb.com. Not a customer surface. `Cache-Control: no-cache`. No auto-open. No PII.

Key files: `app/api/website.py`, `tests/unit/test_website.py`.

### Bible `/health/live` + `/health/ready` — **alive**

`GET /health/live` is process-up only (`{"status":"ok"}`, no DB, ignore kill switch). `GET /health/ready` pings `database_ready()` (`SELECT 1`); 200 `ok` or 503 `not_ready`. Route uses `response_model=None` so FastAPI accepts `JSONResponse`. Neither body has capabilities, env, URLs, or secrets. Existing `GET /health` is unchanged (operator diagnostic). No widget/CORS/graph change.

Key files: `app/main.py`, `app/db/session.py`, `tests/unit/test_health.py`.

### `/health` `sales_llm` boolean — **alive**

Operator diagnostic includes `sales_llm`, `sales_gemini`, `composio`, `composio_webhook`, `postgres`, `public_https`, `whatsapp_ingest`, and `whatsapp_owner`. No secrets, DSNs, or ids. First live needs `postgres` + `public_https` true on the host. Cursor plugin Calendar Active is not Mia Calendar unless `MIA_COMPOSIO_USER_ID` matches Composio debug `@user_id`.

### Gemini sales fallback — **alive**

OpenAI primary still runs first. After OpenAI model ids fail, one Gemini AI Studio Chat Completions retry (`MIA_GEMINI_API_KEY` + `MIA_SALES_GEMINI_MODEL`, official OpenAI-compat host). Lint then canned. Not Vertex. Thread summary stays OpenAI-only this slice.

Key files: `app/integrations/sales_reply.py`, `app/core/config.py`, `tests/unit/test_sales_reply.py`.

### OpenAPI hidden in prod — **alive**

`openapi_surface(env=)` unmounts `/docs`, `/redoc`, and `/openapi.json` when `MIA_ENV=prod`. Dev/test keep Swagger. `/health` and channel APIs unchanged. Local `.env` with `MIA_ENV=prod` will 404 those three paths after restart — use `/health`, not Swagger. No CORS/widget/graph change.

Key files: `app/main.py`, `tests/unit/test_health.py`.

### Ask Mia widget cache + tap targets — **alive** (contrast still pinned)

`GET /v1/website/widget.js` sends `Cache-Control: no-cache` so browsers do not keep the old white-on-white script. Chrome is 16px/1.5 with 44px min launcher and action buttons; input 16px (no iOS zoom). Dark text pins unchanged. No graph/CORS/auto-open change.

Key files: `app/api/website.py`, `app/web/ask_mia.js`, `tests/unit/test_website.py`.

### Ask Mia widget contrast — **alive** (host color cannot leak)

Widget panel/messages/input pin `color:#1a1a1a` (and `color-scheme:light` on `#ask-mia-root`) so AssafWeb or other host `color:#fff` cannot paint white text on the white panel. Launcher/send stay white on `#1a1a1a`; WhatsApp stays white on green. No API/graph change.

Key files: `app/web/ask_mia.js`, `tests/unit/test_website.py`.

### `ai_runs.decision_confidence` (FDE unit 8 remainder) — **alive** (audit only; no LLM score)

Website and prospect `persist_ai_run` stamps `decision_confidence="1.0"` from `DETERMINISTIC_NBA_CONFIDENCE` (same pin as `decision_from_sales`). No persist parameter — callers cannot pass a model score. `sanitize_decision_confidence` clamps 0–1; `1`/`1.0` store as `"1.0"`; invalid `""`. First-write-wins. No prompt/reply/lead text. `cost_usd` still 0. Does not enable HYBRID. Existing DBs: `migrations/20260822_ai_run_decision_confidence.sql` (empty default; do not backfill).

Key files: `app/domain/policies/decision.py`, `app/domain/ai_runs.py`, `app/db/models.py`, `app/db/store.py`, `tests/unit/test_ai_runs.py`, `tests/unit/test_decision_policy.py`.

### Research-snippets freshness (Adjustment N remainder) — **alive** (audit only; no crawl/browser)

`enrich_research_ack` stamps `research_snippets` (`SHORT_CACHE` 300s, source `research_port`) on tool `research_search`. Non-empty https sources block → `cached`; empty/disabled/HTTP → `unverified`; kill-switch deny → `freshness=""`. Hebrew ack unchanged (still title+host only). Canonical TOOL_RESULT payload still `{tool, status, result_count}`. No URLs/excerpts/titles on `tool_runs`. Nineteen catalog facts now. All live/short-cache retrieval stamps are wired; versioned knowledge RAG still Missing — do not mark Adjustment N Complete.

Key files: `app/domain/policies/freshness.py`, `app/integrations/research.py`, `tests/unit/test_freshness.py`, `tests/unit/test_tool_freshness_research.py`.

### LinkedIn profile freshness (Adjustment N remainder) — **alive** (audit only; no post/DM)

`enrich_linkedin_ack` stamps `linkedin_profile` (`SHORT_CACHE` 300s, source `linkedin_port`) on tool `linkedin_profile`. Non-empty line → `cached`; empty/disabled/HTTP → `unverified`; kill-switch deny → `freshness=""`. Hebrew ack unchanged. Canonical TOOL_RESULT payload still `{tool, status, result_count}`. No name/headline/URLs. RAG still Missing.

Key files: `app/domain/policies/freshness.py`, `app/integrations/linkedin.py`, `tests/unit/test_freshness.py`, `tests/unit/test_tool_freshness_linkedin_profile.py`.

### LinkedIn content-metrics freshness (Adjustment N remainder) — **alive** (audit only; no post/DM)

`enrich_linkedin_analytics_ack` stamps `linkedin_content_metrics` (`SHORT_CACHE` 300s, source `linkedin_analytics_port`) on tool `linkedin_analytics`. Non-empty stats line → `cached` (`ok` or `partial`); empty/disabled/HTTP → `unverified`; kill-switch deny → `freshness=""`. Hebrew ack unchanged. Canonical TOOL_RESULT payload still `{tool, status, result_count}`. No URLs/member IDs/tokens. Analytics catalog fact is `linkedin_content_metrics`. RAG still Missing.

Key files: `app/domain/policies/freshness.py`, `app/integrations/linkedin_analytics.py`, `tests/unit/test_freshness.py`, `tests/unit/test_tool_freshness_linkedin.py`.

### `ai_runs.prompt_version` (FDE unit 8 remainder) — **alive** (audit only; no prompt body)

New website and prospect `persist_ai_run` rows stamp `prompt_version=sales_reply_v1` (`PROMPT_VERSION` beside `_SYSTEM_PROMPT` in `app/integrations/sales_reply.py`; domain duplicates the pin — no integrations import). `sanitize_prompt_version` allowlists `^[a-zA-Z0-9._-]{1,32}$`; invalid `""`. First-write-wins. Frozen SHA256 of `_SYSTEM_PROMPT` in tests; bump both pins + hash when the prompt changes. No prompt/reply/lead text. `cost_usd` still 0. `decision_confidence="1.0"` is a separate pin. Does not enable HYBRID. Existing DBs: `migrations/20260822_ai_run_prompt_version.sql` (empty default; do not backfill).

Key files: `app/integrations/sales_reply.py`, `app/domain/ai_runs.py`, `app/db/models.py`, `app/db/store.py`, `tests/unit/test_ai_runs.py`.

### First AWS live (ADR-014) — **docs + deploy templates** (not `AWS_RUNTIME` alive)

Assaf ADOPT: production host is ECS Fargate + RDS PostgreSQL 16 + Secrets Manager box `mia/prod` (Assaf fills keys; ECS injects `MIA_*`; never git/chat/host `.env`) + ALB/ACM `https://mia.assafweb.com`. Lambda is not the graph and not the key box. `deploy/Dockerfile` (RDS CA `chmod 644` + non-root; `--proxy-headers`; `--timeout-keep-alive 130` above ALB idle 120s; local `docker build` proven) + ECS/IAM trust+permissions examples + ALB (public subnets; IP target `/health/live`; TLS1.3; HTTP:80 `HTTP_301`) + first-live CloudWatch ALB alarms without SNS + EventBridge Scheduler persist-only (create after `/health` green). Operator order: SGs (`--query GroupId`) → `deploy/fill-placeholders.ps1` → authorize/RDS from `deploy/local/` (never `*.example.json`) → box → `create-role` → ACM **ISSUED** → ALB + target group → **re-stamp hashes** → listeners / Route53 → `mia-migrate` (`tasks-stopped`) → `create-service` (`services-stable`) → CloudWatch ALB alarms. CI `image` job builds that Dockerfile (no ECR push). `psycopg` DSN pin keeps `sslmode` query. Do not mark `CapabilityId.AWS_RUNTIME` alive until `/health` on the ALB shows `postgres` + `public_https`. Do not implement `app.infra`, SQS, WAF, or AgentCore this slice.

Key files: `docs/DECISIONS.md` ADR-014, `docs/PRODUCTION_BUILD.md`, `docs/RUNTIME_DECISION_PLAN.md`, `deploy/`.

### Prod API skips `create_all` — **alive** (schema = migrate)

`app.main` lifespan calls `init_db()` only when `MIA_ENV` is not `prod`. ALB `/health/live` is process-up and must not wait on RDS `create_all` (which also does not apply SQL migrations). First live schema is the `mia-migrate` run-task. ECS service grace **120s** + circuit breaker enable/rollback. Container health `startPeriod` 90s, urllib timeout 4s, `stopTimeout` 60s (above ALB deregistration 30s). `PYTHONUNBUFFERED=1` in the image.

Key files: `app/main.py`, `deploy/ecs-service.example.json`, `deploy/ecs-task-definition.example.json`, `deploy/Dockerfile`, `tests/unit/test_health.py`, `tests/unit/test_deploy_secret_box.py`.

### First-live ALB CloudWatch alarms — **templates** (Gate F still not Complete)

`deploy/cloudwatch-alb-unhealthy.example.json` (`UnHealthyHostCount` **Minimum**, two datapoints, threshold 0) and `deploy/cloudwatch-alb-5xx.example.json` (`HTTPCode_ELB_5XX_Count` Sum). No `AlarmActions` / SNS. Dimensions use `app/mia/HASH` + `targetgroup/mia/HASH` (not full ARNs). `fill-placeholders.ps1` stamps both hashes. RDS example pins `EngineVersion` `16`. VPC step uses the official **VPC and more** wizard with DNS hostnames on. Do not mark Gate F Complete.

Key files: `deploy/cloudwatch-alb-unhealthy.example.json`, `deploy/cloudwatch-alb-5xx.example.json`, `deploy/rds.example.json`, `docs/PRODUCTION_BUILD.md` §3.6, `tests/unit/test_deploy_secret_box.py`.

### WhatsApp ADR-016 — **alive** (2026-08-22)

Composio WhatsApp toolkit `20260815_00` has no incoming-customer-message trigger (`WHATSAPP_MESSAGE_STATUS_UPDATED_TRIGGER` is delivery status only; `WHATSAPP_GET_MESSAGE_HISTORY` is not an inbox). Do not poll it. Meta `POST /v1/whatsapp/webhook` stays the thin inbound transport. Outbound is one owner: Graph, or Composio `WHATSAPP_SEND_MESSAGE` when `MIA_WHATSAPP_SENDER=composio`. Never dual-send. Template send is not wired. Meta verify/app-secret stay (inbound HMAC). Graph access token still needed for WhatsApp media STT. Production ECS sender is `composio` (Assaf ADOPT 2026-08-22; WhatsApp Active on `MIA_COMPOSIO_USER_ID`; phone number id present).

Key files: `app/integrations/whatsapp.py`, `app/api/composio.py`, `tests/unit/test_whatsapp_composio.py`, `docs/DECISIONS.md` ADR-016.

### Production adapter map (ADR-015) — **docs + env/JSON comments** (no Composio IG ports yet)

Assaf ADOPT: LangGraph brain, Postgres SoR. WhatsApp inbound = Meta webhook (ADR-016). WhatsApp send = Graph default or Composio `WHATSAPP_SEND_MESSAGE` when `MIA_WHATSAPP_SENDER=composio`. IG inbound = Meta webhook. IG send/insights = Composio **when adapters land** (Graph tokens stay until then; `MIA_INSTAGRAM_SENDER` remains `direct`|`manychat`). Gmail/Calendar/Sheets/LinkedIn profile/Meta ads read = Composio. LinkedIn member analytics = direct token. Research = Firecrawl (no Apify env). Composio WhatsApp inbound unused (no customer-message trigger). ManyChat = optional ingest sidecar. Deleted VPS leftovers `deploy/Caddyfile` and `deploy/mia.service`.

Key files: `docs/DECISIONS.md` ADR-015, `.env.example`, `deploy/mia-prod.secret.example.json`, `docs/PROVIDER_MATRIX.md`, `docs/CAPABILITY_OWNERSHIP.md`.

### Production build sequence — **docs** (not a grant of writes)

`docs/PRODUCTION_BUILD.md` is the ordered operator go-live: hygiene → local loopback → Cloudflare **test-only** tunnel → AWS first live (SM box + Fargate + RDS + ALB) → identity → production webhooks → widget on assafweb.com (`NEXT_PUBLIC_MIA_BASE_URL=https://mia.assafweb.com`) → staging calendar/DM → day-2 ops. Cloudflare Workers and VPS `.env` remain out of production. Gated writes stay off. Gate F: first-live ALB metric alarms exist; SNS pager and dashboards still Missing.

Key files: `docs/PRODUCTION_BUILD.md`, `docs/RUNBOOK.md`, `docs/EXTERNAL_SETUP_CHECKLIST.md`, `.env.example`.

### Instagram content-metrics freshness (Adjustment N remainder) — **alive** (audit only; no publish)

`enrich_content_insights_ack` stamps `instagram_content_metrics` (`SHORT_CACHE` 300s, source `instagram_insights_port`) on tool `instagram_insights`. Items present → `cached`; empty/disabled/HTTP → `unverified`; kill-switch deny → `freshness=""`. Hebrew ack unchanged. Canonical TOOL_RESULT payload still `{tool, status, result_count}`. No captions/URLs. Versioned knowledge RAG still Missing.

Key files: `app/domain/policies/freshness.py`, `app/integrations/instagram_insights.py`, `tests/unit/test_freshness.py`, `tests/unit/test_tool_freshness_instagram.py`.

### `ai_runs.automation_mode` (FDE unit 8 remainder) — **alive** (audit only; no HYBRID)

Website and prospect inbound `persist_ai_run` stamp allowlisted `automation_mode` (`off|draft_only|shadow|hybrid|auto_approved`) from `settings.automation_mode`. Invalid → `""`. First-write-wins. No prompt/reply. Does not enable HYBRID send. `cost_usd` still 0. `prompt_version` is a separate pin (`sales_reply_v1`). `decision_confidence="1.0"` is a separate pin. Migration `migrations/20260822_ai_run_automation_mode.sql`.

Key files: `app/domain/ai_runs.py`, `app/db/models.py`, `app/db/store.py`, `app/api/inbound.py`, `app/api/website.py`, `tests/unit/test_ai_runs.py`.

### Local Cloudflare webhook tunnel — **test only** (not production)

TryCloudflare quick tunnel proxies local uvicorn (`127.0.0.1:8000`) so Meta/Composio can POST HTTPS. Sales graph stays FastAPI. Cloudflare Workers/named tunnels are **not** the production runtime. Hostname is ephemeral (`*.trycloudflare.com`); changes every restart. Docs: `docs/PRODUCTION_BUILD.md`, `docs/EXTERNAL_SETUP_CHECKLIST.md`, `docs/RUNBOOK.md`.

### Inspect webhook envelope overlay (Adjustment D remainder) — **alive** (inspect only; no replay)

`inspect_open_findings` overlays sanitized `channel` + `envelope_kind` from `webhook_events` onto `webhook_received` / `sent_without_out` findings. Subject key splits on the first colon (`{provider}:{provider_event_id}`; IG `igref:` ids keep remaining colons). Missing/invalid row → empty strings. `handoff_expired` stays empty (token_hash is not a webhook). `mia-reconcile --inspect` JSON items always include those two keys. Counts-only stdout and logger unchanged. Cap 50. Never repairs, never `mark_webhook`, never dumps body/PII.

Key files: `app/db/store.py` (`get_webhook`), `app/domain/reconciliation.py`, `app/workers/reconcile.py`, `tests/unit/test_reconciliation.py`.

### Sanitized webhook envelope (Adjustment D remainder) — **alive** (kind+channel only; no body)

`webhook_events` stores allowlisted `channel` (`Channel` enum) and `envelope_kind` (`text`|`audio`|`empty`|`referral`) on `claim_webhook`. Kind from `webhook_envelope_kind(item)`: `igref:` → referral, `source=audio` → audio, empty text → empty, else text. Invalid values store `""`. Reclaim fills empty columns only (first-write-wins). Never stores body, `from`, email, or raw JSON. No `webhook_events.correlation_id`. Inspect overlays those two columns; replay still Missing. Migration `migrations/20260821_webhook_envelope.sql`.

Key files: `app/domain/events.py`, `app/db/models.py`, `app/db/store.py`, `app/domain/idempotency.py`, `app/api/inbound.py`, `tests/unit/test_events.py`, `tests/unit/test_idempotency.py`, `tests/unit/test_webhook_envelope.py`.

### `tool_runs.correlation_id` (Adjustment O remainder) — **alive** (audit join; no queue)

`persist_tool_outcome` sanitizes `correlation_id` once and writes it on both the canonical TOOL_RESULT envelope and `tool_runs.correlation_id` (`String(64)`; first-write-wins). Payload stays `{tool, status, result_count}` only. Prospect/website reuse `ai_runs.run_id`; owner `cor_*`. Instagram `conversation_ownership` now shares the prospect `run_id` (generated before the ownership persist). Gmail hydrate `gmail_fetch` reuses MESSAGE_IN correlation. Due-scan `lead_recent_messages` and website session-create `sheets_mirror` stay empty (no ingress run). Invalid ids store `""`. No SQS. Migration `migrations/20260821_tool_run_correlation_id.sql`.

Key files: `app/domain/events.py`, `app/db/models.py`, `app/db/store.py`, `app/api/inbound.py`, `app/api/composio.py`, `tests/unit/test_tool_runs.py`, `tests/unit/test_correlation.py`, `tests/unit/test_tool_freshness_gmail.py`.

### Campaign/content Sheets `tool_runs` (Adjustment I remainder) — **alive** (metadata only; distinct tools)

After a successful `claim_sheets_mirror` on tab `campaign` / `content`, `maybe_mirror_campaign_control` / `maybe_mirror_content_insights` wall-clock upserts and return `sheets_tab_mirror_outcome`. Owner analytics persists extras: `{inbound_id}:tool:sheets_mirror_campaign` and `{inbound_id}:tool:sheets_mirror_content`. Distinct tools so they do not collide with sales/session `sheets_mirror` on the same inbound. Canonical TOOL_RESULT payload stays `{tool, status, result_count}` only. Claim-fail and demo return `None` (no persist). Kill-switch zero writes stay `denied` with measured latency. Unclaimed empty `inbound_id` still upserts, no tool_run. `cost_usd` still 0.

Key files: `app/domain/tools.py`, `app/domain/policies/failure_policy.py`, `app/domain/events.py`, `app/integrations/sheets.py`, `app/integrations/meta_ads.py`, `app/integrations/instagram_insights.py`, `app/api/inbound.py`, `tests/unit/test_pacing.py`, `tests/unit/test_instagram_insights.py`, `tests/unit/test_idempotency_persist_paths.py`.

### Website session-tab `sheets_mirror` TOOL_RESULT + latency (Adjustment I remainder) — **alive** (metadata only)

After a successful `claim_sheets_mirror(..., tab="session")` in `process_website_session`, website wall-clocks `mirror_source` / KPI upserts and persists `sheets_mirror_outcome(..., latency_ms=elapsed_ms(started))`. Key `{session_id}:tool:sheets_mirror` does not collide with sales-tab `{message_id}:tool:sheets_mirror`. Canonical TOOL_RESULT payload stays `{tool, status, result_count}` only. Kill-switch zero writes stay `denied` with measured latency. Claim-fail and demo still skip persist. Campaign/content tabs use distinct tools (`sheets_mirror_campaign` / `sheets_mirror_content`), not `sheets_mirror`. `cost_usd` still 0. No node timers.

Key files: `app/api/website.py`, `tests/unit/test_website.py`, `tests/unit/test_events.py`.

### Sheets sales-tab `tool_runs.latency_ms` (Adjustment I remainder) — **alive** (metadata only)

After a successful `claim_sheets_mirror(..., tab="sales")`, inbound and website wall-clock the `mirror_*` / KPI upserts and pass `latency_ms=elapsed_ms(started)` into `sheets_mirror_outcome`. Persist is `tool_runs.latency_ms`. Canonical TOOL_RESULT payload stays `{tool, status, result_count}` only. Kill-switch zero writes stay `denied` with measured latency (mirrors were attempted). Claim-fail and demo still skip persist. Denied-before-port enrich paths stay 0. `cost_usd` still 0. No node timers.

Key files: `app/domain/events.py`, `app/api/inbound.py`, `app/api/website.py`, `tests/unit/test_events.py`, `tests/unit/test_website.py`.

### HTTP classify on OpenAI thread-summary compose (Adjustment G remainder) — **alive** (canned fallback unchanged; no 502)

`OpenAIThreadSummaryPort._complete` raises `AdapterHttpError` on HTTP ≥400 / transport (same map as sales-reply/STT). `summarize` catches it, tries the fallback model, then canned (`intent=unclear`, empty summary) — owner ack/webhook never 502s on summary HTTP. HTTP 200 empty/parse still returns `None`. Kill switch still skips HTTP. Banned-phrase parse stays fail-closed. `gmail_summary` / `thread_summary` are not `ALLOWLISTED_TOOLS` persist paths. Emails stay untrusted data. No send.

Key files: `app/integrations/thread_summary.py`, `tests/unit/test_gmail_summaries.py`.

### HTTP classify on OpenAI sales-reply compose (Adjustment G remainder) — **alive** (canned fallback unchanged; no 502)

`OpenAISalesReplyPort._complete` raises `AdapterHttpError` on HTTP ≥400 / transport (same map as STT). `compose` catches it, tries the fallback model, then canned — webhook never 502s on paraphrase HTTP. HTTP 200 empty/parse still returns `None` (not AdapterHttpError). Kill switch still skips HTTP. Lint failure unchanged. `sales_reply` is not an `ALLOWLISTED_TOOLS` persist path. No send.

Key files: `app/integrations/sales_reply.py`, `tests/unit/test_sales_reply.py`.

### HTTP classify on WhatsApp/IG send + STT (Adjustment G remainder) — **alive** (502 rollback unchanged; no new send)

Live WhatsApp send, Instagram send, OpenAI STT, and WhatsApp media download raise `AdapterHttpError` on HTTP ≥400 / transport (same map as Gmail/Sheets/Calendar), then wrap as `WhatsAppSendError` / `InstagramSendError` / `TranscriptionError` / `WhatsAppMediaError` so webhook 502 + send-failure claim rollback stay. `send_inbound_reply` still only catches `RuntimeError`. STT primary→fallback still retries the fallback model on classified HTTP; HTTP 200 empty text stays `TranscriptionError` (not AdapterHttpError). Instagram unsupported graph host, media missing-url / host / size stay MiaError without AdapterHttpError. No send tool added. No send enabled.

Key files: `app/integrations/whatsapp.py`, `app/integrations/instagram.py`, `app/integrations/transcribe.py`, `tests/unit/test_channels.py`, `tests/unit/test_instagram.py`, `tests/unit/test_whatsapp_stt.py`.

### Cross-cutting idempotency persist-path suite (Adjustment E acceptance) — **alive** (tests only)

`tests/unit/test_idempotency_persist_paths.py` invokes each listed persist action twice and asserts one SoR row: webhook claim, canonical MESSAGE_IN, MESSAGE_OUT `{inbound_id}:out`, calendar booked persist, booking-key lookup skip-create, reschedule persist, approval handoff, owner_task save, cancellation inbound claim, Sheets sales mirror, follow-up inbound claim. Unique ids prefixed `xcut.`. `LIVE_CLAIM_SCOPES` ⊆ allowlist; `canonical` stay unit-only. No production change. Queue redelivery / Meta execute / Gmail send still missing (gated). Powertools DEFER.

Key files: `tests/unit/test_idempotency_persist_paths.py`.

### Cancellation persist claim (Adjustment E) — **alive** (local-only; no provider delete)

`_request_cancellation` is gated by `claim_operation(scope=calendar_cancellation, key={inbound_id}:cancellation)` then `complete_operation` `{"ok": true}` (try/finally). Key is inbound event, **not** lead — failed webhook reclaim of the same inbound skips a second mark/canonical; already-`cancellation_requested` short-circuits **before** claim so a later inbound still returns the Hebrew requested copy; empty `inbound_id` keeps today’s write (direct unit tests). Claim-fail while still booked → `RETRY` + `CANCELLATION_DENIED_REPLY`. Canonical stays `{lead_id}:cancellation_requested`. Reschedule unclaimed this path. Provider delete still R5.

Key files: `app/domain/idempotency.py`, `app/domain/meeting_changes.py`, `app/domain/calendar_booking.py`, `tests/unit/test_calendar_gate2.py`, `tests/unit/test_idempotency.py`.

### HTTP classify on Sheets + calendar booking (Adjustment G remainder) — **alive** (copy unchanged; no send)

Live Composio Sheets upsert and Calendar booking (EVENTS_LIST / CREATE_EVENT / EVENTS_GET / PATCH_EVENT) raise `AdapterHttpError` on HTTP ≥400 / transport (same map as free/busy, Gmail fetch, LinkedIn, IG media-list, Meta, research). HTTP 200 `successful=False` / parse fail still skip/None. Booking/reschedule catch classified `ToolOutcome` and keep Hebrew retry copy; create/PATCH HTTP still **verifies** (recovery can book/reschedule); lookup/GET HTTP retries without write; `find_free_slots` HTTP is retry, not conflict. `mirror_*` catch `AdapterHttpError` and return False (never raise); inbound `sheets_mirror` still stamps `denied` on zero writes. WhatsApp/IG send and STT unclassified this slice. No send enabled.

Key files: `app/integrations/calendar_booking.py`, `app/domain/calendar_booking.py`, `app/domain/meeting_changes.py`, `app/integrations/sheets.py`, `tests/unit/test_calendar_booking.py`, `tests/unit/test_calendar_gate2.py`, `tests/unit/test_sheets.py`.

### HTTP result classification on remaining read adapters (Adjustment G) — **alive** (acks unchanged)

Live Gmail fetch, LinkedIn profile, and Instagram **media-list** raise `AdapterHttpError` on HTTP ≥400 / transport (same map as Meta/Calendar/research). Enrich/hydrate stamp `ToolOutcome.status`; owner/customer copy unchanged. Per-media IG insights 400 still skips that media. Composio 200 `successful=False` still empty/None. No send.

Key files: `app/integrations/gmail.py`, `app/api/composio.py`, `app/integrations/linkedin.py`, `app/integrations/instagram_insights.py`, `tests/unit/test_gmail.py`, `tests/unit/test_linkedin.py`, `tests/unit/test_instagram_insights.py`.

### Dead-letter inspect (Adjustment D / Gate B) — **alive** (inspect only; no replay)

`inspect_open_findings` reads open `reconciliation_findings` (allowlisted kind + subject_key; sorted; cap 50) and overlays sanitized webhook `channel` + `envelope_kind` for `webhook_received` / `sent_without_out`. `uv run mia-reconcile` stays counts-only. `uv run mia-reconcile --inspect` adds `open_count` (listed length) + `open_findings` (`kind`, `subject_key`, `channel`, `envelope_kind`). Never repairs, never `mark_webhook`, never sends. Logger stays counts-only.

Key files: `app/domain/reconciliation.py`, `app/workers/reconcile.py`, `tests/unit/test_reconciliation.py`, `docs/RUNBOOK.md`.

### Gate F operator runbook (Adjustment O / R) — **docs + first-live ALB alarms** (SNS/dashboards still Missing)

`docs/RUNBOOK.md` is the production operator runbook: `MIA_KILL_SWITCH` + `/health`, conversation kill vs human takeover vs resume, `MIA_AUTOMATION_MODE` (SHADOW default; HYBRID unwired), named write flags (only `MIA_CALENDAR_WRITE` wired), Instagram one-sender, `mia-due-scan` / `mia-reconcile` (persist-only / flag-only), rollback (local restart; production = new ECS task revision, ADR-014). First-live ALB metric alarms exist without SNS. Does not enable gated writes. LangSmith and SNS pager still missing — do not mark Gate F Complete.

Key files: `docs/RUNBOOK.md`, `docs/EXTERNAL_SETUP_CHECKLIST.md`, `docs/PRE_PRODUCTION_GAP_REPORT.md`, `docs/PRD.md`, `docs/BUILD_STATUS.md`.

### Follow-up persist claim (Adjustment E) — **alive** (persist-only; no send)

`apply_follow_up_policy` is gated by `claim_operation(scope=follow_up, key={inbound_id}:followup)` then `complete_operation` `{"ok": true}` (try/finally so early cancel/recover/kill-switch still complete). Key is inbound event, **not** lead — a later inbound still cancels/recovers/creates; failed webhook reclaim of the same inbound skips a second upsert. Empty `inbound_id` keeps today’s write (direct unit tests). Due-scan and `cancel_follow_up_for_booked` unchanged. Send still gated.

Key files: `app/domain/idempotency.py`, `app/domain/followups.py`, `app/api/inbound.py`, `app/api/website.py`, `tests/unit/test_followups.py`, `tests/unit/test_idempotency.py`.

### CanonicalEvent payload_version (Adjustment D) — **alive** (envelope field; no SQS)

`CanonicalEvent.payload_version` + `canonical_events.payload_version` (`String(8)` default `""`). Allowlist `"1"` (`CANONICAL_PAYLOAD_VERSION`). `sanitize_payload_version` / `stamp_payload_version` in `app/domain/events.py`. Stamp runs only in `LeadStore.save_canonical_event` (inbound/website/graph/tool persist share one path). First-write-wins does not rewrite. Not in `payload_json` or GraphState. No parallel `InboundEvent`. No `business_id` tenant. `provider_event_id` remains the provider raw id (not a UUID overlay). Migration `migrations/20260821_canonical_payload_version.sql`.

Key files: `app/domain/events.py`, `app/db/models.py`, `app/db/store.py`, `tests/unit/test_correlation.py`, `tests/unit/test_events.py`.

### Sheets mirror persist claim (Adjustment E / Gate E) — **alive**

Sheets upserts are gated by `claim_operation(scope=sheets_mirror, key={inbound_id}:sheets:{tab})` then `complete_operation` `{"ok": true}`. Tabs: `sales` (prospect inbound + website message batch), `session` (website session create source+KPI), `campaign` (owner analytics 02/03), `content` (owner content 07). Key is inbound event, **not** lead — a later inbound for the same lead still updates the living snapshot; failed webhook reclaim of the same inbound skips the second Composio upsert. Empty `inbound_id` on enrich helpers keeps today's write (unit tests). Postgres stays SoR. No Meta/Gmail/follow-up send.

Key files: `app/domain/idempotency.py`, `app/integrations/sheets.py`, `app/api/inbound.py`, `app/api/website.py`, `app/integrations/meta_ads.py`, `app/integrations/instagram_insights.py`, `tests/unit/test_sheets.py`, `tests/unit/test_pacing.py`, `tests/unit/test_idempotency.py`.

### owner_task persist claim + ManyChat identity columns — **alive** (Adjustment E + B)

Owner task persist: `claim_operation(scope=owner_task, key={provider}:{provider_event_id})` wraps `save_owner_task` only; `complete_operation` `{"ok": true}`; duplicate claim skips second persist; ack/analytics/takeover unchanged; execute still gated (no due-scan execute). ManyChat identity: `channel_identities.manychat_subscriber_id` + `manychat_conversation_id` via `stamp_manychat_identity` on `provider=manychat` prospect path after `open_channel_lead` (first-write-wins; sanitized `[A-Za-z0-9._-]+`; Graph IG inbound does not stamp); identity key remains channel+external_id; Meta routing still external; migration `migrations/20260821_manychat_identity_ids.sql`.

Key files: `app/api/inbound.py`, `app/db/store.py`, `app/db/models.py`, `app/domain/identity.py`, `tests/unit/test_owner_tasks.py`, `tests/unit/test_manychat.py`.

### Auth-owner matrix (Adjustment A / L) — **docs only**

Written matrix in `docs/CAPABILITY_OWNERSHIP.md`: actor identity (prospect / owner phone allowlist / CORS website / unmodeled service account), ingress verification (Meta HMAC, ManyChat bearer, Composio HMAC, website CORS), and per-job credentials with one execution adapter each (ADR-007). Shared Composio/OpenAI/IG tokens are credential pools, not extra owners. Named write flags do not authenticate. No `app/auth` service, no `business_id` tenant. Production key box later became ADR-014 Secrets Manager `mia/prod` (app still reads env after ECS inject). Application code unchanged this slice. Suite still **1603 passed**.

Key files: `docs/CAPABILITY_OWNERSHIP.md`, `docs/PRE_PRODUCTION_GAP_REPORT.md`, `docs/PRD.md`, `docs/BUILD_STATUS.md`.

### ownership + follow-up + funnel freshness (Adjustment N) — **alive** (audit only; ack unchanged)

Four honest-retrieval stamps wired on real paths (no flood): **`conversation_ownership`** (`live`) on prospect Instagram inbound once per lead (`{lead_id}:ownership`; sender allowlist `direct`/`manychat` matching the Graph adapter); **`owner_permissions`** (`live`) on owner inbound once per owner external id (`owner:{from}`); **`lead_recent_messages`** (`cached`) when due-scan counts same-day `message_out` (`{lead_id}:followup-scan:{today}`; provider `followup_scan`); **`website_session_events`** (`cached`) when owner analytics WATCH path runs `count_behavior_events` (separate tool from `meta_ads_insights`). Versioned knowledge RAG still **Missing** — do not mark Adjustment N Complete.

Key files: `app/domain/ownership_freshness.py`, `app/domain/followups.py`, `app/integrations/meta_ads.py`, `app/api/inbound.py`, `app/domain/tools.py`, `app/domain/policies/failure_policy.py`, `tests/unit/test_tool_freshness_ownership.py`.

### approval persist claim + ManyChat story/comment fixtures — **alive** (Adjustment E + B)

Approval persist: `claim_operation(scope=approval, key={lead_id}:approval:proposal_handoff)` and `{campaign_id}:approval:campaign_write` before APPROVAL_REQUIRED canonical; `complete_operation` `{"ok": true}`; duplicate queue skips second canonical; execute still gated (no Meta; `apply_owner_approval_decision` does not claim). ManyChat contract fixtures `external_request_story.json` (`STORY` + `ig_content_id`; media dropped) and `external_request_comment.json` (generic text; no invented trigger); parse + webhook enter-once tests; `DisabledMessagePort`; Meta Conversation Routing still external.

Key files: `app/domain/approvals.py`, `tests/unit/test_approvals.py`, `tests/fixtures/manychat/external_request_story.json`, `tests/fixtures/manychat/external_request_comment.json`, `tests/unit/test_manychat.py`.

### calendar_reschedule claim + campaign_budget_status freshness — **alive** (Adjustment E + N partial)

Verified reschedule persist: `claim_operation(scope=calendar_reschedule, key={lead_id}:rescheduled:{target_key})` before canonical + owner notify + brief; `complete_operation` `{"ok": true}`; duplicate target returns same Hebrew reply with `changed=False` and skips side effects. Cancellation unchanged (no claim). `owner_task` execute still unwired.

Meta pacing fetch (`this_month` when `MIA_CAMPAIGN_MONTHLY_BUDGET` parses): `campaign_budget_outcome` stamps `campaign_budget_status` on allowlisted tool `meta_ads_pacing` (`live` when spend present, `unverified` when missing); appended via `extra_outcomes` on `enrich_analytics_ack`; owner inbound persists separate `tool_runs` row; does not overwrite `campaign_metrics` on `meta_ads_insights`. Hebrew pacing line unchanged. Versioned knowledge RAG still **Missing** — do not mark Adjustment N Complete.

Key files: `app/domain/meeting_changes.py`, `app/integrations/meta_ads.py`, `app/api/inbound.py`, `app/domain/tools.py`, `app/domain/policies/failure_policy.py`, `tests/unit/test_calendar_gate2.py`, `tests/unit/test_tool_freshness_meta.py`.

### gmail_results + opt_out_status freshness (Adjustment N) — **alive** (audit only; ack unchanged)

`gmail_fetch` stamps `gmail_results` on Composio empty-body hydrate only (`cached` when fetch present, `unverified` when disabled/none). Non-empty trigger body skips the port. Owner Gmail summary is not stamped. `opt_out_status` stamps `live` only when `leads.conversation_killed` changes (stop vs recover); qualify-only turns do not write a tool_run. Canonical TOOL_RESULT payload still `{tool, status, result_count}`. Versioned knowledge RAG still **Missing** — do not mark Adjustment N Complete.

Key files: `app/integrations/gmail.py`, `app/api/composio.py`, `app/domain/conversation_kill.py`, `app/api/inbound.py`, `app/api/website.py`, `app/domain/tools.py`, `app/domain/policies/failure_policy.py`, `tests/unit/test_tool_freshness_gmail.py`, `tests/unit/test_tool_freshness_opt_out.py`.

### Voice transcript row confidence / cost / retention (Adjustment K) — **alive** (row complete; no second STT)

Postgres `voice_transcripts` now stores `confidence` (provider JSON key only; not derived from segments), `cost_usd` always **0** (no price table), and `retention_status=text_only` on save (audio discarded). `sanitize_confidence()` clamps 0–1; bool/NaN/out-of-range → `""`. WhatsApp transcribed items pass confidence only; inbound does not accept cost/retention from the item. Second-provider STT benchmark and frozen audio still **Missing**.

Key files: `app/integrations/transcribe.py`, `app/db/models.py`, `app/db/store.py`, `app/api/whatsapp.py`, `app/api/inbound.py`, `migrations/20260821_voice_transcript_retention.sql`, `tests/unit/test_whatsapp_stt.py`.

Operator: `migrations/20260821_voice_transcript_retention.sql` on existing Postgres/file sqlite DBs.

### Graph Lab safety frozen set (Adjustment J) — **alive** (20/20; no router)

`app/evals/datasets/safety_v1.json` — exactly 20 cases: 12 untrusted-text-as-data sales extract→NBA→canned reply (injection ignored; real stop/poor/proposal/clinic controls) + 8 `sanitize_snippets` URL/title cases (https keep, http/localhost/IP/javascript drop, newline flatten, MAX_SNIPPETS_IN_ACK=2 cap, empty host drop). `run_safety_eval()` in `app/evals/harness.py` scores sales via `extract_sales_signals` → `_sales_field_matches` → `select_next_action` → `reply_for` + `lint_customer_reply` + forbidden substring check; snippet via `sanitize_snippets` only; pass iff action/reply/lint/expect/forbidden or kept count + clean title/excerpt; `quality=None`. No LLM judge, LangSmith, inbound, or DB. `tests/unit/test_evals.py` — 20/20, unique case_ids, ≥2 Hebrew sales users, PII lint (`@`/`972`/`+972`), mutated expected_action fails, source excludes inbound/composio. Live model router and `docs/MODEL_ROUTING_DECISION.md` still correctly missing.

Key files: `app/evals/datasets/safety_v1.json`, `app/evals/harness.py`, `app/evals/__init__.py`, `tests/unit/test_evals.py`, `docs/MODEL_BENCHMARK_PLAN.md`.

### Graph Lab campaign frozen set (Adjustment J) — **alive** (20/20; no router)

`app/evals/datasets/campaign_v1.json` — exactly 20 isolated `analyze_insights` + `format_recommendation_line` cases (uncertain/incomplete, watch, spend-without-clicks, 7d/30d spend-up-clicks-down, spend-without-leads, CPL spike vs compare-wins, creative fatigue, funnel drop + 30d skip, impressions-only watch). `run_campaign_eval()` scores kind + anomaly + Hebrew line substring; `quality=None`. No NBA, Meta write, LLM judge, LangSmith, or DB. Parent patched `cmp_cpl_spike` so previous clicks stay **up** (otherwise 7d compare wins). Live model router and `docs/MODEL_ROUTING_DECISION.md` still correctly missing.

Key files: `app/evals/datasets/campaign_v1.json`, `app/evals/harness.py`, `tests/unit/test_evals.py`, `docs/MODEL_BENCHMARK_PLAN.md`.

### Graph Lab calendar frozen set (Adjustment J) — **alive** (20/20; no router)

`app/evals/datasets/calendar_v1.json` — exactly 20 isolated ADR-012 `carve_policy_slots` cases (Sun–Thu workday/hours, 24h notice, MAX_POLICY_SLOTS=3 cap, Friday/Saturday zero, bad timezone, naive gap skip, cross-midnight, snap-to-:30, after-hours, before-open). `run_calendar_eval()` in `app/evals/harness.py` builds `TimeSlot` gaps from fixture timestamps, calls `carve_policy_slots` only; pass iff UTC `expected_slot_starts` match in order; `CaseResult.expected_action`/`actual_action` = comma-joined starts or `none`; `reply=""`; `quality=None`. No NBA, reply, LLM judge, LangSmith, DB, or live Calendar API. `tests/unit/test_evals.py` — 20/20, unique case_ids, `cal_sun_2h_three_slots` landmine (3 starts not 1), mutated expected_slot_starts fails, source excludes inbound/composio. Live model router and `docs/MODEL_ROUTING_DECISION.md` still correctly missing.

Key files: `app/evals/datasets/calendar_v1.json`, `app/evals/harness.py`, `app/evals/__init__.py`, `tests/unit/test_evals.py`, `docs/MODEL_BENCHMARK_PLAN.md`.

### Graph Lab objection frozen set (Adjustment J) — **alive** (20/20; no router)

`app/evals/datasets/objection_v1.json` — exactly 20 Hebrew+English extract→NBA→reply cases (6 objection kinds first-move + reframe, poor/stop/owner landmines beating price, noop deepen_pain, price-wins enum order). `run_objection_eval()` in `app/evals/harness.py` scores `extract_sales_signals` → `select_next_action` → `reply_for` + `lint_customer_reply`; pass iff `expected_objection`, `expected_action`, reply substring, and lint; `quality=None`. No LLM judge, LangSmith, or DB. `tests/unit/test_evals.py` — 20/20, unique case_ids, ≥6 Hebrew + ≥6 English-only, PII lint (`@`/`972`/`+972`), mutated expected_objection fails. Live model router and `docs/MODEL_ROUTING_DECISION.md` still correctly missing.

Key files: `app/evals/datasets/objection_v1.json`, `app/evals/harness.py`, `app/evals/__init__.py`, `tests/unit/test_evals.py`, `docs/MODEL_BENCHMARK_PLAN.md`.

### Graph Lab sales frozen set (Adjustment J) — **alive** (50/50; no router)

`app/evals/datasets/sales_v1.json` — exactly 50 one-shot NBA+reply fixtures (11 original byte-stable + 39 new unique case_ids covering full `select_next_action` priority ladder, MEDDPICC-lite qualify variants, meeting/handoff/stop/disqualify branches, first-move and reframe objection copy). `run_sales_eval()` in `app/evals/harness.py` scores `select_next_action` + `reply_for` + `lint_customer_reply`; §39.3 Sales Quality Score 100.0 on pass. No extract, mark, LLM judge, LangSmith, or DB. `tests/unit/test_evals.py` — 50/50, unique case_ids, mutated expected_action fails. Live model router and `docs/MODEL_ROUTING_DECISION.md` still correctly missing.

Key files: `app/evals/datasets/sales_v1.json`, `app/evals/harness.py`, `tests/unit/test_evals.py`, `docs/MODEL_BENCHMARK_PLAN.md`.

### Graph Lab routing frozen set (Adjustment J) — **alive** (20/20; no router)

`app/evals/datasets/routing_v1.json` — exactly 20 isolated Hebrew+English owner classify cases (exclusive matchers + keyword landmines: lead_review with/without `lead_*`, content_idea, gmail_summary clarification, calendar EN+HE, owner_notify, meeting_brief, campaign pause+id vs keyword approval, takeover/resume, preference, analytics, research, linkedin, daily/weekly brief, unclassifiable note, two-type clash). `run_routing_eval()` in `app/evals/harness.py` replays `classify_owner_task` only; pass iff `task_type` + `needs_clarification` match; `CaseResult.reply=""`; `quality=None`. No NBA, reply, LLM judge, LangSmith, or DB. `tests/unit/test_evals.py` — 20/20, unique case_ids, ≥6 Hebrew + ≥6 English-only, PII lint (`@`/`972`/`+972`), mutated expected_type fails. Live model router and `docs/MODEL_ROUTING_DECISION.md` still correctly missing.

Key files: `app/evals/datasets/routing_v1.json`, `app/evals/harness.py`, `app/evals/__init__.py`, `tests/unit/test_evals.py`, `docs/MODEL_BENCHMARK_PLAN.md`.

### Graph Lab extract frozen set (Adjustment J) — **alive** (30/30; no router)

`app/evals/datasets/extract_v1.json` — exactly 30 isolated Hebrew+English extract cases (workflow, pain, impact/P3, P4, P5, meet, fit, stop, poor, authority, timeline, owner_required, 5 objection kinds, company_domain, no-op short/long). `run_extract_eval()` in `app/evals/harness.py` replays `SalesState` → `extract_sales_signals` → `_sales_field_matches` on all `expect` keys; `CaseResult.expected_action=actual_action=extract` on pass; `quality=None`. No NBA, reply, LLM judge, LangSmith, or DB. `tests/unit/test_evals.py` — 30/30, unique case_ids, ≥12 Hebrew + ≥12 English-only, PII lint (`@`/`972`/`+972`), mutated expect fails. Live model router and `docs/MODEL_ROUTING_DECISION.md` still correctly missing.

Key files: `app/evals/datasets/extract_v1.json`, `app/evals/harness.py`, `tests/unit/test_evals.py`, `docs/MODEL_BENCHMARK_PLAN.md`.

### calendar_availability freshness on offer + owner read (Adjustment N) — **alive** (audit only; ack unchanged)

`prepare_meeting_offer` and `apply_owner_calendar` stamp `calendar_availability` via `calendar_availability_outcome` (`stamp_freshness` + `overlay_stale`). Policy slots present → `ToolOutcome.freshness=live`, status `ok`; empty/HTTP fail → `unverified`; kill-switch denied → `""`. Persist on `tool_runs.freshness`. Canonical TOOL_RESULT payload still `{tool, status, result_count}` only. Customer ack unchanged. RAG missing. Adjustment N still **partially complete** (versioned knowledge RAG missing; live/short-cache retrieval stamps are wired).

Key files: `app/integrations/calendar.py`, `app/domain/owner_calendar.py`, `tests/unit/test_tool_freshness_calendar.py`.

### gmail_results freshness on Composio hydrate (Adjustment N) — **alive** (audit only; ack unchanged)

Empty-body Composio trigger calls `gmail_port` via `hydrate_gmail_item`; `gmail_results_outcome` stamps `gmail_results` (`cached` when fetch present; `unverified` when disabled/none). Tool allowlist `gmail_fetch`; persisted on `tool_runs.freshness` after `process_inbound_texts`. Non-empty trigger body skips port + stamp. Owner Gmail summary (Postgres `message_in`) not stamped. Canonical TOOL_RESULT payload still `{tool, status, result_count}` only.

Key files: `app/integrations/gmail.py`, `app/api/composio.py`, `tests/unit/test_tool_freshness_gmail.py`.

### opt_out_status freshness on conversation_killed change (Adjustment N) — **alive** (audit only; ack unchanged)

`apply_conversation_kill_policy` returns `ToolOutcome | None` when `leads.conversation_killed` changes (NBA `stop` vs recover); `opt_out_status_outcome` stamps `live`. No stamp on unchanged turns or PolicyDenied. Wired on website + prospect inbound; owner path excluded. Customer reply unchanged.

Key files: `app/domain/conversation_kill.py`, `app/api/website.py`, `app/api/inbound.py`, `tests/unit/test_tool_freshness_opt_out.py`.

### campaign_metrics freshness on Meta enrich (Adjustment N) — **alive** (audit only; ack unchanged)

`enrich_analytics_ack` stamps `campaign_metrics` via `stamp_freshness` + `overlay_stale`. Present live fetch → `ToolOutcome.freshness=cached` (short_cache ttl 300, age 0); empty/HTTP fail → `unverified`; kill-switch denied → `""`. Persist on `tool_runs.freshness`. Canonical TOOL_RESULT payload still `{tool, status, result_count}` only. Customer ack unchanged. Calendar availability now wired on offer + owner read. RAG missing. Operator: `migrations/20260821_tool_run_freshness.sql`.

### Approval object fields (Adjustment M) — **alive** (persist-only; execute gated)

`ApprovalRow` gains `approval_id` (`apr_`+12 hex; unique; first-write-wins), `proposed_parameters` (compact identity JSON; 255 cap), `approved_at` (approve only), and reserved-empty `business_id`/`actor_id`/`executed_at`/`execution_operation_id`/`result`. Helpers `new_approval_id()` and `proposed_parameters_json()` in `app/domain/approvals.py` share `_identity_blob` with `payload_hash`. Store upsert/decide wired; `decide_*` accepts `now` for tests. Execute still gated; `named_write_may_auto` R4 False.

Operator: `migrations/20260821_approval_object_fields.sql` on existing Postgres/file sqlite DBs.

Key files: `app/db/models.py`, `app/domain/approvals.py`, `app/db/store.py`, `tests/unit/test_approvals.py`.

### LinkedIn analytics `partial` ToolOutcome (Adjustment G) — **alive** (status only; ack unchanged)

`enrich_linkedin_analytics_ack` stamps `ToolOutcome.status=partial` when the Hebrew stats line is non-empty but fewer than six allowlisted metrics populated; full six stay `ok`; HTTP/denied/empty paths unchanged. Customer ack still omits missing metrics. `stale` remains unused (no cache).

### Webhook received in-flight TTL reclaim (Adjustment E) — **alive** (claim path only; reconciliation flag-only)

`LeadStore.claim_webhook` reclaims stuck in-flight `received` rows after reconciliation `STALE_AFTER_SECONDS` (300): empty/unparseable `claimed_at` or age >300s → refresh `claimed_at` and return True; fresh `received` → False; `failed`→`received` retry unchanged; `processed`/`sent` always False even with old `claimed_at`. Reuses `is_stale_received` from `app/domain/reconciliation.py`; reconciliation worker stays flag-only (never repairs). Powertools **DEFER**.

Key files: `app/db/store.py`, `tests/unit/test_idempotency.py`.

### ManyChat ad/campaign attribution (Adjustment B) — **alive** (sanitize-only; no send)

`parse_manychat_item` maps optional `ad_id`/`campaign_id`/`post_id` (and aliases) through `sanitize_instagram_attribution`. `meta_campaign_id` is on the Instagram allowlist. Canonical ATTRIBUTION uses the existing Instagram persist path. Names, media URLs, and ad titles are dropped. No ManyChat Public API send; one-sender rule unchanged.

### Freshness policy registry (Adjustment N) — **partially complete** (Meta + calendar + gmail + opt-out wired)

Nineteen allowlisted facts pin `FreshnessClass`, `source`, `ttl_seconds`, and `version` in `app/domain/policies/freshness.py`. `freshness_pin()` lookup-only; unknown → fail-closed `live_only`/`none`. `stamp_freshness()` + `overlay_stale()` return frozen `FreshnessStamp` with `live`/`cached`/`stale`/`unverified` status; versioned knowledge always `unverified` (no RAG). Capability `freshness_policy` ALIVE; pinned `_det(..., R0_READ)`. **`campaign_metrics` stamp wired** on Meta insights enrich (`enrich_analytics_ack`); **`instagram_content_metrics` stamp wired** on organic IG insights enrich (`enrich_content_insights_ack`); **`linkedin_content_metrics` stamp wired** on member analytics enrich (`enrich_linkedin_analytics_ack`); **`linkedin_profile` stamp wired** on profile enrich (`enrich_linkedin_ack`); **`research_snippets` stamp wired** on public search enrich (`enrich_research_ack`); **`calendar_availability` stamp wired** on offer + owner read; **`gmail_results` stamp wired** on Composio empty-body hydrate (`gmail_fetch`); **`opt_out_status` stamp wired** on `conversation_killed` change; persisted on `tool_runs.freshness` (audit only; customer ack unchanged). RAG still missing.

Key files: `app/domain/policies/freshness.py`, `app/integrations/meta_ads.py`, `app/integrations/calendar.py`, `app/domain/owner_calendar.py`, `app/integrations/gmail.py`, `app/domain/conversation_kill.py`, `app/domain/tools.py`, `tests/unit/test_tool_freshness_meta.py`, `tests/unit/test_tool_freshness_calendar.py`, `tests/unit/test_tool_freshness_gmail.py`, `tests/unit/test_tool_freshness_opt_out.py`, `tests/unit/test_freshness.py`. Operator: `migrations/20260821_tool_run_freshness.sql`.

### Model task-class registry (Adjustment J) — **alive** (lookup only; not a live router)

Eleven typed task classes pin current owners in `app/domain/policies/task_classes.py`. `task_class_pin()` is lookup-only; unknown → `code`/`none`. Not wired into inbound or the sales graph. No production model ids. Frozen scored sets: `routing_v1` 20 + `extract_v1` 30. `docs/MODEL_ROUTING_DECISION.md` still correctly missing.

### tool_runs.latency_ms from port wall-clock (Adjustment I) — **alive** (metadata only)

`ToolOutcome.latency_ms` (clamped 0–86_400_000) measured around adapter HTTP/search/transcribe **and** sales-tab / session-tab / campaign-tab / content-tab Sheets upserts after claim. `persist_tool_outcome` uses explicit kwarg when non-zero, else the outcome field. Canonical TOOL_RESULT payload unchanged. Denied-before-call enrich paths stay 0. `cost_usd` still 0. Tests: `test_enrich_research_ack_measures_port_latency`, `test_persist_tool_outcome_uses_outcome_latency_ms`, `test_sheets_mirror_outcome_stamps_latency`, `test_website_sheets_mirror_tool_run_latency`, `test_inbound_sheets_mirror_tool_run_latency`, `test_website_session_create_persists_sheets_mirror_tool_run`, `test_owner_analytics_inbound_persists_campaign_mirror_tool_run`, `test_owner_analytics_inbound_persists_content_mirror_tool_run`.

### Webpage-scrape adversarial suite (Adjustment L / Gate A) — **alive** (in-process fakes)

`sanitize_snippets` drops non-https, empty host, `localhost`, and IP literals; titles/excerpts collapse CR/LF/tab before truncate. Owner ack stays title + host. Firecrawl HTTP ≥400 / transport raises `AdapterHttpError`; enrich/meeting-research stamp classified status without changing the ack. Suite: `tests/unit/test_webpage_scrape_adversarial.py`. Meta write replay still gated.

### ToolOutcome HTTP result classification (Adjustment G) — **partially complete**

`ToolOutcome.status` expanded: `ok|denied|empty|error|unauthorized|rate_limited|malformed|retryable|partial|stale` (`app/domain/tools.py`); `ok` remains success (no `success`). `AdapterHttpError` + `tool_status_from_http` classify HTTP (401/403→unauthorized, 429→rate_limited, 400/422→malformed, 5xx/transport→retryable, fail-closed on 200). Wired on Meta insights, LinkedIn member analytics, LinkedIn profile, Calendar free/busy, Gmail fetch, Instagram media-list, and research; enrich/hydrate paths (`enrich_analytics_ack`, `enrich_linkedin_analytics_ack`, `enrich_linkedin_ack`, `enrich_content_insights_ack`, `prepare_meeting_offer`, `apply_owner_calendar`, Composio empty-body Gmail hydrate) catch `AdapterHttpError` and stamp classified status without changing customer-facing acks (no insights/slots/profile/content line appended on HTTP fail). `partial` is used on LinkedIn member analytics when some metrics populate and others are omitted (`enrich_linkedin_analytics_ack`; ack line unchanged). `stale` unused (no freshness clock on the live tool path). No CampaignGateway apply_change.

Tests: `tests/unit/test_tool_status.py`, updates in `test_meta_ads.py`, `test_linkedin_analytics.py`, `test_calendar.py`.

### Voice transcript STT metadata (Adjustment K) — **row complete**; second-provider still **Missing**

See just-finished. `TranscriptResult` includes `confidence`. `save_transcript` stamps `cost_usd=0` and `retention_status=text_only`. Tests: `tests/unit/test_whatsapp_stt.py`.

### Preloaded tool pin registry (Adjustment F Tier 2) — **partially complete** (catalog only; discovery off)

Frozen `PreloadedTool` catalog in `app/tools/registries/mia_preloaded_tools.py`: re-exports Composio/direct pin names and versions from existing adapter constants (Gmail fetch + trigger, Calendar read/write pins, Sheets upsert, Meta insights, LinkedIn profile, direct `member_post_analytics`); `preloaded_tool(name)` lookup; no Composio catalog discovery function; `MIA_DYNAMIC_TOOL_DISCOVERY` remains unused/false; customer graph still has zero Composio tools. Capability `preloaded_tools` ALIVE; execution policy `_det(..., R0_READ)`. Tests: `tests/unit/test_preloaded_tools.py`.

### ManyChat event_id + conversation ids + ad attribution (Adjustment B) — **partially complete**

`parse_manychat_item` requires non-empty `event_id`; payloads without it return `None` (webhook `processed: 0`). Parsed inbound item includes `subscriber_id`, `conversation_id`, and `thread_id` (conversation id from payload or fallback to subscriber). `id` is provider `event_id` only — no synthesized `from:last_interaction:text` fallback. Optional ManyChat payload fields (`ad_id`, `campaign_id`, `post_id`, content/ref/trigger aliases) map through `sanitize_instagram_attribution` onto the inbound item; canonical ATTRIBUTION persists on the same Instagram path as Graph inbound (`meta_campaign_id` added to allowlist). Names, media URLs, and ad titles are never copied. `DisabledMessagePort` unchanged; no IG send via ManyChat Public API; dual-send / Meta routing still blocked.

Tests: `tests/unit/test_manychat.py`, `tests/fixtures/manychat/external_request_ad.json`.

### Ingress correlation_id + payload_version (Adjustment D) — **partially complete** (CanonicalEvent fields; no SQS envelope)

`CanonicalEvent.correlation_id` + `canonical_events.correlation_id` (`String(64)` default `""`). Helpers `new_correlation_id`, `sanitize_correlation_id`, `stamp_correlation` in `app/domain/events.py`. Prospect/website inbound reuse sales `run_id` (generated before MESSAGE_IN); owner inbound uses `cor_*` once per claimed item. Graph `_persist_canonical_event` stamps QUALIFICATION/HANDOFF/MEETING_OFFERED from inbound `run_id`. `persist_tool_outcome(..., correlation_id=)` stamps TOOL_RESULT when non-empty. Not in payload; session LEAD_CREATED may stay empty this slice.

`CanonicalEvent.payload_version` + `canonical_events.payload_version` (`String(8)` default `""`). Allowlist `"1"`; stamped in `save_canonical_event` only; first-write-wins; not in payload. No `business_id`. No parallel `InboundEvent`.

Operator: `migrations/20260821_canonical_correlation_id.sql` + `migrations/20260821_canonical_payload_version.sql`. Tests: `tests/unit/test_correlation.py`, `tests/unit/test_events.py`.

### IdempotencyStore (Adjustment E) — **alive** (Protocol + table + webhook TTL reclaim; Powertools DEFER)

`IdempotencyStore` Protocol in `app/domain/idempotency.py`: `LeadStore.claim_webhook` reclaims stale in-flight `received` via `is_stale_received`/`STALE_AFTER_SECONDS` (300s); `claim_operation(scope, key)` with in-flight TTL + `complete_operation`/`fail_operation`/`get_operation_result` on `idempotency_records`. Scopes: `calendar_create`, `calendar_reschedule`, `canonical`, `approval`, `owner_task`, `sheets_mirror`, `follow_up`. Wired on meeting booked persist (`calendar_create` / `{lead_id}:booked` → `complete_operation` `{"ok": true}`), verified reschedule persist (`calendar_reschedule` / `{lead_id}:rescheduled:{target_key}`), approval persist, owner task persist (`owner_task` / `{provider}:{provider_event_id}` around `save_owner_task` only; execute still gated), Sheets mirror persist (`sheets_mirror` / `{inbound_id}:sheets:{sales|session|campaign|content}` — inbound key, not lead), and follow-up persist (`follow_up` / `{inbound_id}:followup` — inbound key, not lead; due-scan/booking-cancel/send unwired). Capability `fde_idempotency` ALIVE.

Operator: `migrations/20260821_idempotency_records.sql` + `migrations/20260821_idempotency_inflight.sql`.

### R3/R4 approval binding (Adjustment M) — **alive** (persist-only; no execute)

Lead: `approvals.resource_type=lead`, `resource_id=lead_id`, `expires_at` +24h. Campaign: `resource_type=campaign`, `resource_id={campaign_id}`, `lead_id=NULL`, action `campaign_write`, risk R4. Hash binds action+risk+channel+resource. Unique `(resource_type, resource_id, action)`. `approval_id`/`proposed_parameters`/`approved_at` on object fields slice; execute columns reserved empty. Expired/unbound decide returns `expired`/`unbound` without mutating the row. Owner WhatsApp campaign phrases (`pause campaign`, `approve campaign`, Hebrew equivalents) queue/decide persist-only — never Meta.

Operator: `migrations/20260821_approval_binding.sql` + `migrations/20260821_approval_campaign_resource.sql` + `migrations/20260821_approval_object_fields.sql`.

### Named write flags (Adjustment R) — **partially complete** (calendar create + reschedule PATCH gated; others fail-closed unused)

Seven bool settings in `app/core/config.py` (`MIA_CALENDAR_WRITE`, `MIA_AUTO_FOLLOWUP`, `MIA_GMAIL_SEND`, `MIA_META_WRITE`, `MIA_DYNAMIC_TOOL_DISCOVERY`, `MIA_BROWSER_AUTOMATION`, `MIA_AUTO_REPLY_INSTAGRAM`); all default **false**. Helper `app/core/write_flags.py`: `named_write_may_auto` (R4/R5 always False) and allowlisted `write_flag_enabled`. **Wired:** `calendar_create` in `app/domain/calendar_booking.py` and `calendar_patch_event` in `app/domain/meeting_changes.py` after kill switch / before provider write; reads not gated. **Not wired:** Gmail send, Meta writes, follow-up send, dynamic Composio discovery, browser, IG auto-reply (SHADOW still owns prospect send). `decide()` unchanged. Tests: `tests/conftest.py` sets `MIA_CALENDAR_WRITE=true`; `tests/unit/test_write_flags.py`.

Operator: set `MIA_CALENDAR_WRITE=true` in `.env` for local live booking/reschedule PATCH. Flags do not override R4/R5 or kill switch.

### FDE human takeover state — **alive** (persist + skip prospect MessagePort; owner resume clears flag; website HTTP unchanged)

Owner WhatsApp exclusive takeover phrases (`human takeover`, `take over this lead`, `אני לוקח את הליד`, `תפיסה אנושית`) + `lead_*` persist `leads.human_takeover=true` via `apply_owner_human_takeover`; exclusive resume phrases (`resume this lead`, `release this lead`, `mia can reply`, `שחרר את הליד`, `החזר למיאה`) + `lead_*` persist `human_takeover=false` via `apply_owner_human_resume` (`app/domain/takeover.py`; R1 `human_takeover_persist`; kill switch format-only ack without persist; demo skips apply). Prospect WhatsApp/Instagram inbound still runs graph + `ai_runs` but skips `MessagePort.send` when `human_takeover` (`app/core/outbound.py`; owner acks still send). Follow-up send-readiness denies `human_takeover`. Distinct from `conversation_killed` (sales NBA `stop`). Website HTTP replies unchanged (same as SHADOW).

Key files: `app/domain/takeover.py`, `app/domain/owner_tasks.py`, `app/core/outbound.py`, `app/api/inbound.py`, `app/domain/followups.py`, `app/db/models.py`, `app/db/store.py`, `migrations/20260821_human_takeover.sql`, `tests/unit/test_takeover.py`. Capability `fde_human_takeover` ALIVE; pinned `_det(..., R1_LOW_WRITE)` in execution-policy registry.

Operator: run `migrations/20260821_human_takeover.sql` on existing Postgres/file sqlite DBs.

Next FDE unit: none required in sequence — unit 9 (`risk_gate` graph node) **DEFER**.

### Model task-class registry — **alive** (lookup only; not a live router)

`TaskClass` StrEnum + frozen `TaskClassPin` in `app/domain/policies/task_classes.py`: eleven Adjustment J classes pin current owner + `model_source` token (`none`|`env`|`canned`; never a brand/model id). `task_class_pin()` lookup only; unknown name → fail-closed `code`/`none`. Capability `model_task_classes` ALIVE; pinned `_det(..., R0_READ)` in execution-policy registry. **Not wired into inbound/graph** — no runtime model selection.

Key files: `app/domain/policies/task_classes.py`, `app/core/capabilities.py`, `tests/unit/test_task_classes.py`. Frozen scored sets: `routing_v1` 20 + `extract_v1` 30. `docs/MODEL_ROUTING_DECISION.md` still missing (correct).

### FDE node failure policy registry — **alive** (lookup only; adapters unchanged)

`NodeFailurePolicy` registry in `app/domain/policies/failure_policy.py`: pins timeout, retries, fail_closed, fallback token, and notify_owner per allowlisted tool name from `ALLOWLISTED_TOOLS` plus node aliases `sales_reply` (LLM malformed → fallback then canned) and `meta_write` (no adapter; gated). `failure_policy_for` lookup only; unknown node → fail_closed, retries 0, timeout 0, fallback `omit`. Reuses `ToolOutcome` statuses conceptually; does not replace `ToolOutcome`. Capability `fde_failure_policy` ALIVE; pinned `_det(..., R0_READ)` in execution-policy registry. **Not wired into adapters** — no new retry loops, no LangGraph subgraphs.

Key files: `app/domain/policies/failure_policy.py`, `app/core/capabilities.py`, `tests/unit/test_failure_policy.py`.

Better-Way: **KEEP** one sales node; FDE unit 9 (`risk_gate` graph node) **DEFER** — unit 2 pure-function tests do not require a second graph node.

Next FDE unit: human takeover state (sequence row 10; pre-prod + FDE `human_handoff`). Do not wire failure policy into adapters in the same slice.

### FDE business value count events — **alive** (counts only; ILS empty; no deal won/minutes)

Allowlisted canonical `business_value` events via `app/domain/value.py`: kinds `qualified` (fit→`good` transition), `handoff` (graph handoff), `booked` (after `MEETING_BOOKED`), `recovered` (follow-up recovered). Idempotency `{lead_id}:value:{kind}`; payload `kind` + `estimated_value_ils` always `""`. R1 `business_value_persist`; `count_business_value` requires `lead_id` and filters by event type + kind. **Not** in `COUNTABLE_EVENT_TYPES` (weekly KPI unchanged). Existing timeline events (`QUALIFICATION_UPDATED`, `HANDOFF`, `MEETING_BOOKED`, `FOLLOW_UP`) still fire.

Key files: `app/domain/value.py`, `app/domain/events.py`, `app/graph/orchestrator.py`, `app/domain/calendar_booking.py`, `app/domain/followups.py`, `tests/unit/test_value.py`. Capability `fde_value` ALIVE; pinned `_det(..., R1_LOW_WRITE)` in execution-policy registry.

Next FDE unit: human takeover state (`docs/FDE_OPERATING_LAYER_GAP.md` sequence row 10). FDE unit 9 (`risk_gate` graph node) **DEFER** — unit 2 tests pass without it; KEEP one sales node.

### FDE idempotency store — **alive** (Protocol + table; Powertools DEFER)

`IdempotencyStore` Protocol in `app/domain/idempotency.py` wraps existing first-write-wins keys: `LeadStore.claim_webhook` (failed→received retry; stale `received` reclaim via `is_stale_received`/`STALE_AFTER_SECONDS`; `processed`/`sent` unique) and `claim_operation(scope, key)` with in-flight TTL + `complete_operation`/`fail_operation`/`get_operation_result` on Postgres `idempotency_records` (unique `(scope, key)`). Allowlisted generic scopes: `calendar_create`, `calendar_reschedule`, `canonical`, `approval`, `owner_task`, `sheets_mirror`, `follow_up` — `webhook` stays on `claim_webhook`. Wired: `_persist_meeting_booked_event` calls `claim_operation(scope=calendar_create, key={lead_id}:booked)` before canonical + business value, then `complete_operation` `{"ok": true}`; duplicate persist skips both; Sheets mirror persist uses `{inbound_id}:sheets:{tab}` (not lead) so webhook retry skips a second upsert while a later inbound still refreshes the snapshot; lost-response reread via `get_operation_result`. Lambda Powertools **DEFER**.

Key files: `app/domain/idempotency.py`, `app/db/models.py`, `app/db/store.py`, `app/domain/calendar_booking.py`, `migrations/20260821_idempotency_records.sql`, `migrations/20260821_idempotency_inflight.sql`, `tests/unit/test_idempotency.py`. Capability `fde_idempotency` ALIVE; pinned `_det(..., R1_LOW_WRITE)`.

Operator: run `migrations/20260821_idempotency_records.sql` and `migrations/20260821_idempotency_inflight.sql` on existing Postgres/file sqlite DBs.

### FDE gold replay (`mia_sales_gold`) — **alive** (synthetic Bible-shaped; hidden-truth scorer; no private transcripts)

`app/evals/datasets/mia_sales_gold.jsonl` — exactly 20 JSONL cases covering all `NextAction` branches (cold open, discovery, quantify, reflect, hypothesis, qualify variants, meeting, handoff, stop, disqualify, objections including reframe). `run_gold_eval()` in `app/evals/harness.py` scores action + reply substring + `lint_customer_reply` + hidden-truth flags (`must_ask_workflow`, `must_not_pitch`, `must_not_invent_roi`) without a model judge. User-turn cases run `extract_sales_signals` then NBA; sales-only cases mirror `run_sales_eval`. `tests/unit/test_sales_gold.py` — 20/20 pass, PII lint on dataset, synthetic fail case.

Key files: `app/evals/datasets/mia_sales_gold.jsonl`, `app/evals/harness.py`, `tests/unit/test_sales_gold.py`. Does not replace `sales_v1` / `buyers_v1` / `writing_v1`. No graph/prompt mutation.

### FDE owner correction persist — **alive** (persist-only; no remember-ask; activation gated)

Owner WhatsApp PREFERENCE inbound with `InstructionKind.CORRECTION` persists a logged row in `owner_corrections` (`app/domain/feedback.py`; R1 `owner_correction_persist`; scope `this_turn` or `remember` from phrase classify; body truncated 2000; first write wins on `(provider, provider_event_id)`; status always `logged`). Still calls `propose_owner_instruction` as before. No Hebrew remember-ask. No activation. No prompt rewrite. Prospect inbound with correction phrasing does not write corrections.

Key files: `app/domain/feedback.py`, `app/api/inbound.py`, `app/db/models.py`, `app/db/store.py`, `migrations/20260821_owner_corrections.sql`, `tests/unit/test_feedback.py`. Capability `fde_feedback` ALIVE; pinned `_det(..., R1_LOW_WRITE)` in execution-policy registry.

Operator: run `migrations/20260821_owner_corrections.sql` on existing Postgres/file sqlite DBs.

Next FDE unit: `mia_sales_gold.jsonl` gold set (`docs/FDE_OPERATING_LAYER_GAP.md` sequence row 6). Do not wire `risk_gate` into LangGraph in the same slice.

### FDE shadow mode (AutomationMode) — **alive** (prospect MessagePort only; website HTTP still replies)

`MIA_AUTOMATION_MODE` in `app/core/config.py` (`AutomationMode` enum; **production default `shadow`**). Under SHADOW, prospect inbound runs the full graph + `ai_runs` + follow-up persist but skips `MessagePort.send` and writes `shadow_decisions` (metadata + proposed reply truncated 4000; `policy_version=fde_v1`; first write wins on `run_id`; no inbound text). Owner WhatsApp acks still send (`actor_role=owner`). Website HTTP replies unchanged. Does not override R4/R5 or kill switch. HYBRID not wired. **`tests/conftest.py` sets `MIA_AUTOMATION_MODE=auto_approved`** so existing tests keep sending.

Key files: `app/domain/shadow.py`, `app/core/outbound.py`, `app/api/inbound.py`, `app/db/models.py`, `app/db/store.py`, `migrations/20260821_shadow_decisions.sql`, `tests/unit/test_shadow.py`.

Operator: run `migrations/20260821_shadow_decisions.sql` on existing Postgres/file sqlite DBs. Production default is SHADOW (no prospect WhatsApp/IG send); tests default AUTO_APPROVED.

Next FDE unit: `mia_sales_gold.jsonl` gold set (`docs/FDE_OPERATING_LAYER_GAP.md` sequence row 6). Do not wire `risk_gate` into LangGraph in the same slice.

### FDE ai_runs policy_version — **alive** (metadata only; not wired into graph)

Every new `ai_runs` row from `persist_ai_run` stamps `policy_version=fde_v1` from `POLICY_VERSION` in `app/domain/policies/execution_policy.py` (ActionPolicy registry pin; bump only when registry pins change). Allowlisted `automation_mode` from settings (`off|draft_only|shadow|hybrid|auto_approved`; invalid → `""`; first-write-wins). Frozen `prompt_version=sales_reply_v1` from `PROMPT_VERSION` beside the sales-reply system prompt (domain duplicates the pin; bump both + frozen hash when `_SYSTEM_PROMPT` changes). `LeadStore.save_ai_run` accepts `policy_version`, `automation_mode`, `prompt_version`, and `decision_confidence`; duplicate `run_id` first-write-wins unchanged. No prompt/reply/latest_message; `tokens_in`/`tokens_out` from graph compose usage (OpenAI on successful live rewrite; canned/fallback 0); `cost_usd` 0; `decision_confidence="1.0"` pinned from `DETERMINISTIC_NBA_CONFIDENCE` (no LLM self-score). Storing `hybrid` is audit-only — HYBRID send is not wired. Owner/Graph Lab still do not write `ai_runs`. Website + prospect inbound pass `tokens_in`/`tokens_out` from graph result. Existing Postgres/file sqlite DBs need `policy_version` (`migrations/20260821_ai_run_policy_version.sql`), `automation_mode` (`migrations/20260822_ai_run_automation_mode.sql`), `prompt_version` (`migrations/20260822_ai_run_prompt_version.sql`), and `decision_confidence` (`migrations/20260822_ai_run_decision_confidence.sql`) on `ai_runs`.

### FDE ai_runs latency_ms — **alive** (metadata only; graph.invoke wall-clock)

Every website and prospect inbound sales graph invoke measures wall-clock `latency_ms` via `perf_counter` + `elapsed_ms` in `app/domain/ai_runs.py` and persists on `ai_runs` (clamped 0–86_400_000; negative coerced to 0). `tokens_in`/`tokens_out` parsed from OpenAI `usage` when live compose succeeds (`ComposeResult` on `SalesReplyPort`; canned/fallback/kill-switch 0); `cost_usd` 0. Migration: `migrations/20260821_ai_run_latency_ms.sql`.

### FDE tool_runs latency_ms — **alive** (port wall-clock; metadata only)

Allowlisted enrich paths stamp `ToolOutcome.latency_ms` via `perf_counter` + `elapsed_ms` around port HTTP/search/transcribe: `enrich_research_ack`, `_run_meeting_research`, primary `port.get_insights()` in `enrich_analytics_ack`, `enrich_linkedin_analytics_ack`, `enrich_linkedin_ack`, `enrich_content_insights_ack`, `prepare_meeting_offer` / `apply_owner_calendar` `find_free_slots`, calendar booking create/verify/reschedule verify HTTP, WhatsApp STT transcribe. Sales-tab and session-tab Sheets claims stamp wall-clock via `sheets_mirror_outcome` (keys `{inbound_id}:tool:sheets_mirror` / `{session_id}:tool:sheets_mirror`). Campaign/content claims stamp via `sheets_tab_mirror_outcome` (keys `{inbound_id}:tool:sheets_mirror_campaign` / `{inbound_id}:tool:sheets_mirror_content`). `persist_tool_outcome` uses explicit kwarg when non-zero else `outcome.latency_ms`. Canonical TOOL_RESULT payload unchanged (`tool`, `status`, `result_count` only). Denied-before-call enrich stays 0; `cost_usd` 0.

Key files: `app/domain/tools.py`, `app/domain/events.py`, enrich modules under `app/integrations/` and `app/domain/owner_calendar.py`, `app/api/whatsapp.py`, `tests/unit/test_research.py`, `tests/unit/test_ai_runs.py`.

Next FDE unit: `mia_sales_gold.jsonl` gold set (`docs/FDE_OPERATING_LAYER_GAP.md` sequence row 6). Do not implement it in the same slice as shadow.

### FDE decision policy — **alive** (pure functions; not wired into graph)

`AgentDecision` + `route_decision` / `risk_gate` in `app/domain/policies/decision.py` wrap deterministic NBA `action` + `reply` via `decision_from_sales`. Routes: `EXECUTE`, `HUMAN_REVIEW`, `APPROVAL`, `HUMAN_HANDOFF`, `ASK_CLARIFICATION` — first-match over `ActionPolicy` from unit 1. Confidence pinned `1.0` for deterministic NBA; lint reasons do not lower confidence. Handoff → `requires_human` + `approval_required`; `HUMAN_ONLY` policy wins before approval flag. No graph/inbound/outbound change; no `assert_allowed`; `fail_closed` on policy does not alter route (execute still needs write gate later). Graph still `START → sales_next_action → END`.

Key files: `app/domain/policies/decision.py`, `app/domain/policies/__init__.py`, `tests/unit/test_decision_policy.py`.

### FDE execution policy registry — **alive** (lookup only; not wired into graph)

`ExecutionMode` + `ActionPolicy` registry in `app/domain/policies/execution_policy.py` wraps existing `RiskLevel` per `CapabilityId`. `policy_for` lookup only; unknown capabilities → `HUMAN_ONLY` + R5 + fail_closed. Pins: `identity`/`sales_state`/`langgraph`/`meta_ads` DETERMINISTIC; `sales_reply`/`voice_stt`/`gmail_summary` AI_AUTOMATIC; `approvals`/`aws_runtime` HUMAN_ONLY. No graph/inbound/outbound change; R4/R5 unchanged. Capability `fde_execution_policy` ALIVE. Next FDE unit: `AgentDecision` + `route_decision` pure functions (item 2 in `docs/FDE_OPERATING_LAYER_GAP.md`).

Key files: `app/domain/policies/execution_policy.py`, `app/core/capabilities.py`, `tests/unit/test_execution_policy.py`.

### FDE operating layer — **audit only** (no application code beyond unit 1)

Assaf asked for an FDE operating layer (execution policy, `AgentDecision` + risk_gate, SHADOW mode, owner corrections, gold replay, business value events, node failure policy). Not RAG. Not a Mia rebuild. Map: `docs/FDE_OPERATING_LAYER_GAP.md`. Unit 1 (`ExecutionMode` + `ActionPolicy`) is **alive**. Next code unit: `AgentDecision` + `route_decision` pure functions. Do not confuse SHADOW with `MIA_DEMO_MODE`. Do not infer deal ILS. Do not replace `select_next_action`. Complementary to `docs/PRE_PRODUCTION_GAP_REPORT.md` Phase 2 (flags, human takeover, approval binding) — do not duplicate those as a second brain.

### Meeting debrief next_step classify — **alive** (persist-only)

Owner post-meeting debrief now classifies allowlisted `next_step` (`none`/`follow_up`/`proposal`) from deterministic bilingual phrases via `parse_debrief_next_step` in `app/domain/debriefs.py`. Row upsert latest next_step wins; canonical `MEETING_DEBRIEF` first write wins. Still no deal stage/value change, no follow-up upsert, no calendar create, no send, no Sheets debrief dump; `estimated_value`/`notes` stay empty.

Key files: `app/domain/debriefs.py`, `app/db/store.py`, `app/domain/events.py`, `tests/unit/test_debriefs.py`.

### Owner instruction kinds on propose — **alive** (propose-only)

Owner WhatsApp messages already classified as `OwnerTaskType.PREFERENCE` persist `owner_instructions.kind` via deterministic `classify_instruction_kind` in `app/domain/learning.py`: `correction` (that's wrong / correction / זה לא נכון), `behavior_rule` (never say / always say / אל תגידי / תמיד תגידי), else `preference`. First match wins; `fact` not this slice. Still `status=proposed` only; `list_active_instructions()` empty; no prompt append; no activation. Kind-specific Hebrew acks in `ack_for_owner_task` (`text=` from inbound `owner_text`). Sales graph must not import learning.

Key files: `app/domain/learning.py`, `app/domain/owner_tasks.py`, `app/api/inbound.py`, `tests/unit/test_learning.py`.

### Booked-meeting brief stamp + owner pull — **alive** (persist-only)

Verified booking (including provider-already-there recovery) and verified reschedule stamp the existing Postgres `meeting_briefs` row with `meeting_status=booked` and UTC `scheduled_at` only — no Meet link, names, emails, or phones; demo/kill switch skip stamp; no new canonical `MEETING_BRIEF` (`{lead_id}:brief:offer_meeting` stays first-write-wins on offer). A later `offer_meeting` upsert keeps the booked stamp. Owner WhatsApp exclusive pull phrases (`meeting brief`, `pre-meeting brief`, `pre meeting brief`, `תקציר פגישה`, `בריף פגישה`) classify before keyword matching; requires `lead_*` or Understanding Check; `apply_owner_meeting_brief` returns Hebrew from stored payload (read-only on kill switch; no proactive send; task `logged`; no `due_at`). `סיכום פגישה` stays debrief; `booked meetings` stays owner_notify.

Key files: `app/domain/briefs.py`, `app/domain/calendar_booking.py`, `app/domain/meeting_changes.py`, `app/domain/owner_tasks.py`, `app/api/inbound.py`, `tests/unit/test_briefs.py`.

### Phase 1 documentation package (no application code)

`docs/CAPABILITY_OWNERSHIP.md`, `docs/PERFORMANCE_BUDGET.md`, `docs/RUNTIME_DECISION_PLAN.md`, `docs/MODEL_BENCHMARK_PLAN.md`, `docs/EXTERNAL_SETUP_CHECKLIST.md`. Honest: webhook ack target is unmet until ingress splits; runtime ADR and model-routing decision are **not** written (need benchmarks). Application-code units still wait on Assaf approving `docs/PRE_PRODUCTION_GAP_REPORT.md`.

### Pre-production gap audit — Phase 0 (no application code)

Control files at workspace root (not a nested `mia/`): `MIA_PRE_PRODUCTION_ARCHITECTURE_ADJUSTMENTS.md`, `MIA_FINAL_MILE_PLAYBOOK.md`. Factual map: `docs/PRE_PRODUCTION_GAP_REPORT.md`. Do not implement application code from that file until Assaf approves the report.

### Owner meeting notify — reschedule + cancellation kinds — **alive** (persist-only)

Extended persist-only owner meeting notify inbox to three allowlisted kinds: `meeting_booked` (existing), `meeting_rescheduled`, `meeting_cancellation_requested`. Same unique `(kind, lead_id)` — first write wins per kind. Verified reschedule (including provider-already-target recovery) and first cancellation request upsert unseen rows after local+canonical writes; demo/kill switch skip persist. `apply_owner_notify` lists unseen rows of all three kinds (max 3; ordered by `scheduled_at` then `id`); kind-specific Hebrew first lines; empty `אין התראות פגישות חדשות.`; extra `עוד {n} התראות.` Same exclusive pull phrases; no proactive send.

Key files: `app/domain/owner_notify.py`, `app/domain/meeting_changes.py`, `app/domain/calendar_booking.py`, `app/db/store.py`, `tests/unit/test_owner_notify.py`.

### Graph Lab writing_v1 eval suite — **alive** (local only)

`app/evals/datasets/writing_v1.json` + `run_writing_eval()` cover playbook §29 writing categories (discovery, short answer, technical, objection, booking, follow-up, owner report, complaint) in Hebrew and English, plus anti-pattern lint-fail cases. Sales/buyer evals now require `lint_customer_reply(reply).ok`. Hebrew technical buyer turn uses extract token `לא סומך על ai` (same path as English `don't trust ai`). No LangSmith, no send, no DB.

Key files: `app/evals/harness.py`, `app/evals/datasets/writing_v1.json`, `tests/unit/test_evals.py`.

### Prospect follow-up draft compose — **alive** (persist-only)

When due-scan marks a prospect follow-up `send_ready`, Mia composes canned Hebrew copy from `app/domain/followup_voice.py`, runs `lint_customer_reply`, and persists approved text on `lead_follow_ups.draft`. Upsert resets `draft` with scan fields. No MessagePort, no HTTP, no send. Draft never in Sheets tab 08, due-scan JSON, logs, traces, or canonical events.

Key files: `app/domain/followup_voice.py`, `app/domain/followups.py`, `app/db/models.py`, `app/db/store.py`, `migrations/20260821_follow_up_draft.sql`, `tests/unit/test_followups.py`, `tests/unit/test_due_scan_worker.py`, `tests/unit/test_humanity.py`.

**Operator migration:** on existing Postgres/file sqlite DBs run `migrations/20260821_follow_up_draft.sql` to add `draft` on `lead_follow_ups`.

### Human Voice deterministic linter — **alive**

Before LLM sales paraphrase reaches a prospect, `lint_customer_reply` in `app/domain/humanity.py` runs deterministic checks (AI phrases, typography, question count, unsupported-claim block). Phrase match folds curly apostrophes (`Let’s` / `It’s`). Typography (Assaf): em/en dash, backslash, decorative ` / ` and ` - `, `--`, and letters spaced with `-` / `--` / `'` / `\\` — not `//`. `OpenAISalesReplyPort` treats lint failure like HTTP failure — tries fallback model, then canned. Canned copy is not re-linted at runtime; sales canned, booking-voice, and meeting-change constants are proven by unit tests. Meet-link confirmations are not linted. Owner WhatsApp, scorecards, tool payloads, and provider data are out of scope. No LLM rewrite this slice.

Key files: `app/domain/humanity.py`, `app/integrations/sales_reply.py`, `app/core/capabilities.py`, `app/graph/replies.py`, `tests/unit/test_humanity.py`, `tests/unit/test_sales_reply.py`.

**Canned copy fix:** three objection/disqualify strings had em dashes replaced with commas (same meaning, Human Voice typography rule).

### Owner daily/weekly brief — booked meetings + cancellation requests — **alive**

Owner scorecards (`סיכום יומי` / `סיכום שבועי`) now include `פגישות נקבעו` and `בקשות ביטול` counts from canonical `MEETING_BOOKED` and `MEETING_CANCELLATION_REQUESTED` events. Counts use `COUNTABLE_EVENT_TYPES` in `LeadStore.count_canonical_events`; `KPI_EVENT_TYPES` and Sheets tab 09 / `compute_weekly_kpi` contract unchanged. Persisted on `owner_briefs` and `owner_weeklies` as `meetings_booked` and `cancellation_requests`. No PII in Hebrew ack; no execute/send/Sheets.

Key files: `app/domain/kpis.py`, `app/domain/owner_briefs.py`, `app/domain/owner_weeklies.py`, `app/db/store.py`, `app/db/models.py`, `migrations/20260821_owner_brief_booked.sql`, `tests/unit/test_owner_briefs.py`, `tests/unit/test_owner_weeklies.py`, `tests/unit/test_kpis.py`.

**Operator migration:** on existing Postgres/file sqlite DBs run `migrations/20260821_owner_brief_booked.sql` to add `meetings_booked` and `cancellation_requests` to `owner_briefs` and `owner_weeklies`.

### Owner meeting booked notify — **alive** (persist-only inbox)

Verified booking (website + prospect inbound) upserts one unseen row in `owner_notifications` (`kind=meeting_booked`; unique per lead; `scheduled_at` UTC ISO; `seen_at` empty; first write wins). Owner WhatsApp exclusive pull phrases classify as `owner_notify` after calendar/gmail_summary: English `booked meetings`, `what got booked`, `meeting notifications`; Hebrew `מה נקבע`, `פגישות שנקבעו`, `התראות פגישות`. Bare `התראות`/`notifications`/`calendar`/`יומן` do not match. Preference text with those phrases would classify as notify first (product phrases; do not use in preference tests). `apply_owner_notify` returns Hebrew blocks with `lead_id` + `מועד` only (via `format_slot_time`); max 3 unseen across booked/rescheduled/cancellation kinds; extra line `עוד {n} התראות.`; empty `אין התראות פגישות חדשות.`; marks seen on deliver (R1 `owner_notify_deliver`; business kill switch bypass in assert); kill switch pull is format-only; demo skips persist and apply returns None. Never proactive MessagePort; owner inbound reply only. Not in daily brief scorecard. Task stays `logged`; no `due_at`.

Key files: `app/domain/owner_notify.py`, `app/domain/calendar_booking.py`, `app/domain/owner_tasks.py`, `app/api/inbound.py`, `app/db/store.py`, `app/db/models.py`, `migrations/20260821_owner_notifications.sql`, `tests/unit/test_owner_notify.py`.

**Operator migration:** on existing Postgres/file sqlite DBs run `migrations/20260821_owner_notifications.sql`.

### Final Mile Gate 3 owner calendar availability — **alive** (read-only)

Owner WhatsApp calendar phrases (`calendar availability`, `check my calendar`, `מועדים פנויים`, etc.) classify exclusively as `calendar` before keyword matching. `"calendar"` / `"my calendar"` alone stay NOTE so preference text is not stolen. `apply_owner_calendar` in `app/domain/owner_calendar.py` reads free/busy via typed `CalendarPort`, applies ADR-012 `carve_policy_slots`, replaces ack with numbered Hebrew times + `לא יוצרת פגישה` — never prospect `השב 1, 2 או 3`. R0 `calendar_read`; canonical `TOOL_RESULT` `calendar_find_free_slots` (`tool`/`status`/`result_count` only). Kill switch: generic logged ack + denied, no port. Demo: generic ack, outcome None. Task stays `logged`; no `due_at`; action LOG. No create, no busy-event dump, no CalendarBookingPort.

Key files: `app/domain/owner_calendar.py`, `app/domain/owner_tasks.py`, `app/api/inbound.py`, `app/domain/commitments.py`, `app/core/capabilities.py`, `tests/unit/test_owner_calendar.py`.

### Final Mile Gate 2 safe remainder — **alive by fake** (ADR-013)

Accepted boundary: automatic confirmed reschedule; cancellation request for Assaf. Exact bilingual whole-message parsers only. Booked reschedule reads availability under ADR-012 and stores separate `reschedule_slots_json`; exact numbered selection is R2 `calendar_reschedule` AUTO in approved scope. Exact provider GET by stored event ID blocks on `error|not_found`; provider-already-target recovers locally; otherwise exact conflict recheck → narrow PATCH → mandatory exact GET verify, including PATCH timeout recovery. Local update preserves event ID, strict Meet link, meeting type, and `booked_at`; canonical `MEETING_RESCHEDULED` contains only status and UTC time.

Cancellation is R1 local-only `cancellation_requested`, timestamped and idempotent. No Calendar port call, no provider delete, and customer copy says Assaf will update the calendar. `GOOGLECALENDAR_EVENTS_GET` and `GOOGLECALENDAR_PATCH_EVENT` join existing Calendar pins on toolkit `20260812_00`; `GOOGLECALENDAR_UPDATE_EVENT` and provider deletion remain unavailable. Verified initial booking/crash recovery closes pending meeting-offered follow-up with `meeting_booked`; send-readiness independently blocks booked/cancellation-requested meetings.

Meeting migration: `migrations/20260821_adr013_calendar_gate2.sql` adds `reschedule_slots_json`, `rescheduled_at`, and `cancellation_requested_at`. Status allowlist is `offered|booked|cancellation_requested`. Create and reschedule are alive by fake; cancellation is manual request by safety design. Remaining live gate: operator staging OAuth CREATE/PATCH/GET acceptance and manual cancellation handoff verification.

Key files: `app/domain/meeting_changes.py`, `app/integrations/calendar_booking.py`, `app/domain/calendar_booking.py`, `app/domain/followups.py`, `app/domain/events.py`, `app/domain/meetings.py`, `app/db/store.py`, `app/db/models.py`, `tests/unit/test_calendar_gate2.py`, `docs/DECISIONS.md` (ADR-013).

### §12.2 / §18.2 calendar booking — **alive by fake** (ADR-011 + ADR-012)

Explicit numbered slot confirmation (`1`/`2`/`3`, `slot N`, `option N`, Hebrew ordinals). Separate `CalendarBookingPort`; read-only `CalendarPort` unchanged. R2 `calendar_create` in approved scope; conflict recheck; idempotent private-property lookup; **post-create verify** (`calendar_booking_verify`); **Sun–Thu 09:00–17:00 Asia/Jerusalem + 24h notice** (no env bypass); canonical `MEETING_BOOKED`; Human Voice Hebrew copy. ADR-013 above completes the safe reschedule/cancellation-request code; live staging OAuth acceptance remains open.

Key files: `app/integrations/calendar_booking.py`, `app/domain/calendar_booking.py`, `app/domain/meeting_availability.py`, `app/domain/booking_voice.py`, `app/domain/meeting_slots.py`, `app/domain/meetings.py`, `app/db/store.py`, `app/db/models.py`, `app/integrations/calendar.py`, `app/api/inbound.py`, `app/api/website.py`, `tests/unit/test_calendar_booking.py`, `tests/unit/test_meeting_availability.py`, `docs/DECISIONS.md` (ADR-011, ADR-012).

**Operator migration:** on existing Postgres/file sqlite DBs add `meetings.offered_slots_json TEXT DEFAULT '[]'`, `meetings.meet_link VARCHAR(512) DEFAULT ''`, widen `meetings.calendar_event_id` to 1024, add `meetings.meeting_type VARCHAR(32) DEFAULT 'intro_call'`, `meetings.booked_at VARCHAR(32) DEFAULT ''`.

**Operator OAuth:** live Composio create needs Calendar **write** scope on connected Google account — verify in Composio before production; code proven by fake/mock only until then.

### §12.2 pre-meeting company research — **alive** (ADR-010)

Explicit `SalesState.company_domain` via `app/domain/company.py` (`sanitize_company_domain`, `extract_explicit_company_domain`); first-write-wins in `extract_sales_signals`; excluded from qualification/NBA/missing_fields/canonical `MEETING_BRIEF`/Sheets/lead review. `OFFER_MEETING` appends Hebrew domain question only when domain empty. `apply_meeting_brief_policy` enriches Postgres brief row (not canonical event) with domain/research; one cached `ResearchPort.search(domain)` per lead/domain; `TOOL_RESULT` tool `meeting_research` on inbound + website prospect paths.

Key files: `app/domain/company.py`, `app/domain/briefs.py`, `app/domain/extract.py`, `app/graph/replies.py`, `app/domain/sales.py`, `app/db/models.py`, `app/db/store.py`, `app/domain/tools.py`, `app/api/inbound.py`, `app/api/website.py`, `tests/unit/test_company_meeting_research.py`, `docs/DECISIONS.md` (ADR-010).

**Operator migration:** add `company_domain VARCHAR(253) DEFAULT ''` to `lead_sales_state` on existing Postgres/file sqlite DBs.

### §20.2 website funnel drop — **alive**

Behavior events already persist (`mia_opened`, `conversation_started`). They are **not** in `KPI_EVENT_TYPES`. New `LeadStore.count_behavior_events(*, kind, occurred_from, occurred_to)` allowlists `ALL_BEHAVIOR_KINDS`, filters `event_type=behavior` + bounds, counts payload `kind` in Python (no dialect `json_extract`; no whole-table `COUNT(*)`).

- Single Bible-aligned anomaly `website_funnel_drop`: current `mia_opened > 0` with zero `conversation_started`, or current opens > previous opens while current starts < previous starts (all relevant counts known; 7d only).
- Missing counts never invented. No magic conversion-rate threshold. Funnel investigate skips 30d fetch. No Meta writes.
- Hebrew: `זוהתה ירידה במשפך האתר — ממליצה לבדוק את המסלול, בלי שינוי תקציב.`
- Enrich funnel path runs only when rec remains `watch` after leads/CPL/fatigue; `spend_without_leads` / `cpl_spike` / `creative_fatigue` still win.

Key files: `app/db/store.py`, `app/domain/campaigns.py`, `app/integrations/meta_ads.py`, `app/domain/events.py`, `tests/unit/test_campaigns.py`, `tests/unit/test_meta_ads.py`, `tests/unit/test_events.py`.

### §20.2 creative fatigue — **alive** (parent-reviewed)

Official Composio `METAADS_GET_INSIGHTS` `fields` include `frequency` ([docs](https://docs.composio.dev/toolkits/metaads)). Same pin: toolkit `20260731_00`. No new permission.

- `INSIGHT_FIELDS = ["spend", "impressions", "clicks", "ctr", "frequency", "campaign_name"]`
- `CampaignInsights.frequency: str | None`
- Anomaly `creative_fatigue` (`kind=investigate`) when **7d** current frequency **>** previous-7d frequency **and** current CTR **<** previous CTR. All four must parse. Missing never treated as 0. **No magic threshold** (no “frequency > 3”).
- Hebrew: `תדירות עלתה ו-CTR ירד מול שבעת הימים הקודמים — ממליצה לבדוק קריאייטיב, בלי שינוי תקציב.`
- Displayed snapshot stays `last_7d` (appends `freq {value}` when present).
- Pin stays `METAADS_GET_INSIGHTS`. No Meta writes.

**Parent patch (Composer missed this):** first `analyze_insights` can already return `creative_fatigue` before Postgres leads/CPL run. Enrich must still enter the leads/CPL path when rec is `creative_fatigue`, and **must not** take fatigue from the leads re-call before previous-window leads are counted. `cpl_spike` and `spend_without_leads` still win over fatigue. Tests: `test_analyze_cpl_spike_wins_over_creative_fatigue`, `test_enrich_analytics_ack_cpl_spike_wins_over_creative_fatigue`.

Key files: `app/domain/campaigns.py`, `app/integrations/meta_ads.py`, `app/domain/events.py` (`_CAMPAIGN_RECOMMENDATION_ANOMALIES`), `tests/unit/test_campaigns.py`, `tests/unit/test_meta_ads.py`.

### §20.2 today-vs-baseline — **alive** (ADR-008)

Baseline = previous seven **completed** local-calendar days (since=D-7, until=D-1). Read-only Hebrew line after recommendation; not an anomaly.

- `baseline_7d_time_range` in `app/domain/campaigns.py`; `format_today_baseline_line` in `app/integrations/meta_ads.py`.
- Calls: `get_insights(date_preset="today")` and `get_insights(date_preset=None, time_range=baseline_7d_time_range(...))` — never together.
- Additive metrics ÷7 for daily average; CTR aggregate ratio not divided; frequency omitted; missing pairs omitted.
- `FakeMetaAdsPort`: `today_snapshot` + explicit `time_range_snapshots` keyed by `(since, until)`.
- Does not change `CampaignRecommendation`, anomaly priority, or 30d fetch. No Meta writes.

Key files: `app/domain/campaigns.py`, `app/integrations/meta_ads.py`, `docs/DECISIONS.md` (ADR-008), `tests/unit/test_campaigns.py`, `tests/unit/test_meta_ads.py`.

### §21A personal LinkedIn post analytics — **alive** (ADR-009)

Separate typed `LinkedInAnalyticsPort` in `app/integrations/linkedin_analytics.py` — **not** Composio `LINKEDIN_GET_SHARE_STATS` (organization URN only). Direct official `GET /rest/memberCreatorPostAnalytics`; pin `LINKEDIN_API_VERSION=202608`; scope `r_member_postAnalytics`; `q=me`; previous 30 completed local days; six metrics; Hebrew stats-only ack line; canonical `TOOL_RESULT` tool `linkedin_analytics`. Profile read stays Composio `LinkedInPort`. Live HTTP needs operator OAuth app approval + `MIA_LINKEDIN_ACCESS_TOKEN` — code alive by mock.

Key files: `app/integrations/linkedin_analytics.py`, `app/api/inbound.py`, `app/api/deps.py`, `app/domain/tools.py`, `docs/DECISIONS.md` (ADR-009), `tests/unit/test_linkedin_analytics.py`.

### Adjustment L adversarial identity tests — **alive** (tests only)

`tests/unit/test_adversarial_identity.py` proves phone-only owner auth and prospect isolation: “I am Assaf” / “אני אסף” on non-owner WhatsApp stays prospect sales path; forwarded owner commands (`from now on remember my style`, `how's the campaign spend`) do not persist `owner_instructions` or owner analytics tasks; Gmail `Assaf <inject@example.com>` stays prospect-by-email with no send; website prompt-injection and campaign-write asks return sales NBA without prompt dumps or owner tasks (canned sales path — `conftest` empties sales models); prospect `review lead_*` does not persist `lead_reviews`; prospect “approve the proposal” leaves approval pending; owner approve without `lead_*` with two pending rows stays ambiguous; duplicate WhatsApp identities do not leak sales state across leads; owner authorization survives separate inbound calls (channel restart); revoked owner phone falls back to prospect path while prior owner task history remains; owner research scrape-injection ack shows title+host only — excerpt/instruction text does not activate preferences or campaign recommendations. Full webpage-scrape adversarial suite in `tests/unit/test_webpage_scrape_adversarial.py` (http/javascript/data URLs dropped; path/query/excerpt not in ack or TOOL_RESULT; meeting brief stores title+host only). Website campaign-write checks are conversation-scoped canonical events (not global `campaign_recommendations.scope=account`). Scrape-injection adversarial coverage includes owner-research ack and meeting-brief research storage. No application-code units from the gap report until Assaf approves it.

---

## Campaign analysis priority (highest wins)

1. Incomplete metrics → `uncertain` / `incomplete_metrics`
2. Spend without clicks → `spend_without_clicks`
3. 7d spend-up / clicks-down → `spend_up_clicks_down`
4. Spend without leads (watch + store + count==0) → `spend_without_leads`
5. CPL spike (watch + both CPLs parse, current > previous) → `cpl_spike`
6. Creative fatigue (freq↑ + CTR↓ vs previous 7d; all four parse) → `creative_fatigue`
7. Website funnel drop (`website_funnel_drop` via `count_behavior_events`; 7d only)
8. Watch → may upgrade via 30d compare **only** on `spend_up_clicks_down_30d`

30d fetch runs only when rec remains `watch` after 1–7. Displayed snapshot stays `last_7d`. No Meta writes. Never send `date_preset` and `time_range` together.

Enrich note: leads/CPL path also runs when first pass is already `creative_fatigue`, so 4–5 can still override 6.

---

## Gated — stop for Assaf (do not implement)

- Lambda / SQS / WAF / AgentCore / `app.infra` (`AWS_RUNTIME` still **specified** until ALB+RDS run; first live host is ADR-014)
- Instruction **activation** (must not append raw owner text to prompts)
- Owner task **execute** (sales/analytics/research still log-only; spend-threshold `due_ready` does not execute analyze)
- Gmail **send**
- Meta/LinkedIn **writes**, ManyChat Public API send
- Follow-up **send** (policy + send-readiness + frequency cap + due-scan CLI exist; no MessagePort send)
- TTS, browser/crawl
- Widget embed on production AssafWeb (script is in the Next app; Vercel `NEXT_PUBLIC_MIA_BASE_URL` still needs a public HTTPS Mia origin — not localhost)
- Identity **unmerge** (R5)
- Spend-threshold owner **execute**

---

## Later non-gated gaps

- **Website + SEO** — implemented (inspect/ports); live Google still needs Composio OAuth + property/site URL. Does **not** block AWS first live.
- **ADR-015 Composio Instagram send + insights ports** — **alive** (toolkit `20260819_00`). Default sender remains `direct`. Do not flip production env to `composio` until staging send is tested.
- **Apify behind `ResearchPort`** — no `MIA_APIFY_*` until that adapter exists
- **§18.1 Gmail summary into sales graph** — explicitly out of scope; summaries must not become instructions
- `APPROVAL_REQUIRED` payload stays `pending` after owner decide (row is SoR; event first-write-wins) — intentional

---

## Landmines

- Shared in-memory SQLite across tests. Unique `provider_event_id`s, emails, phones. **Never `COUNT(*)` the whole table**; filter by `lead_id` / `run_id` / `provider_event_id` / `media_id` / `brief_date` / `week_start` / `thread_id`. Account-level counts: increment (`after >= before + 1`).
- Do not use `reset_engine` autouse fixtures that wipe the shared DB.
- Adding a campaign anomaly: **both** `app/domain/campaigns.py` `_VALID_ANOMALIES` **and** `app/domain/events.py` `_CAMPAIGN_RECOMMENDATION_ANOMALIES`.
- Account-level `campaign_recommendations` is unique on `scope="account"` (upsert). Canonical `CAMPAIGN_RECOMMENDATION` first-write-wins (`meta:campaign:recommendation`). Persist INVESTIGATE in a shared-DB test can break later kill-switch assertions — monkeypatch; don’t count the whole recommendations table.
- `count_canonical_events` returning `0` for unknown types looks like “no leads”. Funnel drop must use a **new** behavior count, not KPI types.
- Demo never contains private lead data; never activates in `MIA_ENV=prod`.
- Campaign budget is user-configured (`MIA_CAMPAIGN_MONTHLY_BUDGET`); never infer an approved spend limit. Missing metrics stay empty (never zero-fill).
- Deal `expected_value` / `closed_value` always `""`.
- Tests isolate via `tests/conftest.py`: memory sqlite, kill switch false, `MIA_DEMO_MODE=false`, empty sales/transcribe models, empty campaign budget/name, empty Yuma prelaunch env.
- Do not implement MIME decoders, store IG CDN URLs, infer campaign budget, infer deal values, send/delete Gmail, send follow-ups, activate instructions, or Meta-write.
- Production Mia never self-edits graph/prompts/code. Graph Lab is local eval only.
- One IG sender per conversation. Never dual-send via ManyChat, Composio, and Graph.
- Owner inbound debrief + English `follow-up` / `deal` also matches sales keywords → Understanding Check (two types). Use Hebrew `מעקב` / `לשלוח הצעה` on the inbound path; domain `parse_debrief_next_step` may still see English via direct apply.
- Sheets `claim_operation(scope=sheets_mirror)` key is `{inbound_id}:sheets:{sales|session|campaign|content}` — never `{lead_id}:sheets`. A later inbound must still refresh the snapshot.
- `CanonicalEvent.payload_version` is an envelope column stamped in `save_canonical_event` only (allowlist `"1"`). Never put it in `payload_json` or GraphState. Do not add a parallel `InboundEvent` or a `business_id` tenant column.
- Follow-up `claim_operation(scope=follow_up)` key is `{inbound_id}:followup` — never `{lead_id}:followup`. A later inbound must still cancel/recover/create. Do not wrap due-scan or booking cancel. Send stays gated.
- Cancellation `claim_operation(scope=calendar_cancellation)` key is `{inbound_id}:cancellation` — never `{lead_id}:cancellation`. Already-requested short-circuits **before** claim. Empty `inbound_id` still persists. Do not claim reschedule on this scope. Provider delete stays R5.
- `send_inbound_reply` catches **only** `RuntimeError` (DisabledMessagePort). WhatsApp/IG send HTTP stays `WhatsAppSendError` / `InstagramSendError` (502) wrapping `AdapterHttpError` so webhook claim rolls back. Do not catch `AdapterHttpError` there. STT HTTP stays `TranscriptionError` wrapping `AdapterHttpError`; HTTP 200 empty text is not AdapterHttpError. Instagram unsupported host and media missing-url/host/size are not HTTP classify.
- OpenAI sales-reply `_complete` raises `AdapterHttpError` on HTTP/transport; `compose` catches and returns canned. Do not add `sales_reply` to `ALLOWLISTED_TOOLS`. Do not let compose HTTP 502 the webhook.
- OpenAI thread-summary `_complete` raises `AdapterHttpError` on HTTP/transport; `summarize` catches and returns canned unclear. Do not add `gmail_summary` / `thread_summary` to `ALLOWLISTED_TOOLS`. Do not let summary HTTP 502 the webhook. Emails stay untrusted data.
- Operator runbook is `docs/RUNBOOK.md`. First-live ALB CloudWatch alarms exist (`deploy/cloudwatch-alb-unhealthy.example.json`, `deploy/cloudwatch-alb-5xx.example.json`) with no SNS. Do not invent a pager or mark Gate F Complete. Kill switch is global; named flags do not override R4/R5.
- `mia-reconcile --inspect` is read-only SoR listing (cap 50). Webhook findings overlay `channel` + `envelope_kind` from `webhook_events` (first-colon subject split). Default CLI stays counts-only. Do not add replay or `mark_webhook`. Do not dump body/PII.
- Instagram insights: raise `AdapterHttpError` on **media list** HTTP only. Per-media insights 400 still skips that media (`test_graph_port_insights_400_skips_media`).
- Calendar booking HTTP: lookup/GET classified retry, no write. Create/PATCH HTTP still **verifies** (recovery can persist). `find_free_slots` HTTP is retry, **not** conflict (do not clear offered/reschedule slots). 200 `successful=False` still None, do not raise.
- Sheets: `ComposioSheetsPort` raises `AdapterHttpError` on HTTP/transport. Every `mirror_*` catches it and returns False. Do not let upsert exceptions escape inbound. `sheets_mirror_outcome` still `denied` on zero writes (kill switch unchanged). Sales-tab, session-tab, campaign-tab, and content-tab timers start **after** claim. Session-tab key is `{session_id}:tool:sheets_mirror`; sales-tab key is `{message_id}:tool:sheets_mirror`; campaign/content keys are `{inbound_id}:tool:sheets_mirror_campaign` / `{inbound_id}:tool:sheets_mirror_content` — never reuse `sheets_mirror` on those tabs (same inbound would collide). Claim-fail returns `None` (no persist). Do not put `latency_ms` in the canonical payload. Website session+message tests that count conversation TOOL_RESULT must expect two `sheets_mirror` rows. `sheets_tab_mirror_outcome` only accepts the two campaign/content tool names.

---

## Canonical timeline (current)

| Event | `provider_event_id` | When | Payload |
| --- | --- | --- | --- |
| MESSAGE_IN | `{inbound_id}` | inbound accept / website message | `{"text": …}` truncated 2000 |
| MESSAGE_OUT | `{inbound_id}:out` | successful inbound send **or** finalized website HTTP reply | text only |
| LEAD_CREATED | `{lead_id}:created` | first `open_channel_lead` only | `{"stage": "open"}` |
| QUALIFICATION_UPDATED | `{run_id}:qual` | graph extract changes SalesState | allowlisted qual fields |
| MEETING_OFFERED | `{run_id}:meet` | graph selects `offer_meeting` | `{"next_action": "offer_meeting"}` |
| MEETING_BRIEF | `{lead_id}:brief:offer_meeting` | first write on offer_meeting snapshot | allowlisted SalesState flags |
| MEETING_BOOKED | `{lead_id}:booked` | first write on explicit slot confirmation | `{status: booked, scheduled_at: UTC ISO}` only |
| MEETING_RESCHEDULED | `{lead_id}:rescheduled:{target_booking_key}` | first write per verified target | `{status: booked, scheduled_at: UTC ISO}` only |
| MEETING_CANCELLATION_REQUESTED | `{lead_id}:cancellation_requested` | first local cancellation request | `{status: cancellation_requested}` only |
| MEETING_DEBRIEF | `{lead_id}:debrief` | owner debrief persist | `outcome` + `next_step` only |
| HANDOFF | `{run_id}:handoff` | graph selects `handoff` | `{"next_action": "handoff"}` |
| APPROVAL_REQUIRED | `{lead_id}:approval:proposal_handoff` or `{campaign_id}:approval:campaign_write` | first write when R3 handoff or R4 campaign request persists | `{action, risk, decision}` — **stays `pending` after owner decide** |
| FOLLOW_UP | `{lead_id}:followup:meeting_offered...` or `:followup:meeting_booked:cancelled` | create / opt-out / recovered / verified booking stop | `{status, reason}` |
| ATTRIBUTION | `{lead_id}:attribution` | website UTMs **or** IG organic/referral | website utm keys; IG: `ig_content_id` / `ig_trigger_source` / `ig_ref` / `meta_ad_id` / `meta_post_id` only; referral-only without mid uses `igref:{sender}:{stable}` |
| BEHAVIOR | `{session_id}:mia_opened` etc. | funnel events | allowlisted kind + path/section/cta |
| TOOL_RESULT | `{inbound_id}:tool:{tool}` | calendar booking/reschedule, sheets, meta, research, LinkedIn, STT, Instagram insights | `tool` / `status` / `result_count` |
| CAMPAIGN_RECOMMENDATION | `meta:campaign:recommendation` | owner analytics ack after insights | `{kind, anomaly}` first write wins |
| DEAL_UPDATED | `{lead_id}:deal:{stage}` | first write per stage | `stage` / `source` / `attribution_confidence` |

R3/R4 approval binding (**alive**, persist-only): lead rows `resource_type=lead`, `resource_id=lead_id`; campaign rows `resource_type=campaign`, `resource_id=campaign_id`, `lead_id=NULL`, action `campaign_write`, risk R4; `expires_at` +24h TTL; five-key `payload_hash`; `approval_id`/`proposed_parameters`/`approved_at` persist-only; execute columns reserved empty; stale/unbound pending → owner decide returns `expired`/`unbound` without row mutation; migrations `migrations/20260821_approval_binding.sql` + `migrations/20260821_approval_campaign_resource.sql` + `migrations/20260821_approval_object_fields.sql`. No Meta execute.

`SENDABLE_CHANNELS` = `{whatsapp, instagram}` only.

Sheets tabs **01–10** all claimed alive. Demo skips built tabs.

Campaign recommendation anomalies now: `none`, `spend_without_clicks`, `incomplete_metrics`, `spend_up_clicks_down`, `spend_up_clicks_down_30d`, `spend_without_leads`, `cpl_spike`, `creative_fatigue`, `website_funnel_drop`.

---

## Commands

```
uv sync --group dev
uv run pytest
uv run ruff check app tests
uv run uvicorn app.main:app --reload
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
uv run mia-migrate
uv run mia-due-scan
uv run mia-reconcile
uv run mia-reconcile --inspect
```

Go-live order: `docs/PRODUCTION_BUILD.md` (ADR-014). Day-2 ops: `docs/RUNBOOK.md`. Laptop: fill `.env` from `.env.example`. Production keys: Secrets Manager `mia/prod` only. `MIA_KILL_SWITCH=false` for live. R4 Meta writes stay approval-gated and R5 stays deny; those are not env knobs.

A capability is **wired** only if it has a typed port in code and appears in `app/core/capabilities.py`. It is **alive** only if a test proves the path runs. No dead folders.

---

## How to continue

1. Inspect current tree (`docs/PRD.md`, `docs/BUILD_STATUS.md`, `docs/HANDOFF.md`).
2. Operator next: Assaf 2026-08-22 — **do not inspect `.env`**. Production keys go only in AWS Secrets Manager `mia/prod` (the box); laptop `.env` stays local. Confirm by Assaf saying `done` (no key in chat), then `/health` booleans (`composio` / `composio_webhook` / `sales_llm` / `postgres` / `public_https` / `whatsapp_ingest`). OpenAI + Gemini are connected. Calendar is Active on the **Cursor** Composio plugin — Mia needs the same `MIA_COMPOSIO_USER_ID` as Composio debug `@user_id`. Clear the dashboard OAuth user-verification placeholder; do not build `/composio/verify`. First live = Fargate + RDS + SM box + ALB `https://mia.assafweb.com` (`docs/PRODUCTION_BUILD.md`). Set Vercel `NEXT_PUBLIC_MIA_BASE_URL` to that origin. Adapter map is ADR-015 + ADR-016 — WhatsApp inbound stays Meta; do not fake a Composio inbound trigger; do not poll `WHATSAPP_GET_MESSAGE_HISTORY`; do not flip ECS `MIA_WHATSAPP_SENDER` to composio until WhatsApp is Active on that user; do not add Apify env; do not flip `MIA_INSTAGRAM_SENDER=composio` until staging send is tested. Do not enable Gmail/Meta/follow-up send. Local `MIA_ENV=prod` unmounts `/docs` — that is intended; use `/health`. If loopback widget.js is stale, a leftover `127.0.0.1:8000` listener may be in front of the `0.0.0.0` uvicorn. Next implementable Python (not gated): Composio Instagram send + insights behind existing ports (keep Graph until tested), then Adjustment J transcript frozen set (20) — blocked on frozen audio. FDE unit 8 versions + `decision_confidence="1.0"` are audit-only — do not invent `cost_usd` or wire HYBRID. Adjustment N live/short-cache stamps are wired; versioned knowledge RAG still missing — do not mark Complete. `tool_runs.correlation_id`, sanitized `webhook_events` envelope, and inspect overlay are done — do not add SQS, raw webhook body, or `webhook_events.correlation_id`. Ask Assaf before HYBRID. Do **not** enable Gmail/Meta/follow-up send, instruction activation, Lambda/SQS/AgentCore, or `app.infra`. Do not invent `cost_usd` or RAG. Do not write `MODEL_ROUTING_DECISION.md` until scoring. Live staging OAuth/Meta routing is operator setup. FDE unit 9 (`risk_gate` graph node) **DEFER**. Auth-owner matrix is written; production key box is Secrets Manager `mia/prod` (ADR-014). Operator runbook is `docs/RUNBOOK.md` — dashboards/alerts still missing; do not mark Gate F Complete. `mia-reconcile --inspect` is read-only — do not add replay/repair. WhatsApp/IG send and STT stay wrapped as MiaError 502 after `AdapterHttpError` (rollback unchanged). Do not catch send HTTP in `send_inbound_reply`. OpenAI sales-reply compose catches `AdapterHttpError` and returns canned (never 502). OpenAI thread-summary compose catches `AdapterHttpError` and returns canned unclear (never 502). Do not add `sales_reply` / `gmail_summary` / `thread_summary` to `ALLOWLISTED_TOOLS`. Do not add adapter retry loops. Cross-cutting persist-path suite is `tests/unit/test_idempotency_persist_paths.py` (listed actions write once). Do not add queue redelivery, Meta execute, or Gmail send to “complete” Adjustment E. Powertools DEFER. Cancellation persist is `claim_operation(scope=calendar_cancellation)` per inbound — do not claim per lead; provider delete stays R5. Sheets mirror persist is `claim_operation(scope=sheets_mirror)` per inbound — do not claim per lead. Follow-up persist is `claim_operation(scope=follow_up)` per inbound — do not claim per lead; send still gated. `CanonicalEvent.payload_version` is `"1"` on persist — do not add a parallel `InboundEvent` or `business_id` tenant. Existing DBs need `migrations/20260821_canonical_payload_version.sql`.
3. Parent independently reviews, patches, `uv run ruff check app tests; uv run pytest`.
4. Update `docs/PRD.md` + `docs/BUILD_STATUS.md` in the same turn.
5. Do not mark the `/goal` complete.
