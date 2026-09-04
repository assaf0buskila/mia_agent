# Mia operations

Everything an operator does after the code is written: run it locally, deploy it,
migrate it, roll it back, stop it, and find out when it breaks.

Product: `docs/PRODUCT.md`. Architecture: `docs/ARCHITECTURE.md`. Decisions:
`docs/DECISIONS.md`.

Package manager is **uv**. Python `>=3.12`. This laptop is PowerShell — `;` not `&&`,
and the repo path contains a space, so `Set-Location` to the repo root first. Restart
the API process after env edits.

---

## Local

```
uv sync --group dev
uv run ruff check app tests
uv run pytest
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Fill `.env` from `.env.example`. Never commit `.env`. Never copy it onto Fargate.
Example files stay empty of real phones and tokens. Mia reads `MIA_`-prefixed names
only — `COMPOSIO_API_KEY` without the prefix is ignored.

`GET http://127.0.0.1:8000/health` → `"status": "ok"`. `GET /health/live` and
`GET /health/ready` → `{"status":"ok"}`. Leave
`MIA_PUBLIC_BASE_URL=http://127.0.0.1:8000` for this step.

If `.env` has `MIA_ENV=prod`, `GET /docs` is **404** — Swagger is off in prod. That is
not a failed API. Confirm the health JSON, not the FastAPI docs UI.

First-boot tables locally: `uv run mia-migrate`. Dev and test still `create_all` on
boot; prod does not.

**Widget looks wrong locally?** Check `GET /v1/website/widget.js` headers — the live
script must send `Cache-Control: no-cache` and must not send `etag` / `last-modified`
from an old `FileResponse`. Windows can leave a dead `127.0.0.1:8000` listener while
uvicorn is bound to `0.0.0.0:8000`, and loopback then serves the old widget. Open the
LAN IP of the new process, or reboot to drop the ghost bind.

**Webhook testing only.** Providers cannot POST to `127.0.0.1`. For a single session:

```
cloudflared tunnel --url http://127.0.0.1:8000
```

Paste that host into Meta / Composio for that session only. The hostname changes on
restart. Cloudflare Tunnel is **test only** — never in production DNS or the widget
script.

---

## What production is

ECS Fargate + RDS + Secrets Manager + ALB in **eu-north-1** (ADR-014, ADR-019).

| Layer | Production value |
| --- | --- |
| Process | ECS Fargate container: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips * --timeout-keep-alive 130` (no `--reload`; keep-alive **above** the ALB idle timeout of 120s) |
| Image | `deploy/Dockerfile` → ECR |
| Database | RDS PostgreSQL 16, private subnets, public access off (`MIA_DATABASE_URL` from the box) |
| Public URL | `https://mia.assafweb.com` via ALB + ACM |
| Keys | Secrets Manager secret `mia/prod` (the box). ECS injects `MIA_*`. Never git, never chat |
| Env | `MIA_ENV=prod` and `MIA_DEMO_MODE=false` — never prod+demo together |
| Prospect send | `MIA_AUTOMATION_MODE=auto_approved` (ADR-022). Unknown WhatsApp still silent. Instagram send stays off |
| Calendar writes | `MIA_CALENDAR_WRITE=true`. Exact create/move proposals execute only after a one-tap Telegram approval |
| Gmail / Meta send | Gmail draft send is approval-only and additionally requires `MIA_GMAIL_SEND=true`; Meta writes stay off. R4/R5 are not env knobs |
| Kill switch | `MIA_KILL_SWITCH=false` for live |
| Networking | ALB in 2 public subnets; ECS + RDS in 2 private subnets; NAT in 1 AZ. SGs: ALB 80/443 from the internet, tasks 8000 from the ALB SG only, RDS 5432 from the tasks SG only. Never 8000 or 5432 open to `0.0.0.0/0` |
| Target group | **IP** (Fargate `awsvpc`), HTTP:8000, health `/health/live`, deregistration delay 30s, idle timeout 120s |
| MultiAZ | **false**, deliberately — it roughly doubles the cost and one operator can tolerate the failover window. An AZ failure is an outage, not a blip |
| Lambda / AgentCore / SQS / WAF | Specified later. Not deployed |

