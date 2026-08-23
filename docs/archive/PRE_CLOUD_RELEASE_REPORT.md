# Pre-cloud release report

**Date:** 2026-08-22  
**Status:** Application package ready for AWS. Not a grant of gated writes. Not permission to start RDS/ECS/ECR/ALB until Assaf says so.  
**Authority:** Assaf chat → `AGENTS.md` → Bible/`docs/PRD.md` → code  
**Related:** `docs/PRODUCTION_BUILD.md`, `docs/RUNBOOK.md`, `docs/EXTERNAL_SETUP_CHECKLIST.md`, `docs/PROVIDER_MATRIX.md`, `docs/CAPABILITY_OWNERSHIP.md`, `docs/HANDOFF.md`

End state claimed: **CODE READY → TESTS GREEN → SECURITY REVIEWED → PROVIDER CONTRACTS READY → CLOUD DEPLOYMENT IS THE NEXT STEP.**

This file is the operator brief. It does not invent live Google/Meta/AWS measurements.

---

## 1. Completed capabilities (application code)

Alive in `app/core/capabilities.py` unless noted. `AWS_RUNTIME` stays **specified**.

| Area | State |
| --- | --- |
| LangGraph sales brain, SalesState, Human Voice linter, Graph Lab evals | Complete (in-process). Live router decision deferred until scored. |
| Website widget, funnel events (`form_started` included), UTM attribution, WhatsApp handoff tokens | Complete. AssafWeb host attrs + localhost-in-dev widget path A. |
| SEO analysis, GA4 read, Search Console read, Firecrawl homepage audit | Complete as ports. Disabled when env empty. Never Measurement Protocol / GSC writes. `website_edit` persist-only. |
| WhatsApp Cloud API inbound + send + STT | Complete. Composio WhatsApp toolkit unused (ADR-006/015). |
| Instagram inbound (Meta HMAC) + one sender | Complete. Allowlist `direct` \| `manychat` \| `composio`. Default **`direct`**. |
| Instagram send + organic insights (ADR-015) | Complete. Composio toolkit `20260819_00` pins `INSTAGRAM_SEND_TEXT_MESSAGE`, `INSTAGRAM_GET_IG_USER_MEDIA`, `INSTAGRAM_GET_IG_MEDIA_INSIGHTS`. Graph remains the default-direct path. No publish/comments/captions/URLs. |
| ManyChat ingest sidecar | Complete. Dynamic Block only when sender=`manychat`. |
| Gmail ingest (Composio trigger + optional hydrate) | Complete. No send/delete. |
| Calendar free/busy, create, reschedule PATCH, local cancel request | Complete by fake + live Composio pins. `MIA_CALENDAR_WRITE` default false. Provider delete R5 denied. |
| Sheets mirror tabs 01–10 | Complete. Postgres is SoR. |
| LinkedIn profile (Composio) + member analytics (direct REST) | Complete as ports. Live member analytics needs approved token. |
| Meta Ads **read**, campaign analysis, pacing, prelaunch | Complete. Writes gated (R4). |
| Research search (Firecrawl) | Complete. No browser/crawl/Apify env. |
| Lead identity, memory/events, owner commands, voice-note transcription | Complete. Instructions **proposed only**. |
| Shadow mode, human takeover, approvals persist, kill switch, named write flags | Complete. Flags do not override R4/R5. |
| Outbound policies | Complete. Prospect send follows `MIA_AUTOMATION_MODE` (default shadow). Follow-up send unwired. |
| Idempotency, webhook HMAC, retries/fail-closed, reconciliation inspect | Complete. Replay still missing (intentional). |
| Postgres models + `mia-migrate` | Complete. Prod API does not `create_all`. |
| Observability: logs+redact, `ai_runs`, `tool_runs`, correlation, latency, tokens | Complete. `cost_usd` stays 0 (no price table). |
| Staging E2E (in-process fakes) | Complete for website, Instagram, WhatsApp, Calendar, Gmail ingest, Sheets, research, campaign analysis + §23 stories. Live OAuth/Meta writes stay gated. |

---

## 2. Remaining operator-only steps

These are not application-code blockers.

