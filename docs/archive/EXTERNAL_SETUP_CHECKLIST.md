# External integration readiness checklist

**Date:** 2026-08-22  
**Status:** Phase 1 operator checklist (Adjustment B/C/E/L + playbook Gate 7). Not a grant of write access.  
**Fill from:** `.env.example`, `docs/PROVIDER_MATRIX.md`, `docs/CAPABILITY_OWNERSHIP.md`  
**Ordered go-live:** `docs/PRODUCTION_BUILD.md`. Re-read official docs before enabling any write.

Use: `[ ]` open, `[x]` done, `N/A` intentionally disabled.

## Production AWS (ADR-014 — first live)

- [x] This Windows laptop: AWS CLI v2 current-user MSI (`AWSCLIV2-User.msi` — no UAC; `winget` `Amazon.AWSCLI` is all-users and stalls). Verified `aws --version` → `aws-cli/2.36.29`
- [ ] Credentials: `aws login` (console user) or `aws configure sso` then `aws sso login` (Identity Center). Then `powershell -File deploy/assert-aws-identity.ps1` must print a 12-digit account id (exit 0) before the VPC wizard. Never paste keys in chat

The key **box** is Secrets Manager secret `mia/prod`. Assaf puts SECRET JSON keys there. ECS Fargate injects them as `MIA_*`. Do not copy `.env` onto the host. Do not use a Lambda as the box. Lambda webhook ingress is the next slice after first live is healthy.

- [ ] RDS PostgreSQL **16** private: `deploy/rds-subnet-group.example.json` + `deploy/rds.example.json` (`EngineVersion` `16`, `PubliclyAccessible` false, `ManageMasterUserPassword` true — copy password into `mia/prod` only; never git/chat); `MIA_DATABASE_URL` uses `sslmode=verify-full` + image CA path
- [ ] Copy `deploy/mia-prod.secret.example.json` → local `mia-prod.secret.json` (gitignored), fill, paste into Secrets Manager name `mia/prod`, then delete the filled file
- [ ] VPC: **VPC and more** wizard (2 AZs, 2 public ALB, 2 private tasks/RDS, NAT **In 1 AZ**, DNS hostnames + resolution **on**); `create-security-group` (`--query GroupId`) → `deploy/fill-placeholders.ps1` → authorize from `deploy/local/sg-*-ingress.json` (never `*.example.json`; ALB 80/443, tasks 8000 from ALB, RDS 5432 from tasks; never 8000/5432 to `0.0.0.0/0`)
- [ ] Task execution role: `aws iam create-role` + trust `deploy/iam-ecs-task-trust.example.json` + `AmazonECSTaskExecutionRolePolicy` + `deploy/iam-task-execution-secrets.example.json` (`mia/prod*` only)
- [ ] ECR image from `deploy/Dockerfile`; register `deploy/ecs-task-definition.example.json` (do **not** `create-service` yet)
- [ ] ACM wait **ISSUED**; create ALB + target group; **re-stamp** hashes into `deploy/local/`; then idle 120s + `/health/live` + HTTPS TLS1.3 + HTTP:80 `HTTP_301`; Route53 alias to ALB
- [ ] One-off `mia-migrate` via `deploy/ecs-migrate-overrides.example.json` **before** `create-service`
- [ ] ECS service from `deploy/ecs-service.example.json` (target group ARN pasted; Fargate 1.4.0; public IP disabled)
- [ ] CloudWatch ALB alarms after `services-stable`: `deploy/cloudwatch-alb-unhealthy.example.json` (`UnHealthyHostCount` Minimum) + `deploy/cloudwatch-alb-5xx.example.json` (no SNS this slice)
- [ ] Vercel `NEXT_PUBLIC_MIA_BASE_URL=https://mia.assafweb.com` + landing-page redeploy
- [ ] `GET https://mia.assafweb.com/health` → `"env":"prod"`, `"postgres":true`, `"public_https":true`
- [ ] `/health/ready` 200 after `mia-migrate` on RDS
- [ ] After health is green: IAM `miaSchedulerRole` + `aws scheduler create-schedule` from `deploy/eventbridge-due-scan.example.json` (15m) and `deploy/eventbridge-reconcile.example.json` (hourly). Persist-only. Never send.

