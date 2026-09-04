# Live-proof runbook: Telegram voice and owner integrations

Status: prepared from source only on 2026-08-28. This is not evidence that any
production setting, AWS resource, provider connection, or webhook is live. The operator
must run the steps in order in the intended AWS account and record only the stated
non-secret evidence.

## Preflight evidence — 2026-08-28

Result: **blocked before AWS inspection**.

1. `powershell -File deploy/assert-aws-identity.ps1` was blocked by this Windows host's
   PowerShell execution policy before it could call AWS.
2. The same checked-in, read-only gate was retried with a process-scoped execution-policy
   bypass. It reported **No AWS credentials** for `eu-north-1`.

Classification: AWS authentication is unavailable in this shell. No account identifier is
recorded. Per the preflight stop condition, no ECS service/task-definition/rollout request
was made. The parent verifier separately attempted public `/health/live`, `/health/ready`,
and `/health` requests after this AWS stop: DNS resolved `mia.assafweb.com` to the eu-north-1
load balancer, but HTTP and HTTPS both failed to connect (`curl` exit 7), so no status or
health body was observed. Therefore current task revision, desired/running counts, rollout
state, health booleans, and missing-setting names remain unobserved. The next authorized
operator action is `aws login` (or the configured SSO login), followed by the identity gate
and ECS/ALB inspection; do not supply credentials in chat.

### Preflight refresh — 2026-08-28 (second continuation)

- The checked-in AWS identity gate, run with process-scoped PowerShell bypass, again
  reported no AWS credentials. No account identifier was recorded and no ECS request was
  made; task revision, desired/running counts, and rollout remain unobserved.
- Bounded 10-second HTTPS checks to `/health/live`, `/health/ready`, and `/health` each
  ended in `HttpRequestException`. No HTTP status or body was received, so no health
  booleans or missing-setting names were observed.

Hard stops remain AWS authentication and public-service connectivity. No deployment,
mutation, secret/env inspection, log inspection, or provider API call was performed.

### Preflight refresh — 2026-08-28 (third continuation)

- A fresh HEAVY verifier and the parent independently repeated bounded public HTTPS
  checks outside the restricted network sandbox. `/health/live`, `/health/ready`, and
  `/health` now return HTTP 200 with status `ok`.
- The sanitized health contract reports production mode, Postgres/public HTTPS/Telegram
  owner enabled, voice input ready, and Composio/Sheets mirror/LinkedIn profile/GSC/GA4
  configured; both reported missing-name lists are empty. This is configuration and
  reachability evidence only, not a real Telegram or provider capability proof.
- The checked-in AWS identity gate still exits before inspection because this shell has
  no AWS credentials. An interactive `aws login` was opened and remains pending browser
  authorization. No account identifier was recorded and no ECS request was made.

Current classification: public-service connectivity has recovered. AWS authentication
remains the deployment-inspection blocker; ECS revision/rollout state and all real
Telegram/provider executions remain unproven. No deployment, mutation, secret/env/log
inspection, or provider API call was performed.

### AWS authorization and ECS inspection — 2026-08-28 (resumed by Assaf)

- Assaf authorized a fresh `aws login`. Identity verification succeeded; the account
  identifier is intentionally not recorded here.
- Read-only ECS inspection in `eu-north-1` found service `mia` ACTIVE on task definition
  `mia:28`, desired/running `1/1`, pending `0`, and primary rollout `COMPLETED`.
- Deployment circuit breaker and automatic rollback are enabled. `mia:28` is the captured
  rollback target for the next authorized release.
- The deployed image tag is `mia:ae432f6`, matching the repository's current HEAD commit,
  while the requested voice/integration/cleanup work exists as intentional uncommitted
  worktree changes. Therefore the current release candidate is not yet deployed.
- Public health remained independently proven 200/ok before this inspection. No task
  registration, migration, service update, secret/env/log read, or provider call occurred.

Next safe action: finish the exhaustive cleanup audit, implement only accepted changes,
repeat the full local release gate, then build/push one immutable release image and follow
the migration/rollout/health sequence with `mia:28` retained for rollback.

## Scope and hard boundaries

- Region is `eu-north-1`; service and cluster names in the current scripts are both `mia`.
- Telegram identity remains `MIA_TELEGRAM_OWNER_USER_IDS` numeric-only. Do not test
  username-based access and do not replace the allowlist with a username.
- Voice is input-only: Telegram voice note -> download -> STT -> OwnerGraph -> text.
  Do not configure or test TTS.