Prod API lifespan skips `create_all`, so `/health/live` binds without waiting on the
schema. Existing databases must run `mia-migrate` for SQL column files.

Not production: Vercel, Cloudflare Workers, trycloudflare, copying `.env` onto a VPS,
Caddy or systemd unit files (the ALB terminates TLS). Do not put LangGraph on Lambda.
Do not use a Lambda as the key box — Secrets Manager is the box.

Rotating `mia/prod` does **not** update a running task. Force a new deployment.

### If the stack ever has to be rebuilt

Order is required, and the reason is in the order itself: `create-service` needs a
target group ARN, the HTTPS listener needs an **ISSUED** ACM certificate, and the
schema must exist before the service takes traffic.

```
VPC / RDS  →  secret box mia/prod  →  image to ECR  →  IAM + cluster + task definition
           →  ACM ISSUED + ALB + target group  →  migrate  →  create-service  →  alarms
```

The same order as commands. Every `--cli-input-json` is `file://./deploy/local/<name>.json`
after `fill-placeholders.ps1`, run from the repo root — `file://deploy/local/...` makes the
CLI treat `deploy` as a host. Never point one at a `*.example.json`: those still carry
`sg-MIA_*` tokens.

```bash
aws iam create-role --role-name miaTaskExecutionRole --assume-role-policy-document file://./deploy/local/iam-ecs-task-trust.json
aws ecs create-cluster --cluster-name mia
aws ecs register-task-definition --cli-input-json file://./deploy/local/ecs-task-definition.json
aws acm request-certificate --cli-input-json file://./deploy/local/acm-certificate.json
aws elbv2 create-load-balancer --cli-input-json file://./deploy/local/alb.json
aws elbv2 wait load-balancer-available --load-balancer-arns LOAD_BALANCER_ARN
aws elbv2 create-target-group --cli-input-json file://./deploy/local/alb-target-group.json
aws elbv2 create-listener --cli-input-json file://./deploy/local/alb-listener-https.json
aws ecs run-task --cluster mia --launch-type FARGATE --task-definition mia --overrides file://./deploy/local/ecs-migrate-overrides.json
aws ecs create-service --cli-input-json file://./deploy/local/ecs-service.json
```

`wait tasks-stopped` is not success: the migrate task's printed **exit code must be 0**.
Anything else means the schema is not there — read CloudWatch `/ecs/mia` and do **not**
`create-service`.

The JSON templates live in `deploy/*.example.json`. `deploy/fill-placeholders.ps1`
stamps account, region, VPC, subnet, SG, ALB/target-group hashes, cert id and Route 53
zone id into `deploy/local/`; `deploy/assert-local-stamped.ps1` exits 1 while any token
remains, and `deploy/assert-aws-identity.ps1` gates on a real caller identity before
anything is created. Neither assert script calls AWS or reads `.env`. Pass every file
as `file://./deploy/local/<name>.json` — `file://deploy/local/...` makes the AWS CLI
treat `deploy` as a host. Never authorize or `create-db-instance` against
`*.example.json`; those still contain `sg-MIA_*` tokens.

Landing-page Vercel sets `NEXT_PUBLIC_MIA_BASE_URL=https://mia.assafweb.com`.

---

## Health

```
GET {MIA_PUBLIC_BASE_URL}/health
```

Expect `status ok`, `"env": "prod"`, `"demo": false`, `"postgres": true`,
`"public_https": true`, `deployment.commit_sha` matching what you deployed, and
`brain.corpus.knowledge_chunks` non-zero. `/docs` is 404.

`owner_integrations` true means house Composio is wired — Telegram must not then say
those apps are disconnected. `composio` must be true and `MIA_COMPOSIO_USER_ID` must
equal the Composio debug `@user_id`; a Cursor Composio plugin showing Calendar Active
does not prove Mia Calendar.

`/health/ready` 503 means migrate did not finish. Do not point webhooks at it.