## Local webhook test (Cloudflare Tunnel — **test only**)

Cloudflare is **not** Mia’s production runtime. Do not deploy the sales graph to Workers. A TryCloudflare quick tunnel only publishes local uvicorn so Meta/Composio can POST HTTPS.

```
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
cloudflared tunnel --url http://127.0.0.1:8000
```

Paste the printed `https://*.trycloudflare.com` host into provider consoles. The hostname **changes every restart**. Leave `MIA_PUBLIC_BASE_URL=http://127.0.0.1:8000` in `.env` unless you need widget links to use the tunnel for that session.

- [ ] Test Telegram owner: `POST {tunnel}/v1/telegram/webhook` (`X-Telegram-Bot-Api-Secret-Token`)
- [ ] Test WhatsApp: `POST {tunnel}/v1/whatsapp/webhook` (GET verify same path)
- [ ] Test Instagram: `POST {tunnel}/v1/instagram/webhook`
- [ ] Test ManyChat: `POST {tunnel}/v1/manychat/external-request`
- [ ] Test Composio/Gmail ingest: `POST {tunnel}/v1/composio/webhook`
- [ ] Confirm `GET {tunnel}/health` is 200 before registering providers

## Identity and Meta

- [ ] Telegram owner bot: token + webhook secret + numeric user ids; `POST {MIA_PUBLIC_BASE_URL}/v1/telegram/webhook`
- [ ] WhatsApp Cloud app: verify token, app secret, webhook `POST {MIA_PUBLIC_BASE_URL}/v1/whatsapp/webhook`
- [ ] `MIA_WHATSAPP_OWNER_PHONES` is Assaf’s real WA ids (digits, no +) — never inferred from text
- [ ] Instagram professional account + Instagram Login app
- [ ] Instagram webhook `POST {MIA_PUBLIC_BASE_URL}/v1/instagram/webhook`
- [ ] **One** IG sender: `MIA_INSTAGRAM_SENDER=direct` (default Graph send), `manychat`, or `composio`. Inbound stays Meta webhook. Never Graph send and ManyChat send together. Flip production to `composio` only after staging send is tested.
- [ ] Meta Instagram Conversation Routing configured so a second app cannot take the thread (operator console; not in repo)
- [ ] ManyChat External Request bearer `MIA_MANYCHAT_INGEST_TOKEN` only if ManyChat is the chosen sender or trigger sidecar
- [ ] Existing Postgres/file sqlite DBs: run `migrations/20260821_manychat_identity_ids.sql` for `channel_identities.manychat_subscriber_id` + `manychat_conversation_id` (empty default; in-memory tests need no migration)
- [ ] Existing Postgres/file sqlite DBs: run `migrations/20260821_webhook_envelope.sql` for `webhook_events.channel` + `envelope_kind` (empty default; in-memory tests need no migration)
- [ ] Existing Postgres/file sqlite DBs: run `migrations/20260822_ai_run_automation_mode.sql` for `ai_runs.automation_mode` (empty default; in-memory tests need no migration)
- [ ] Existing Postgres/file sqlite DBs: run `migrations/20260822_ai_run_prompt_version.sql` for `ai_runs.prompt_version` (empty default; do not backfill)
- [ ] Existing Postgres/file sqlite DBs: run `migrations/20260822_conversation_controls.sql` for `leads.takeover_state` + `conversation_controls` (ADR-017)
- [ ] Insights token has `instagram_business_manage_insights` if organic insights are needed

## Google / Composio