- Google Sheets remains an explicitly authorized, allowlisted operational surface. It is
  never a source of truth and must not be used for Drive discovery, creation, deletion,
  clearing, formatting, or formulas. A read of the same bounded QA range is permitted
  only to verify this runbook's write result; it is never fed back into Mia state.
- GSC and GA4 are API-backed owner reads. LinkedIn is own-profile read only. Do not use
  a live proof to post, comment, message, or alter any provider data.

## 1. Local release gate (no live call)

Run from the repository root before a release candidate is built:

```powershell
uv --cache-dir .uv-cache run pytest -q tests/unit/test_telegram.py -k voice -p no:cacheprovider
uv --cache-dir .uv-cache run pytest -q tests/unit/test_transcribe.py -p no:cacheprovider
uv --cache-dir .uv-cache run pytest -q tests/unit/test_sheets.py tests/unit/test_ga4.py tests/unit/test_search_console.py tests/unit/test_linkedin.py -p no:cacheprovider
uv --cache-dir .uv-cache run ruff check app tests
python scripts/assert_origin_bind.py
```

Expected evidence: every command exits `0`; the first two commands report the Telegram
voice and model-family tests passed; the Google integration tests pass; the final command
reports the origin-bind gate is present. Stop here on any failure. Do not build, register,
or deploy a failed SHA.

## 2. AWS and release-candidate preflight (operator-authorized, no secret values)

These commands only establish identity or inspect deployment state. They require the
operator's AWS login and must not be run by an unauthorised automation.

```powershell
powershell -File deploy/assert-aws-identity.ps1
aws ecs describe-services --cluster mia --services mia --region eu-north-1 --query "services[0].{taskDefinition:taskDefinition,desiredCount:desiredCount,runningCount:runningCount,rolloutState:deployments[0].rolloutState}" --output json
aws ecs describe-task-definition --task-definition mia --region eu-north-1 --query "taskDefinition.{family:family,revision:revision,taskRoleArn:taskRoleArn,executionRoleArn:executionRoleArn}" --output json
```

Expected evidence: the identity script prints only a 12-digit account id; the service has
the expected cluster/service names; retain the currently deployed task-definition ARN as
the rollback target. Do not print environment or Secrets Manager values.

Before registering a new image revision, the operator builds/pushes the already approved
image and then runs this state-changing command with an immutable image tag:

```powershell
uv --cache-dir .uv-cache run python scripts/deploy_ecs_revision.py --tag <approved-image-tag>
```

Expected evidence: `assert_origin_bind.py` passes, the script prints the old revision and
the newly registered `mia:<revision>`. It does **not** update the service. If it fails,
stop; do not hand-edit the task definition or retry with unreviewed code.

## 3. Schema and ECS rollout decision

Run migration before routing the service to a revision that needs schema changes:

```powershell
uv --cache-dir .uv-cache run python scripts/run_ecs_migration.py --task-definition mia:<new-revision>
```

Expected evidence: the Fargate task reaches `STOPPED` with `exitCode 0` and prints
`task mia-migrate completed`. Any other exit code, missing exit code, or timeout is a
hard stop: inspect sanitized CloudWatch operational logs, fix or roll back, and do not
update the ECS service.

Only after the migration succeeds, the authorized operator may make the service-changing
cut to the registered revision:

```powershell
aws ecs update-service --cluster mia --service mia --task-definition mia:<new-revision> --force-new-deployment --region eu-north-1
```

Expected evidence: the response names `mia:<new-revision>` as the service task definition.
Do not run it after a migration failure or with an unrecorded image/task revision. Then wait
for rollout completion:

```powershell
aws ecs wait services-stable --cluster mia --services mia --region eu-north-1
aws ecs describe-services --cluster mia --services mia --region eu-north-1 --query "services[0].deployments[*].{taskDefinition:taskDefinition,status:status,rolloutState:rolloutState,runningCount:runningCount,desiredCount:desiredCount}" --output json
```

Expected evidence: wait exits `0`; the new revision is primary, has running equal to
desired count, and reports completed. Deployment circuit-breaker rollback is enabled in
the service template, but the operator must still retain and verify the prior revision.

## 4. Health contract and configuration status (read-only HTTP)

Only after the ECS service is stable, run:

```powershell
curl.exe --fail-with-body https://mia.assafweb.com/health/live
curl.exe --fail-with-body https://mia.assafweb.com/health/ready
curl.exe --fail-with-body https://mia.assafweb.com/health
```

Expected evidence, without copying sensitive fields:

- `/health/live` returns `{"status":"ok"}`.
- `/health/ready` returns `{"status":"ok"}`. A `503` means schema/readiness is not
  proven; stop before provider tests.
