# Operator runbook — Mia

Go-live order: `docs/PRODUCTION_BUILD.md`. Product: `docs/PRODUCT.md`.

Package manager is **uv**. Restart the API process after env edits.

```
uv run uvicorn app.main:app --reload
GET {MIA_PUBLIC_BASE_URL}/health
```

`GET /health` `owner_integrations` true means house Composio is wired. Telegram must not say those apps are disconnected.

## Emergency stop

`MIA_KILL_SWITCH=true` then restart. High-risk writes stay denied. Owner Telegram talk and website chat stay up. `GET /health/live` stays process-up.

Restore: `MIA_KILL_SWITCH=false`, restart, confirm `/health` `"status": "ok"`.

## Deploy

CI never deploys. The `deploy` job in `.github/workflows/ci.yml` ends in `exit 1`
on purpose, and the `image` job builds without pushing. Every deploy is manual, and
the order below is not optional: the schema must lead the code.

```
test  ->  build  ->  migrate  ->  deploy  ->  smoke  ->  verify
```

1. **Test.** Wait for green CI on a SHA, then `git checkout` that SHA. The image is
   built from the working tree, so a dirty tree would ship. `deploy_ecs_revision.py`
   now refuses a dirty tree or a SHA that does not match HEAD, but check out the
   tested commit anyway.

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
   be rolled back (see below), so fix forward from the current schema.

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
   policy, and that a normal turn answers. It creates no CRM row and sends you no
   Telegram notification.

6. **Verify** `GET /health`: `status ok`, `deployment.commit_sha` matches, and
   `brain.corpus.knowledge_chunks` is non-zero.

The ECS deployment circuit breaker is on, so a container that fails `/health/live`
rolls back by itself. It catches "will not boot". The smoke test is what catches
"boots fine and answers badly".

### Migrations pending in this release

`20260904_website_session_state.sql` creates `website_session_state`. The website turn
reads and writes that table on every message, so deploying the code without running the
migration first will error on the first visitor.

## Rollback

Register a revision pointing at the previous good tag and move the service to it:

```
python scripts/deploy_ecs_revision.py --tag PREVIOUS_GOOD
aws ecs update-service --cluster mia --service mia --force-new-deployment
```

For a bad model or a bad prompt, blanking the relevant `MIA_*` model id and
forcing a new deployment is faster than rebuilding an image.

A bad migration cannot be rolled back. `migrations/` is additive only, with no
down steps, and each file commits on its own — so a failure leaves every earlier
file applied. The only recovery is an RDS point-in-time restore. Take a manual
snapshot before running `mia-migrate` on anything you cannot lose.

## One-off jobs

`scripts/run_ecs_command.py` runs any container command as a one-off Fargate task.
It reuses the running service's network config, so the task definition is the only
argument you need:

```
aws ecs describe-services --cluster mia --services mia --query 'services[0].taskDefinition' --output text
python scripts/run_ecs_command.py --task-definition TASK_DEF -- mia-ingest-knowledge
```

This is the only way to run `mia-ingest-knowledge`, `mia-sheets-maintain` and
`mia-telegram-webhook` in production. Its own `--help` uses `mia-wipe-data` as the
example; that command truncates every table and its only guard is typing
`--confirm fresh-start`. It does not check the environment. Snapshot first.

## Alarms

Nothing pages you until the steps below are run. The alarm definitions exist and are
inert: the only alarm with an SNS action is the ALB 5xx one, and it points at a
placeholder ARN. **This has not been applied for you** — it needs your AWS
credentials, and creating a topic that pages a real phone is not something to do
automatically.

One time, to create the destination:

```
aws sns create-topic --name mia-ops
aws sns subscribe --topic-arn arn:aws:sns:REGION:ACCOUNT_ID:mia-ops   --protocol email --notification-endpoint YOU@example.com
```

Confirm the subscription from the email, then turn the log lines into metrics. Each
entry in `deploy/cloudwatch-metric-filters.example.json` becomes one call:

```
aws logs put-metric-filter --log-group-name /ecs/mia   --filter-name mia-owner-agent-silent   --filter-pattern '"owner_agent used=False"'   --metric-transformations     metricName=OwnerAgentSilent,metricNamespace=Mia,metricValue=1,defaultValue=0
```

`defaultValue=0` matters: without it the metric is absent rather than zero when Mia is
idle, and an alarm cannot tell healthy from no-data.

Then create the alarms, replacing the placeholder ARN in
`deploy/cloudwatch-mia-alarms.example.json` with the real topic:

```
aws cloudwatch put-metric-alarm --cli-input-json file://alarm.json
```

Verify one end to end before trusting it:

```
aws cloudwatch set-alarm-state --alarm-name mia-owner-agent-silent   --state-value ALARM --state-reason "smoke test"
```

If that does not reach your phone, nothing else on this page will either.

`/health` answers "is the config filled in", not "is Mia working". In particular
`ops.failed_sends` counts only an owner turn that broke *and* could not deliver its
own apology; it does not count a failed customer reply.

## Database safety

Everything Mia knows is in Postgres: every conversation, every lead's sales state,
every approval, every memory and knowledge embedding, and the website session state.
Only the knowledge corpus can be rebuilt, with `mia-ingest-knowledge`. Contacts
survive only as far as they were mirrored to the Sheet.

`deploy/rds.example.json` is the template, not the live instance. Applying it to the
running database is a separate act:

```
aws rds modify-db-instance --db-instance-identifier mia   --deletion-protection --backup-retention-period 14 --apply-immediately
```

Deletion protection was off. That is one API call, or one console misclick, from
losing the business. Turn it on before anything else on this page.

Take a snapshot before anything irreversible — a migration, a schema change, or
`mia-wipe-data`:

```
aws rds create-db-snapshot --db-instance-identifier mia   --db-snapshot-identifier mia-before-CHANGE-$(date +%Y%m%d%H%M)
```

Restore is a new instance, never in place, so it also means repointing
`MIA_DATABASE_URL` and forcing a new deployment:

```
aws rds restore-db-instance-to-point-in-time   --source-db-instance-identifier mia   --target-db-instance-identifier mia-restored   --restore-time 2026-09-04T12:00:00Z
```

`MultiAZ` stays false deliberately: it roughly doubles the cost and one operator can
tolerate the failover window. That is a cost decision, not an oversight — it does mean
an AZ failure is an outage, not a blip.

## What not to do

Do not copy `.env` onto Fargate. Do not dump secrets. Do not auto-deploy. Do not invent metrics or prices. Assaf sends customer WhatsApp.
