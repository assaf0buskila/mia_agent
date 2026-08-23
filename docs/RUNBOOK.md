# Operator runbook — Mia

**Date:** 2026-08-23  
**Status:** Gate F partial (this file + named-flag table + first-live ALB metric alarms without SNS). Dashboards, SNS pager, LangSmith, and automated paging are **not** in this repo.  
**Not a grant of write access.** Does not enable Gmail send, Meta writes, follow-up send, instruction activation, Lambda/SQS/AgentCore, or `app.infra`.

Go-live order: `docs/PRODUCTION_BUILD.md`. Related: `.env.example`, `docs/ARCHITECTURE.md`, `docs/PROJECT_MAP.md`.

Package manager is **uv**. PowerShell: use `;` not `&&`. Restart the API process after env edits.

```
uv run uvicorn app.main:app --reload
GET {MIA_PUBLIC_BASE_URL}/health
```

Local webhook **test** (Cloudflare Tunnel only; not production):

```
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
cloudflared tunnel --url http://127.0.0.1:8000
GET https://<random>.trycloudflare.com/health
```

Paste the tunnel host into Meta/Composio for that session. Hostname changes on restart. Do not treat Cloudflare as the production owner.

`GET /health` WhatsApp keys (ADR-016): `whatsapp_provider` is the **outbound** owner (`meta` or `composio`); inbound is always Meta. `whatsapp_ingest` is Meta verify+app secret only — never true just because a Composio key exists. `whatsapp_send` means the chosen sender would not be Disabled. `whatsapp_connected` is outbound auth presence, not ingest.

ADR-017 communication keys: `website_chat` (widget path is mounted), `telegram_owner` (bot token + webhook secret + numeric owner ids — not “key exists” alone), `email_read` (`composio` ready), `email_send_policy` always `approval`, `automation_mode`. `ops` counts: `pending_approvals`, `human_takeover`, `failed_sends`, `integration_failures` (open reconciliation).

---

## 1. Emergency stop (business kill switch)

The only live emergency env stop is **`MIA_KILL_SWITCH`**. R4 stays approval and R5 stays deny in `app/core/risk.py` — there is no `MIA_R4_AUTO` / `MIA_R5_ALLOW`.

1. Set `MIA_KILL_SWITCH=true` in the process environment (or `.env`).
2. Restart uvicorn (or the host process). Do not assume a running process reloads `.env`.
3. Confirm `GET /health` returns `"status": "killed"` and `"kill_switch": true`. `GET /health/live` stays `"ok"` if the process is up. `GET /health/ready` is independent of the kill switch (DB ping only).

### What it does

| Surface | Behavior |
| --- | --- |
| Telegram owner webhook | Secret still verified, then **early return** `{killed: true}` — no owner classify, no send |
| WhatsApp / Instagram / Composio (Gmail) webhooks | Signature still verified, then **early return** `{killed: true, processed: 0}` — no graph, no send, no claim |
| Website Ask Mia | Graph still runs; compose is canned; gated writes (Sheets, calendar create) skip |
| Named write flags | Ignored — kill switch wins |
| `mia-due-scan` | Follow-up send-readiness denied (`kill_switch`); still **never sends** |
| `mia-reconcile` | Skips persist (same as demo) |
| Owner WhatsApp / Telegram (including takeover) | **Not processed** — webhook returns killed before classify. Park the thread in the provider inbox. |

Meta/Composio will retry unacked webhooks. That is expected while killed.

### Restore

Set `MIA_KILL_SWITCH=false`, restart, confirm `/health` `"status": "ok"`.

---

## 2. Conversation-level stops (not the env switch)

These are **not** `MIA_KILL_SWITCH`. Do not confuse them.

| Control | How | Effect |
| --- | --- | --- |
| Conversation kill (`leads.conversation_killed`) | Sales NBA `stop` (prospect opt-out) | Follow-up send-readiness denies; later non-stop NBA clears it |
| Human takeover (`leads.takeover_state` / `human_takeover`) | Owner Telegram or WhatsApp phrase **and** `lead_<12 hex>` in the same message | Prospect `MessagePort` skip; pending follow-ups cancelled; graph + `ai_runs` still run; website HTTP unchanged |
| Resume | Owner Telegram or WhatsApp resume phrase **and** the same `lead_*` | Clears takeover; outbound follows policy again |