- `/health` has `env: "prod"`, `demo: false`, `kill_switch: false`, `postgres: true`,
  `public_https: true`, and `telegram_owner: true`.
- `/health` field `brain.voice_in` has `ready: true` and an empty `missing` list.
- `/health` field `owner_integrations` has `composio: true`, `sheets_mirror: true`,
  `linkedin_profile: true`, `search_console: true`, and `ga4: true`. Its `missing`
  list must not include the required integration settings below.

Record booleans and missing-setting **names** only. `/health` is configuration readiness,
not proof that a Composio connected account, OAuth scope, Telegram webhook, or provider API
call works. In particular, `owner_integrations.sheets_mirror` is calculated from Composio
configuration plus the primary mirror ID only. It neither evaluates
`MIA_SHEETS_ALLOWED_SPREADSHEET_IDS` nor proves an authenticated owner `sheets.read`,
`sheets.update`, or `sheets.append` call.

Required setting names to verify by presence/status only:

- Telegram voice: `MIA_TELEGRAM_BOT_TOKEN`, `MIA_TELEGRAM_WEBHOOK_SECRET`,
  `MIA_TELEGRAM_OWNER_USER_IDS`, `MIA_OPENAI_API_KEY`, `MIA_OPENAI_TRANSCRIBE_MODEL`,
  optional `MIA_OPENAI_TRANSCRIBE_FALLBACK_MODEL`.
- Google adapters: `MIA_COMPOSIO_API_KEY`, `MIA_COMPOSIO_USER_ID`,
  `MIA_SHEETS_SPREADSHEET_ID`, `MIA_SHEETS_ALLOWED_SPREADSHEET_IDS`,
  `MIA_GSC_SITE_URL`, `MIA_GA4_PROPERTY_ID`. The effective Sheets owner allowlist is
  the union of the primary mirror ID and the comma-separated allowed-ID setting.
  `MIA_COMPOSIO_DISCOVERY=true` is an alternative to the GSC/GA4 resource-id settings,
  not proof that discovery succeeds.

Never retrieve, paste, log, or print a value from `mia/prod`.

## 5. Telegram webhook and voice proof (authorized owner only)

1. In BotFather/Telegram webhook administration, register
   `https://mia.assafweb.com/v1/telegram/webhook` with a `secret_token` that matches
   `MIA_TELEGRAM_WEBHOOK_SECRET`. Do not place the token in this runbook, shell history,
   a URL, or a chat.
2. Confirm the registered webhook is HTTPS and that Telegram's webhook status shows no
   delivery error. Record endpoint/status only, never the secret token.
3. From an account whose numeric id is already in `MIA_TELEGRAM_OWNER_USER_IDS`, send one
   benign voice note such as “בדיקת קול”.
4. Expected evidence: exactly one **text** reply arrives, it is a normal OwnerGraph reply
   (not TTS), and it does not render arbitrary HTML from the transcript. In sanitized ECS
   operational logs, record only correlation/event identifiers and the outcome class,
   never audio bytes, transcript text, headers, or credentials.
5. Send the same note/update only if Telegram itself retries it. Expected evidence: no
   duplicate owner-facing reply for the same update id.
6. From a non-owner account, send a benign text/voice note. Expected evidence: no owner
   graph action and no reply. This verifies the numeric allowlist remains enforced.

If the owner voice note yields the fixed transcription-failure text, record an adapter
failure, preserve the safe message, and stop. Do not expose provider error bodies or
attempt a second “voice brain.”

## 6. Read-only Google/LinkedIn live proofs (one capability at a time)

Use the authenticated owner Telegram account after health is green. Each request should be
one narrow, benign read and each expected answer must contain data that is plausible for the
connected account; empty/disabled/error is a failed proof, not a reason to change policy.

| Capability | Owner request | Expected evidence | Must not do |
| --- | --- | --- | --- |
| Sheets read | Ask for a small explicitly allowed range from the configured spreadsheet. | One bounded read answer; sanitized outcome status `ok`; no more than the supported range/row limits. | Drive discovery, new spreadsheet, clear/delete/format/formula, or treating output as system truth. |
| GA4 traffic | Ask for AssafWeb traffic for a completed bounded date range. | API-backed answer with normalized traffic/users/sessions/conversions/pages where available; sanitized `ok` outcome. | Browser scraping or changing GA4 configuration. |
| GSC metrics | Ask for search clicks/impressions/CTR/position for a completed bounded date range. | API-backed answer with normalized GSC metrics; sanitized `ok` outcome. | URL/property mutation or inventing a site when resource resolution is empty. |
| LinkedIn profile | Ask for Mia's connected own LinkedIn profile. | Profile-only answer (name/headline if available); sanitized `ok` outcome. | Post, comment, DM, upload, or request member/post analytics. |