1. Fill laptop `.env` from `.env.example` (never commit; never paste keys in chat). Confirm with `done`, not by inspecting `.env`.
2. `uv run mia-migrate` on any existing Postgres/file sqlite.
3. Local loopback: `uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000` then `GET /health`, `/health/live`, `/health/ready`.
4. Connect Composio OAuth for Gmail, Calendar, Sheets, LinkedIn profile, Meta Ads, Instagram (if flipping sender), Search Console, GA4. `MIA_COMPOSIO_USER_ID` must match Composio debug `@user_id`.
5. Set `MIA_GSC_SITE_URL` and `MIA_GA4_PROPERTY_ID` when those connections exist (ports stay Disabled when empty).
6. Keep `MIA_INSTAGRAM_SENDER=direct` until a staging Composio DM send is proven. Then — and only then — consider `composio`.
7. Configure Meta Instagram Conversation Routing so a second app cannot reply.
8. Set Vercel `NEXT_PUBLIC_MIA_BASE_URL` to `https://mia.assafweb.com` **after** the ALB is live (not localhost in production).
9. AWS login (`aws login` / SSO) then follow `docs/PRODUCTION_BUILD.md` §3 **when Assaf approves AWS**.
10. Optional: unify AssafWeb contact form / FAB onto the handoff token (`docs/WEBSITE_MINIMIZATION_REPORT.md`) — recommendation only.

---

## 3. External credentials / scopes still required

Do not invent metrics if a port is Disabled.

| Job | Need | Scope / pin notes |
| --- | --- | --- |
| Postgres | `MIA_DATABASE_URL` | Production: RDS 16 + `sslmode=verify-full` |
| WhatsApp | verify token, app secret, optional send token + phone id | Owner phones exact set |
| Instagram inbound | app secret + verify token | HMAC. No Composio ingress |
| Instagram Graph send/insights | access token + account id | Used while sender=`direct` |
| Instagram Composio send/insights | Composio Instagram OAuth | Toolkit `20260819_00`. Flip sender only after staging test |
| ManyChat | ingest bearer | Only if sidecar or sender=`manychat` |
| Composio pool | API key + user id + webhook secret | Gmail, Calendar, Sheets, LinkedIn profile, Meta ads, IG send/insights, GSC, GA4 |
| Gmail | Composio Gmail connection | Ingest only. No send/delete |
| Calendar | Composio Calendar; write OAuth for CREATE/PATCH | Reads ungated; writes need `MIA_CALENDAR_WRITE` |
| Sheets | spreadsheet id + Composio Sheets | Upsert only. Never read back |
| Meta Ads read | ads account id | `METAADS_GET_INSIGHTS` only |
| LinkedIn profile | Composio LinkedIn | `LINKEDIN_GET_MY_INFO` |
| LinkedIn member analytics | `MIA_LINKEDIN_ACCESS_TOKEN` | `r_member_postAnalytics` app approval |
| Firecrawl | API key | Search + allowlisted homepage scrape |
| GSC | `MIA_GSC_SITE_URL` + Composio GSC | Read pins only |
| GA4 | `MIA_GA4_PROPERTY_ID` (`properties/{digits}`) + Composio GA4 | Never `SEND_EVENTS` |
| OpenAI / Gemini | sales + STT keys | Model ids stay env. Kill switch forces canned |
| AWS | CLI login + account | Not started |

---

## 4. Migrations to run

`uv run mia-migrate` applies `migrations/*.sql` in filename order. Sqlite skips Postgres-only `20260821_approval_campaign_resource.sql`. Duplicate column/table is treated as applied.

```
20260821_adr013_calendar_gate2.sql
20260821_ai_run_latency_ms.sql
20260821_ai_run_policy_version.sql
20260821_approval_binding.sql
20260821_approval_campaign_resource.sql
20260821_approval_object_fields.sql
20260821_canonical_correlation_id.sql
20260821_canonical_payload_version.sql
20260821_follow_up_draft.sql
20260821_human_takeover.sql
20260821_idempotency_inflight.sql
20260821_idempotency_records.sql
20260821_manychat_identity_ids.sql
20260821_owner_brief_booked.sql
20260821_owner_corrections.sql
20260821_owner_notifications.sql
20260821_shadow_decisions.sql
20260821_tool_run_correlation_id.sql
20260821_tool_run_freshness.sql
20260821_voice_transcript_retention.sql
20260821_voice_transcript_stt_meta.sql
20260821_webhook_envelope.sql
20260822_ai_run_automation_mode.sql
20260822_ai_run_decision_confidence.sql
20260822_ai_run_prompt_version.sql
20260822_existing_db_scan_columns.sql
20260822_lead_sales_state_company_domain.sql
20260822_seo_recommendations.sql
```