**Takeover phrases** (whole-message match after normalize): `human takeover`, `take over this lead`, `אני לוקח את הליד`, `תפיסה אנושית`.

**Resume phrases** (must stay disjoint from takeover): `resume this lead`, `release this lead`, `mia can reply`, `שחרר את הליד`, `החזר למיאה`.

Example: `Take over this lead lead_ab12cd34ef56` (Telegram or owner WhatsApp). Missing `lead_*` → Understanding Check, no park. While `MIA_KILL_SWITCH=true`, owner Telegram/WhatsApp is not processed at all — park in the provider inbox.

---

## 3. Automation mode (prospect send only)

`MIA_AUTOMATION_MODE` (production live test **`auto_approved`**, ADR-022). Does **not** override R4, R5, or kill switch. Instagram send is a separate flag.

| Value | Prospect WhatsApp send |
| --- | --- |
| `shadow` | Graph runs; skip `MessagePort` unless verified handoff send flag; persist `shadow_decisions` |
| `auto_approved` | Send when R2 allows and WhatsApp scope is allowed |
| `off` | No extra shadow gate |
| `draft_only` | Due-scan draft only; never send |
| `hybrid` | **Not wired** — do not invent it |

Instagram prospect send requires `MIA_AUTO_REPLY_INSTAGRAM=true` even under `auto_approved`. Website HTTP replies are unchanged by this knob. Owner WhatsApp acks still send under shadow (`actor_role=owner`).

To stop prospect DMs without a full kill: set `shadow` + new task revision. That does not stop website canned replies.

---

## 4. Named write flags

All default **false**. `named_write_may_auto` returns false for R4/R5 always. Flags never override kill switch.

| Flag | Wired | Operator meaning |
| --- | --- | --- |
| `MIA_CALENDAR_WRITE` | **Yes** — create + reschedule PATCH | Staging booking/reschedule only after Calendar write OAuth. Reads (free/busy, GET) are not gated. |
| `MIA_WHATSAPP_HANDOFF_SEND` | Yes (shadow bypass) | When true, verified `MIA_BUSINESS` WhatsApp continuation may send under shadow. Unused while production is `auto_approved`. |
| `MIA_GMAIL_SEND` | No | Ingest only. |
| `MIA_META_WRITE` | No | Cannot override R4. Campaign execute gated. |
| `MIA_AUTO_REPLY_INSTAGRAM` | Yes (send gate) | Default **false**. Instagram is not a v1 sales inbox. `auto_approved` does not open it. |

Rollout modes (percentage / allowlist) do not exist. Kill switch is global, not per capability.

---

## 5. Instagram one-sender rule

Highest operational risk if Graph and Composio both send.

- Set **exactly one** of `MIA_INSTAGRAM_SENDER=direct` or `composio`. Production default is `direct`. Flip to `composio` only after staging send is tested (ADR-015).
- Instagram inbound stays Meta HMAC webhook. Not a v1 sales inbox (ADR-017). `MIA_AUTO_REPLY_INSTAGRAM=false`.
- ManyChat is not mounted. A leftover `MIA_MANYCHAT_INGEST_TOKEN` in Secrets Manager is unused — do not delete it from AWS without Assaf.

---

## 6. Schema migrate (existing DBs)

```
uv run mia-migrate
```

Applies `migrations/*.sql` in filename order. Sqlite skips Postgres-only `20260821_approval_campaign_resource.sql`. Duplicate columns are treated as already applied. JSON only (`applied` / `skipped` / `already` / `failed`). Never sends. Run this on an existing file-sqlite or Postgres after pulling new SQL files. Prod API does not `create_all` on boot — `mia-migrate` is required. Dev/test `init_db()` still creates missing tables. `/health/ready` is 503 until mapped columns exist. Existing sqlite also needs `20260822_existing_db_scan_columns.sql` (`owner_tasks.due_ready`/`block_reason`, `webhook_events.claimed_at`, `lead_follow_ups.send_ready`/`block_reason`) after `company_domain`. ADR-017: `20260822_conversation_controls.sql` (`leads.takeover_state` + `conversation_controls`).

---

## 7. Due scan (persist-only)

```
uv run mia-due-scan
```

Also: `uv run python -m app.workers.due_scan`.