`/health` answers "is the config filled in", not "is Mia working". In particular
`ops.failed_sends` counts only an owner turn that broke **and** could not deliver its
own apology; it does not count a failed customer reply.

---

## Deploy

CI never deploys. The `deploy` job in `.github/workflows/ci.yml` ends in `exit 1` on
purpose, and the `image` job builds without pushing. Every deploy is manual, and the
order is not optional: the schema must lead the code.

```
test  ->  build  ->  migrate  ->  deploy  ->  smoke  ->  verify
```

1. **Test.** Wait for green CI on a SHA, then `git checkout` that SHA. The image is
   built from the working tree, so a dirty tree would ship. `deploy_ecs_revision.py`
   refuses a dirty tree or a SHA that does not match HEAD, but check out the tested
   commit anyway.

2. **Build** with the commit stamped in, and push to ECR:
   ```
   SHA=$(git rev-parse HEAD)
   docker build -f deploy/Dockerfile --build-arg MIA_BUILD_SHA=$SHA -t mia:N .
   docker tag mia:N <ECR>/mia:N && docker push <ECR>/mia:N
   ```
   Without `--build-arg`, `/health` reports an empty `deployment.commit_sha` and the
   smoke test fails check A, which is the point.

3. **Migrate, before the service moves.** New columns and tables must exist before the
   new code serves a request. `mia-migrate` is additive and records each file in
   `schema_migrations`, so re-running it is safe and already-applied files are skipped.
   ```
   python scripts/run_ecs_migration.py --task-definition mia
   ```
   If this fails, stop. Do not move the service. A partly applied migration set cannot
   be rolled back, so fix forward from the current schema.

4. **Register and deploy:**
   ```
   python scripts/deploy_ecs_revision.py --tag N --sha $SHA
   aws ecs update-service --cluster mia --service mia --force-new-deployment
   ```

5. **Smoke.** This has a real exit status and is the release gate:
   ```
   python scripts/smoke_production.py --sha $SHA
   ```
   Non-zero means roll back. It checks that production reports the commit you just
   deployed, that a real conversation reaches an offer instead of asking questions
   forever, that a price question never invents a number, that R5 reports the current
   policy, and that a normal turn answers. It creates no CRM row and sends no Telegram
   notification.

6. **Verify** `GET /health` as above.

The ECS deployment circuit breaker is on, so a container that fails `/health/live`
rolls back by itself. It catches "will not boot". The smoke test is what catches
"boots fine and answers badly".

---

## Migrations

`migrations/` is additive only. There are no down steps, and each file commits on its
own — so a failure leaves every earlier file applied.

**Take a manual RDS snapshot before running `mia-migrate` on anything you cannot lose.**

`20260904_website_session_state.sql` creates `website_session_state`. The website turn
reads and writes that table on every message, so deploying the code without running the
migration first will error on the first visitor.

---

## Rollback

Register a revision pointing at the previous good tag and move the service to it:

```
python scripts/deploy_ecs_revision.py --tag PREVIOUS_GOOD
aws ecs update-service --cluster mia --service mia --force-new-deployment
```

For a bad model or a bad prompt, blanking the relevant `MIA_*` model id and forcing a
new deployment is faster than rebuilding an image.

**A bad migration cannot be rolled back.** The only recovery is an RDS point-in-time
restore.

---

## Kill switch

`MIA_KILL_SWITCH=true` then restart. High-risk writes stay denied. Owner Telegram talk
and website chat stay up. `GET /health/live` stays process-up and `/health` reports
`"killed"`.

Restore: `MIA_KILL_SWITCH=false`, restart, confirm `/health` `"status": "ok"`.

Narrower stops:

| Need | Action |
| --- | --- |
| Stop prospect DMs only | `MIA_AUTOMATION_MODE=shadow` + restart |
| Stop calendar provider writes | `MIA_CALENDAR_WRITE=false` + restart |
| Human takeover | Assaf on Telegram or WhatsApp. No `lead_` ids |
| Stale webhooks | `uv run mia-reconcile --inspect` (no replay) |

---

## One-off jobs and knowledge ingest