- [ ] Composio API key + user id in Secrets Manager `mia/prod` (production) or local `.env` (laptop) — never git. Cursor plugin user and `MIA_COMPOSIO_USER_ID` must be the same UUID or Mia will not see plugin OAuth.
- [ ] **OAuth user verification URL is empty** in Composio Project Settings. Do not leave `https://your-app.com/composio/verify`. Mia has no verify route; that field blocks dashboard connections (yellow warning is correct).
- [ ] White-label (optional, production consent screens): Project Settings → White Labeling (logo + title). Per OAuth toolkit (Calendar, Gmail, Sheets, LinkedIn): Authentication management → Create Auth Config → OAuth2 → **Use your own developer credentials**. Not needed for API-key toolkits. Official: [White-labeling](https://docs.composio.dev/docs/auth-configuration/white-labeling), [Google OAuth app](https://composio.dev/auth/googleapps).
- [ ] Composio webhook secret for Gmail trigger `POST /v1/composio/webhook`
- [ ] WhatsApp Composio Managed App: connect on the **same** `MIA_COMPOSIO_USER_ID` before flipping ECS `MIA_WHATSAPP_SENDER` (ADR-016). Inbound stays Meta webhook. Do not subscribe `WHATSAPP_MESSAGE_STATUS_UPDATED_TRIGGER` as customer ingest.
- [ ] Gmail toolkit pin `20260817_00` — ingest only; send/delete **off**
- [ ] Calendar connected with **write** OAuth if staging CREATE/PATCH/GET will be accepted (ADR-011/013)
- [ ] Calendar toolkit pin `20260812_00` (FIND_FREE_SLOTS, EVENTS_LIST, CREATE_EVENT, EVENTS_GET, PATCH_EVENT)
- [ ] Sheets spreadsheet id; tabs 01–10 exist; pin `GOOGLESHEETS_UPSERT_ROWS` `20260813_00`
- [ ] Confirm Sheets is never read back into SalesState

## Meta Ads / LinkedIn / research

- [ ] `MIA_META_ADS_ACCOUNT_ID` + Composio Meta credentials for **read** only (`METAADS_GET_INSIGHTS` `20260731_00`)
- [ ] `MIA_CAMPAIGN_MONTHLY_BUDGET` set by Assaf — never inferred from spend
- [ ] LinkedIn Composio profile connected (`LINKEDIN_GET_MY_INFO`)
- [ ] LinkedIn app approved for `r_member_postAnalytics` if personal stats needed; token only in the SM box / local `.env`
- [ ] Firecrawl key if owner/meeting research search is live. No Apify key until `ResearchPort` has an Apify adapter (ADR-015).
- [ ] Meta **writes** remain off until Phase 7 + exact approval objects

## Runtime, secrets, kill switch

- [ ] `MIA_ENV=prod` never with `MIA_DEMO_MODE=true`
- [ ] `MIA_KILL_SWITCH` documented for Assaf (only emergency env stop) — operator steps in `docs/RUNBOOK.md`
- [ ] `MIA_DATABASE_URL` is RDS Postgres in production (file sqlite is local only). Existing DBs: `uv run mia-migrate`
- [ ] Production keys only in Secrets Manager `mia/prod` (ADR-014); ECS injects `MIA_*`; no secrets in git/logs/prompts; no host `.env`
- [ ] CORS limited to AssafWeb origins
- [ ] Widget: Vercel `NEXT_PUBLIC_MIA_BASE_URL=https://mia.assafweb.com` **by Assaf** (`GET /v1/website/widget.js`)

## Calendar / outbound staging (blocked until identity + flags + Assaf)

- [ ] Staging book / reschedule / GET verify on a throwaway calendar
- [ ] Manual cancellation handoff (provider delete stays denied)
- [ ] WhatsApp/Instagram send in a staging conversation (not mass outbound)
- [ ] Follow-up send, Gmail send, LinkedIn post, Meta write: **leave unchecked** until Phase 7

## Human / dual-control

- [ ] Human takeover process agreed (code **alive**: owner WhatsApp phrase + `lead_*`; see `docs/RUNBOOK.md` §2). Provider inbox still required when kill switch is on.
- [ ] Confirm no Meta icebreaker / ManyChat default reply overlapping Mia

## Docs to re-fetch before go-live

Official pages in `docs/PROVIDER_MATRIX.md` (Composio pins, Meta webhooks, LinkedIn member analytics, ManyChat Conversation Routing). Pins and prices go stale; do not copy prices into Python.