Prod first boot: one-off Fargate `mia-migrate` **before** `create-service`. `/health/ready` is 503 until mapped columns exist.

---

## 5. Production env / secrets checklist

Source of truth: `.env.example` (laptop) and `deploy/mia-prod.secret.example.json` (box). ECS injects JSON keys as `MIA_*`. Do not copy `.env` onto Fargate.

**KEEP (policy)**

- `MIA_ENV=prod`, `MIA_DEMO_MODE=false`, `MIA_KILL_SWITCH=false` for live
- `MIA_AUTOMATION_MODE=shadow`
- `MIA_CALENDAR_WRITE=false` until staging CREATE/PATCH proven
- `MIA_AUTO_FOLLOWUP=false`, `MIA_GMAIL_SEND=false`, `MIA_META_WRITE=false`
- `MIA_DYNAMIC_TOOL_DISCOVERY=false`, `MIA_BROWSER_AUTOMATION=false`, `MIA_AUTO_REPLY_INSTAGRAM=false`
- `MIA_INSTAGRAM_SENDER=direct`
- `MIA_CORS_ORIGINS=https://www.assafweb.com,https://assafweb.com`
- `MIA_PUBLIC_BASE_URL=https://mia.assafweb.com` (no trailing slash)

**SECRET (box)** — OpenAI, Gemini, Composio, Meta WhatsApp/IG, ManyChat, Firecrawl, LinkedIn member token, webhook secrets, `MIA_DATABASE_URL` with `sslmode=verify-full` + image CA path.

R4/R5 are **not** env knobs. Empty unused keys stay `""`.

---

## 6. Exact AWS deployment sequence

Do **not** start this until Assaf approves. Full commands: `docs/PRODUCTION_BUILD.md` §3. Region `il-central-1`.

0. Hygiene: `uv sync --group dev`; `uv run ruff check app tests`; `uv run pytest`; `aws login`; `deploy/assert-aws-identity.ps1` exit 0.
1. **VPC + RDS 16** (private, `PubliclyAccessible` false, `ManageMasterUserPassword`). Stamp `deploy/local/` via `fill-placeholders.ps1`. Never authorize `*.example.json`.
2. **Secrets Manager box** `mia/prod` from filled gitignored JSON. Delete the filled file.
3. **ECR image** from `deploy/Dockerfile`. GitHub Actions already builds; no ECR push from CI unless Assaf adds it.
4. **IAM execution role** + cluster + task definition. Do **not** `create-service` yet.
5. **ACM ISSUED** → ALB (public subnets) + target group `/health/live` + TLS 1.3 + HTTP→HTTPS 301 + idle 120s.
6. **One-off `mia-migrate`** via `deploy/ecs-migrate-overrides.example.json`.
7. **`create-service`** (`deploy/ecs-service.example.json`; public IP disabled; grace 120s; circuit breaker).
8. CloudWatch ALB alarms (no SNS this slice).
9. `GET https://mia.assafweb.com/health` → `"env":"prod"`, `"postgres":true`, `"public_https":true`. Then EventBridge due-scan 15m + reconcile hourly (persist-only).
10. Vercel `NEXT_PUBLIC_MIA_BASE_URL=https://mia.assafweb.com`. Register production webhooks.

Do not mark `CapabilityId.AWS_RUNTIME` alive until that health URL is green.

---

## 7. Staging test sequence after deployment

In-process suite already covers these paths with fakes (`tests/e2e/test_preprod_stories.py`). After AWS + credentials:

1. `GET /health/live` 200; `GET /health/ready` 200; `GET /health` booleans (no secrets).
2. Website: session + UTMs + `page_viewed` + message + handoff token. Confirm CORS from assafweb.com only.
3. WhatsApp verify GET + signed POST. Owner ack still sends under shadow; prospect DM does **not**.
4. Instagram signed POST. Confirm one sender. No dual-send with ManyChat.
5. Gmail Composio webhook ingest. Graph runs; **no** email send.
6. Calendar **read** slots on meeting offer. CREATE/PATCH only if Assaf sets `MIA_CALENDAR_WRITE=true` on a throwaway calendar.
7. Sheets: one inbound/website cycle upserts; never read the sheet back.
8. Owner WhatsApp `research` — titles+hosts only; snippets are data.
9. Owner WhatsApp campaign spend — recommendation line; **no** Meta write.
10. Owner `check seo` — omit Google lines if GSC/GA4 Disabled; do not invent metrics.
11. `mia-due-scan` and `mia-reconcile --inspect` — JSON only; never send/repair.

Leave unchecked: follow-up send, Gmail send, Meta writes, instruction activation, HYBRID, browser automation.

---

## 8. Known risks and intentionally gated features

**Gated (must stay off)**

- Gmail send / delete
- Meta campaign writes (R4 approval persist-only; `MIA_META_WRITE` unwired)
- Automatic follow-up send
- Owner instruction activation
- Browser automation / Playwright / Apify
- Provider calendar delete (R5)
- Dynamic Composio catalog discovery
- Production graph/prompt self-edit
- `MIA_AUTOMATION_MODE=auto_approved` in production (tests only)

**Intentionally deferred (not blockers for this package)**

- Live typed model router (`docs/MODEL_ROUTING_DECISION.md` only after scoring — do not invent results)
- Second STT provider + frozen audio set
- SQS / Lambda ingress / WAF / AgentCore / `app.infra`
- Reconciliation **replay**
- Versioned knowledge RAG
- `cost_usd` pricing table
- SNS pager / dashboards (ALB metric alarms exist, no pager)
- AssafWeb form/FAB raw `wa.me` unification
- Meta Conversation Routing proof (operator console)

**Highest operational risks once live**

1. Two Instagram senders if Graph, ManyChat, and Composio are connected together.
2. Synchronous webhook work (no SQS) — long graph/tool calls sit in the HTTP request.
3. Cursor Composio plugin OAuth ≠ Mia unless user ids match.

---

## 9. Security review (this close)

Covered in code + `tests/unit/test_security_review.py` + existing adversarial suites.

| Control | Evidence |
| --- | --- |
| Secrets | Env / SM box only. `redact()` on log structures. Tests assert keys not in send errors. |
| Permissions | `assert_allowed` before writes. Preloaded write pins: calendar create/PATCH, Sheets upsert, Instagram DM send only. |
| CORS | Allowlist; no `*`. AssafWeb origins required. |
| Prompt injection | Gmail/website/research scrape suites; E2E story 8. Untrusted text is data. |
| PII | Canonical payloads allowlisted. Handoff token hashed at rest. Insights drop captions/URLs. |
| Provider scopes | Pins exclude Gmail send, Meta write, GSC sitemap, GA4 `SEND_EVENTS`, IG publish. |

---

## 10. Verification commands

```
uv run ruff check app tests
uv run pytest
```

**Verified 2026-08-22:** ruff all checks passed. `uv run pytest` — **1860 passed** in 48.40s. Independent of live credentials.

Definition of done:

1. Ruff on `app` + `tests` — pass
2. Full test suite — 1860 passed
3. Remaining capabilities: all `ALIVE` except `AWS_RUNTIME` (**specified**, operator AWS). Gated writes stay off by policy, not missing code.
4. `docs/BUILD_STATUS.md`, `docs/HANDOFF.md`, `docs/RUNBOOK.md`, `docs/EXTERNAL_SETUP_CHECKLIST.md` match this report
5. No known application-code blocker for production deployment
6. This file is the operator brief

End state: **CODE READY → TESTS GREEN → SECURITY REVIEWED → PROVIDER CONTRACTS READY → CLOUD DEPLOYMENT IS THE NEXT STEP.**