`scripts/run_ecs_command.py` runs any container command as a one-off Fargate task. It
reuses the running service's network config, so the task definition is the only
argument you need:

```
aws ecs describe-services --cluster mia --services mia --query 'services[0].taskDefinition' --output text
python scripts/run_ecs_command.py --task-definition TASK_DEF -- mia-ingest-knowledge
```

This is the only way to run `mia-ingest-knowledge`, `mia-sheets-maintain` and
`mia-telegram-webhook` in production.

`mia-ingest-knowledge` chunks and embeds the published AssafWeb facts
(`llms.txt`, `llms-full.txt`, `pricing.md`). It is the one store that can be rebuilt
from scratch. After it runs, `/health` `brain.corpus.knowledge_chunks` must be
non-zero.

> `run_ecs_command.py --help` uses `mia-wipe-data` as its example. That command
> truncates every table and its only guard is typing `--confirm fresh-start`. It does
> not check the environment. Snapshot first.

Scheduled persist-only jobs run on EventBridge Scheduler in the same private network:
due-scan `rate(15 minutes)`, reconcile `rate(1 hour)`. Execute-command stays off.
Until those schedules exist, do not pretend cron is running. Never send from these
CLIs.

```
uv run mia-migrate
uv run mia-due-scan
uv run mia-reconcile
uv run mia-reconcile --inspect
```

---

## Webhook registration

Use `{MIA_PUBLIC_BASE_URL}`, not a tunnel.

| Channel | Path |
| --- | --- |
| Telegram | `POST {base}/v1/telegram/webhook` (`X-Telegram-Bot-Api-Secret-Token`) |
| WhatsApp | `POST {base}/v1/whatsapp/webhook` (GET verify on the same path) |
| Instagram | `POST {base}/v1/instagram/webhook` (analytics; not a v1 sales inbox) |
| Composio Gmail ingest | `POST {base}/v1/composio/webhook` |

**Telegram** is the one that must be right. `setWebhook` to
`POST {base}/v1/telegram/webhook` with `secret_token` matching
`MIA_TELEGRAM_WEBHOOK_SECRET`. Owner access is **numeric**
`MIA_TELEGRAM_OWNER_USER_IDS` only — a username never grants access. In production
this is done with `mia-telegram-webhook` through `run_ecs_command.py`.

Also confirm: `MIA_WHATSAPP_VERIFY_TOKEN` + `MIA_WHATSAPP_APP_SECRET` for inbound HMAC
(ADR-016); one Instagram sender only, never Graph + Composio together; Composio
`MIA_COMPOSIO_API_KEY` + `MIA_COMPOSIO_USER_ID` + `MIA_COMPOSIO_WEBHOOK_SECRET`, then
restart and check `/health` `composio` and `composio_webhook`; the Composio project's
OAuth user verification URL is **empty** (Mia has no verify route, and a value there
blocks dashboard connections); and no Meta icebreaker overlapping Mia.

**Website widget** on assafweb.com:

```html
<script
  src="{MIA_PUBLIC_BASE_URL}/v1/website/widget.js"
  data-mia-api="{MIA_PUBLIC_BASE_URL}"
  defer
></script>
```

`data-mia-api` is required when the host loads the script via Next.js `<Script>` or any
loader where `document.currentScript` is null. CORS must allow
`https://www.assafweb.com` and `https://assafweb.com`. Host funnel attributes:
`data-mia-section`, `data-mia-cta`, `form[data-mia-form]`.

---

## Database safety

Everything Mia knows is in Postgres: every conversation, every lead's sales state,
every approval, every memory and knowledge embedding, and the website session state.
Only the knowledge corpus can be rebuilt, with `mia-ingest-knowledge`. Contacts survive
only as far as they were mirrored to the Sheet.

`deploy/rds.example.json` is the template, not the live instance. Applying it to the
running database is a separate act:

```
aws rds modify-db-instance --db-instance-identifier mia \
  --deletion-protection --backup-retention-period 14 --apply-immediately
```