Between requests, verify `/health` remains `kill_switch: false` and that the corresponding
`owner_integrations` booleans remain true. A `ready` flag alone is not the evidence: the
real capability response and its sanitized outcome are.

### 6.1 Sheets write proof: one bounded, idempotent QA append

This is an authorized operational write, not a production-data exercise. Before sending
the request, Assaf must preauthorize one otherwise-unused QA sheet/range and confirm that
its spreadsheet ID is present in either `MIA_SHEETS_SPREADSHEET_ID` or
`MIA_SHEETS_ALLOWED_SPREADSHEET_IDS`. Record the ID and range in the operator's private
change record; do not put the ID in chat or an application log.

Use an append-only QA range with no business data, for example the privately preauthorized
`<QA_RANGE>`, and choose one literal marker such as `MIA_QA_SHEETS_WRITE_20260828`. The
marker is intentionally retained: clear/delete is forbidden. Do not use a formula, a
timestamp generated by Sheets, an account identifier, or customer/lead data.

From the numeric-allowlisted owner Telegram account, send this exact style of explicit
request, replacing only the private placeholders:

> In the allowlisted Google Sheet `<QA_SPREADSHEET_ID>`, append the exact literal
> `MIA_QA_SHEETS_WRITE_20260828` to `<QA_RANGE>`. This is the authorized QA write.

Expected evidence: the owner reply states one row appended; the adapter uses the bounded
append path with `valueInputOption: RAW`; its sanitized `sheets.append` outcome is `ok`.
Immediately send this explicit read request: “Read only `<QA_RANGE>` in allowlisted Google
Sheet `<QA_SPREADSHEET_ID>` and show that QA value.” Verify exactly one matching literal
row. That read-back proves the external operational result only; it must not be stored or
used as Mia knowledge, memory, lead state, or a decision input.

For the retry/idempotency proof, do **not** send a fresh Telegram message. Cause or wait
for a delivery retry of the same owner update/event only. The write key is the owner event
reference plus canonical operation and arguments, so the existing `sheets.append` claim
must report that the exact write was already handled and must not issue another append.
Read back the same QA range again: exactly one marker row must remain. If a second marker
appears, stop immediately, set the kill switch if the service is still processing writes,
and treat Sheets append idempotency as failed. A manually resent request has a different
owner event and is not an idempotency test; do not use it.

Hard stops: absent preauthorization/allowlist, a range outside the agreed QA range, a
formula-shaped value, an ambiguous owner reply, any non-`ok` sanitized outcome, an
unbounded read-back, or a second marker. Do not compensate by clearing/deleting the
marker; leave it as evidence and use a different preauthorized QA range only after the
failure is understood.

## 7. Stop and rollback decisions

| Condition | Immediate decision | Evidence before proceeding |
| --- | --- | --- |
| Migration failed/not `exitCode 0`, `/health/ready` is `503`, or rollout is unstable | Do not update/continue service rollout. | Migration/task failure reason without secrets; prior task revision retained. |
| New service is unhealthy, returns 5xx, or voice/access control regresses | Set `MIA_KILL_SWITCH=true` in the approved task environment and force a new deployment/restart. | `/health` shows `status: "killed"`; `/health/live` remains `ok`. |
| Need to restore application behavior | Run `aws ecs update-service --cluster mia --service mia --task-definition <captured-prior-task-definition-arn> --force-new-deployment --region eu-north-1`, then wait for stability. | Previous revision is primary and health/ready return 200. |
| Provider capability fails while service is otherwise healthy | Keep provider capability unproven/disabled; do not broaden scopes, bypass policy, or substitute a second agent. | Sanitized failure class and current health booleans. |

After emergency stop, restoring `MIA_KILL_SWITCH=false` requires another deployment/restart
and repeat of the health and affected capability proof. Do not reset production data, delete
provider objects, or alter owner ids as a rollback shortcut.

## What local gates cannot prove

- The current contents/presence of `mia/prod`, ECS task environment injection, IAM/KMS
  access, RDS network reachability, or active task revision.
- ALB/TLS/DNS routing, ECS health check behaviour, migration against production Postgres,
  or CloudWatch logs/alarms.
- Telegram webhook registration, real media download, OpenAI STT credential/model access,
  owner numeric-id match in Telegram, or real HTML rendering in a Telegram client.
- A live Composio connected account, OAuth scopes, tool-version compatibility, configured
  Sheets allowlist/range, GSC property resolution, GA4 property access, or LinkedIn profile
  access.
- Any assertion that deployments, provider reads, or voice notes are currently working.
