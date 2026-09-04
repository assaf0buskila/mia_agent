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
on purpose, and the `image` job builds without pushing. Every deploy is manual.

1. Wait for green CI on a SHA, then check out **that** SHA locally. The image is
   built from the working tree, not from what CI tested, so a dirty tree ships.
2. Build, tag and push to ECR with the next integer tag:
   `docker build -f deploy/Dockerfile -t mia:N .`
3. `python scripts/deploy_ecs_revision.py --tag N` — re-runs the origin-bind gate
   and registers a new task revision with only the image tag swapped. It does
   **not** move the service.
4. `aws ecs update-service --cluster mia --service mia --force-new-deployment`

Step 4 is the one that actually deploys. Record which tag came from which SHA;
nothing in the repo does that for you.

The ECS deployment circuit breaker is on, so a container that fails
`/health/live` rolls back by itself. It catches "will not boot". It catches
nothing about "boots fine and answers badly" — for that, run
`scripts/probe_live_website.py` and read the output yourself.

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

Nothing pages you by default. `deploy/cloudwatch-metric-filters.example.json`
turns the log lines Mia already writes into metrics, and
`deploy/cloudwatch-mia-alarms.example.json` alarms on them. Both need a real SNS
topic — the existing ALB 5xx alarm points at a placeholder, which is why nothing
currently reaches anyone.

`/health` answers "is the config filled in", not "is Mia working". In particular
`ops.failed_sends` counts only an owner turn that broke *and* could not deliver
its own apology; it does not count a failed customer reply.

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