Prints JSON counts only: `follow_ups_scanned`, `follow_ups_send_ready`, `owner_tasks_scanned`, `owner_tasks_due_ready`. Uses `MIA_CALENDAR_TIMEZONE` (default `Asia/Jerusalem`) and `MIA_CAMPAIGN_MONTHLY_BUDGET` vs `campaign_pacing.spend` for spend-threshold owner tasks. **Never sends. Never executes owner tasks.** `send_ready=true` is not permission to send.

Kill switch: follow-up rows are not send-ready. Owner-task scan still records `due_ready` (`owner_task_scan` asserts with `kill_switch=False`); execute remains gated. While the API is killed, do not expect owner WhatsApp to consume scan results.

---

## 8. Reconciliation (flag-only)

```
uv run mia-reconcile
```

Also: `uv run python -m app.workers.reconcile`.

Prints JSON counts: `webhook_received`, `sent_without_out`, `handoff_expired`. Upserts `reconciliation_findings`; a later clean scan closes a finding (`open=false`). **Never repairs, never `mark_webhook`, never sends, never consumes handoff tokens, never Sheets read-back, never Meta/calendar writes.** Kill switch and demo skip persist.

Inspect open findings (read-only SoR, no replay):

```
uv run mia-reconcile --inspect
```

Adds `open_count` and `open_findings` (`kind` + `subject_key` + sanitized webhook `channel`/`envelope_kind`, sorted, cap 50). `open_count` is the listed length, not a full-table count. Subject keys are `{provider}:{provider_event_id}` or handoff token hash — no message text. Handoff findings leave channel/kind empty. Default CLI without `--inspect` stays counts-only.

Stale `received` means empty/unparseable `claimed_at` or older than 300s. Fresh in-flight webhooks are not findings.

Replay / provider repair is not implemented. Do not invent a DLQ worker.

---

## 9. Rollback (what exists today)

Laptop: restart uvicorn after env edits. Production (ADR-014): new ECS task definition revision (kill switch and KEEP flags live in task `environment`; SECRET rotation in `mia/prod` also needs a new deployment because ECS injects secrets at start). No canary or dashboard rollback in this repo. CloudWatch alarms still missing.

1. **Stop writes:** `MIA_KILL_SWITCH=true` + restart / new task revision (section 1).
2. **Stop calendar provider writes:** `MIA_CALENDAR_WRITE=false` + restart / new task revision. Local meeting rows are not deleted (R5).
3. **Stop prospect DMs:** `MIA_AUTOMATION_MODE=shadow` + restart / new task revision.
4. **Code rollback:** revert the git commit, build/push a previous image, point the ECS service at that task definition. Do not `reset --hard` production data unless Assaf asks.
5. **Do not** identity-unmerge, provider-delete calendar events, or enable gated sends as a “fix”.

Postgres is the system of record. Sheets is a living snapshot; never read it back into SalesState.

---

## 10. Local live check (not production)

Fill `.env` from `.env.example` for laptop only. Production keys live in Secrets Manager `mia/prod`. `MIA_ENV=prod` must never pair with `MIA_DEMO_MODE=true`. For a local test against [assafweb.com](https://www.assafweb.com/): `MIA_KILL_SWITCH=false`. Widget: `GET /v1/website/widget.js` — AssafWeb already embeds `AskMiaWidget`; Vercel `NEXT_PUBLIC_MIA_BASE_URL=https://mia.assafweb.com` (HTTPS public origin only; localhost rejected). Local look: `GET /v1/website/preview` (LAN bind, not `127.0.0.1` if a leftover listener exists). Prod unmounts `/docs` `/redoc` `/openapi.json` — use `/health`. If loopback widget.js lacks `Cache-Control: no-cache`, a leftover `127.0.0.1:8000` process is serving old bytes.

```
uv sync --group dev
uv run pytest
uv run ruff check app tests
```

---

## 11. Still missing (do not pretend otherwise)

- CloudWatch / LangSmith / Langfuse dashboards and alerts
- Queue age, DLQ, auth-failure-spike, latency-SLA, cost-anomaly pages
- Spend-without-leads as an **ops alert** (owner-ack text exists; no pager)
- Per-capability kill, percentage rollout, AgentCore, Lambda webhook ingress, SQS, WAF
- Dead-letter replay

If those are required for a production go-live, they are new work — not this file.