Deletion protection was off. That is one API call, or one console misclick, from losing
the business. Turn it on before anything else on this page.

Snapshot before anything irreversible — a migration, a schema change, or
`mia-wipe-data`:

```
aws rds create-db-snapshot --db-instance-identifier mia \
  --db-snapshot-identifier mia-before-CHANGE-$(date +%Y%m%d%H%M)
```

Restore is a new instance, never in place, so it also means repointing
`MIA_DATABASE_URL` and forcing a new deployment:

```
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier mia \
  --target-db-instance-identifier mia-restored \
  --restore-time 2026-09-04T12:00:00Z
```

---

## Alarms

Nothing pages you until the steps below are run. The alarm definitions exist and are
inert: the only alarm with an SNS action is the ALB 5xx one, and it points at a
placeholder ARN. **This has not been applied for you** — it needs AWS credentials, and
creating a topic that pages a real phone is not something to do automatically.

One time, to create the destination:

```
aws sns create-topic --name mia-ops
aws sns subscribe --topic-arn arn:aws:sns:REGION:ACCOUNT_ID:mia-ops \
  --protocol email --notification-endpoint YOU@example.com
```

Confirm the subscription from the email, then turn the log lines into metrics. Each
entry in `deploy/cloudwatch-metric-filters.example.json` becomes one call:

```
aws logs put-metric-filter --log-group-name /ecs/mia \
  --filter-name mia-owner-agent-silent \
  --filter-pattern '"owner_agent used=False"' \
  --metric-transformations \
    metricName=OwnerAgentSilent,metricNamespace=Mia,metricValue=1,defaultValue=0
```

`defaultValue=0` matters: without it the metric is absent rather than zero when Mia is
idle, and an alarm cannot tell healthy from no-data.

Then create the alarms, replacing the placeholder ARN in
`deploy/cloudwatch-mia-alarms.example.json` with the real topic:

```
aws cloudwatch put-metric-alarm --cli-input-json file://alarm.json
```

The ALB alarms are `deploy/cloudwatch-alb-unhealthy.example.json` (statistic
**Minimum**, two datapoints) and `deploy/cloudwatch-alb-5xx.example.json` (pages SNS
once `MIA_ALB_5XX_SNS_TOPIC_ARN` is stamped into `AlarmActions`; an empty ARN is
fail-closed — do not `put-metric-alarm` with a blank action).

Verify one end to end before trusting it:

```
aws cloudwatch set-alarm-state --alarm-name mia-owner-agent-silent \
  --state-value ALARM --state-reason "smoke test"
```

If that does not reach your phone, nothing else on this page will either.

---

## Incident response

1. **Stop the bleeding.** If writes are the problem: `MIA_KILL_SWITCH=true` + restart.
   If a deploy is the problem: roll back to the previous good tag. If one channel is
   the problem, use the narrower stop above instead of killing everything.
2. **Find out what actually happened.** CloudWatch log group `/ecs/mia`. `GET /health`
   for config truth and the deployed commit. `uv run mia-reconcile --inspect` for
   webhook state, which inspects and does not replay.
3. **Do not fix a schema failure by rolling back.** A partly applied migration set has
   no down step. Fix forward, or restore point-in-time.
4. **Confirm the fix with the smoke test**, not by reading a log:
   `python scripts/smoke_production.py --sha $SHA`.
5. **Say what happened.** If a decision came out of it, it is an ADR — see
   `docs/DECISIONS.md`.

---

## WhatsApp: Baileys sidecar

A Baileys WhatsApp transport exists at `services/whatsapp-baileys`. It is **built,
parked, and not deployed**. It is not part of the running stack, nothing routes to it,
and it must be pointed at a spare number first, because the ban risk lands on the
linked number (ADR-053).

Until official Cloud API inbound, WhatsApp is Assaf's human inbox (ADR-024). Mia
composes; Assaf sends.

---

## What not to do

Do not copy `.env` onto Fargate. Do not dump secrets. Do not paste tokens or a
`SecretString` into chat. Do not auto-deploy. Do not invent metrics or prices. Assaf
sends customer WhatsApp.
