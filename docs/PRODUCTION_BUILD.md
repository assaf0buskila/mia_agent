# Production build — operator sequence

**Date:** 2026-08-22  
**Status:** First AWS live (ADR-014). Live host `https://mia.assafweb.com` in **eu-north-1** (ADR-019; Assaf ADOPT 2026-08-22). Not a grant of gated writes. Not AgentCore. Not Lambda for LangGraph.  
**Related:** `.env.example`, ADR-015 in `docs/DECISIONS.md`, `docs/RUNBOOK.md`, `docs/ARCHITECTURE.md`, `deploy/`

Package manager is **uv**. Python `>=3.12`. PowerShell: `;` not `&&`. Local laptop uses `.env`. Production keys live in the **Secrets Manager box**, not in a file on the host.

Cloudflare Tunnel is **test only**. Do not deploy Mia to Vercel, Cloudflare Workers, or Lambda.

---

## What “production” means today

| Layer | Production value |
| --- | --- |
| Process | ECS Fargate container: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips * --timeout-keep-alive 130` (no `--reload`; ALB TLS terminate — SG must only admit the ALB; keep-alive **above** ALB idle 120s) |
| Image | `deploy/Dockerfile` → ECR |
| Database | RDS PostgreSQL (`MIA_DATABASE_URL` from the box) |
| Public URL | `https://mia.assafweb.com` via ALB + ACM |
| Keys | Secrets Manager secret `mia/prod` (the box). ECS injects `MIA_*`. Never git, never chat |
| Env | `MIA_ENV=prod` and `MIA_DEMO_MODE=false` — never together as prod+demo |
| Prospect send | `MIA_AUTOMATION_MODE=auto_approved` (ADR-022). Unknown WhatsApp still silent. Instagram send stays off. |
| Calendar writes | `MIA_CALENDAR_WRITE=true` (Assaf ADOPT 2026-08-22). Exact create/move proposals execute only after Assaf's one-tap Telegram approval. |
| Gmail / Meta send | Gmail draft send is wired but stays approval-only and additionally requires `MIA_GMAIL_SEND=true`; Meta writes remain off. R4/R5 are not env knobs. Follow-up persist/due-scan is alive; no auto-send flag. |
| Kill switch | `MIA_KILL_SWITCH=false` for live; `true` + restart denies high-risk writes and does not 503 owner talk or site chat |
| Lambda / AgentCore / SQS / WAF | Specified later. Not first live |

First live is this FastAPI process on Fargate + RDS with writes gated. Cursor Composio plugin Calendar Active does not prove Mia Calendar — `/health` `composio` must be true and `MIA_COMPOSIO_USER_ID` must be the Composio debug `@user_id`.

First-boot tables: `uv run mia-migrate` (or the `mia-migrate` Fargate run-task). The worker runs `init_db()` then `migrations/*.sql`. Prod API lifespan skips `create_all` so ALB `/health/live` is process-up. Dev/test still `create_all` on boot. Existing DBs must run migrate for SQL column files.

---

## 0. Hygiene (do this first)

1. Laptop `.env` is for local uvicorn only. Production keys go in AWS Secrets Manager `mia/prod` (ADR-014). Do not copy `.env` onto Fargate. `.env.example` lists KEEP vs EDIT/SECRET names **and** the ADR-015 adapter sections. `deploy/mia-prod.secret.example.json` keys must match ECS `secrets` names (empty string is allowed). Mia reads `MIA_` only. `COMPOSIO_API_KEY` without the prefix is ignored.
2. Never commit `.env`. Never paste tokens in chat.
3. This Windows laptop needs **AWS CLI v2** before §3 ([install](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)). Docker Desktop is already present for the image push. Prefer the **current-user** MSI — it does not need Administrator / UAC. `winget` `Amazon.AWSCLI` is all-users and stalls on UAC. After install, **open a new PowerShell** so `aws` is on PATH. Sign in yourself — never paste access keys in chat. Console user: [`aws login`](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html). IAM Identity Center: `aws configure sso` then `aws sso login`.

```
msiexec.exe /i https://awscli.amazonaws.com/AWSCLIV2-User.msi /qn
# new PowerShell:
aws --version
$env:AWS_DEFAULT_REGION = "eu-north-1"
aws login
aws sts get-caller-identity --query Account --output text
```

Expect `aws-cli/2.` then a 12-digit account id. `NoCredentials` means login is not done yet. All-users fallback (needs UAC Yes): `winget install -e --id Amazon.AWSCLI --accept-package-agreements --accept-source-agreements`. Then:

```
uv sync --group dev
uv run ruff check app tests
uv run pytest
```

---

## 1. Local API (loopback)

```
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Expect `GET http://127.0.0.1:8000/health` → `"status": "ok"`. Also `GET /health/live` and `GET /health/ready` → `{"status":"ok"}`. Leave `MIA_PUBLIC_BASE_URL=http://127.0.0.1:8000` for this step.

If `.env` has `MIA_ENV=prod`, `GET /docs` is **404** (intentional — Swagger is off in prod). That is not a failed API. Confirm health JSON, not the FastAPI docs UI.

If the Ask Mia widget still looks white-on-white after a hard refresh: check `GET /v1/website/widget.js` headers. The live script must send `Cache-Control: no-cache` and must **not** send `etag`/`last-modified` from an old `FileResponse`. Windows can leave a dead `127.0.0.1:8000` listener while uvicorn is bound to `0.0.0.0:8000` — loopback then serves the old widget. Open the LAN IP of the new process, or reboot to drop the ghost bind.

---

## 2. Webhook **test** only (Cloudflare)

Providers cannot POST to `127.0.0.1`. For a **session test**:

```
cloudflared tunnel --url http://127.0.0.1:8000
GET https://<random>.trycloudflare.com/health
```

Paste that host into Meta / Composio for **this session**. The hostname changes on restart. Do not put trycloudflare in production DNS or widget script.

| Channel | Path |
| --- | --- |
| Telegram | `POST {base}/v1/telegram/webhook` (`X-Telegram-Bot-Api-Secret-Token`) |
| WhatsApp | `POST {base}/v1/whatsapp/webhook` (GET verify same path) |
| Instagram | `POST {base}/v1/instagram/webhook` (not a v1 sales inbox) |
| Composio Gmail ingest | `POST {base}/v1/composio/webhook` |

HMAC (Meta/Composio) still runs on Mia. The tunnel only forwards HTTPS.

---

## 3. Production host (AWS — ADR-014)

Do this in the AWS account. You fill the box; code never contains keys. Official injection: [ECS Secrets Manager env vars](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html) (Fargate platform **1.4.0+** for JSON keys). Rotating the box does **not** update a running task — Force new deployment.

**Order is required.** `create-service` needs a target group ARN. The HTTPS listener needs an **ISSUED** ACM certificate. Migrate the empty RDS **before** the service takes ALB traffic.

`3.1 VPC/RDS` → `3.2 box` → `3.3 image` → `3.4 IAM + cluster + task definition` → `3.5 ACM ISSUED + ALB + target group` → `3.6 migrate, then create-service, then CloudWatch ALB alarms, then /health` → EventBridge only after health is green.

Every `--cli-input-json` below is **`file://./deploy/local/<name>.json`** after `fill-placeholders.ps1`, run from this repo root. Do not use `file://deploy/local/...` — AWS CLI treats `deploy` as a host ([load parameters from a file](https://docs.aws.amazon.com/cli/latest/userguide/cli-usage-parameters-file.html); `./` is supported). Do not authorize or `create-db-instance` against `*.example.json` — those still contain `sg-MIA_*` tokens. Templates stay in `deploy/*.example.json`. Re-run the script after ALB/target-group hashes, ACM cert id, and Route 53 ids. The script does **not** call AWS and does **not** read `.env`.

This laptop is PowerShell. The repo path contains a space — `Set-Location` to the repo root first. Pin the region so ACM and the ALB are not created in different regions. **Do not open the VPC wizard until the identity gate exits 0.**

```
Set-Location "<this-repo-root>"
$env:AWS_DEFAULT_REGION = "eu-north-1"
aws login
powershell -File deploy/assert-aws-identity.ps1
```

Expect a 12-digit account id. Use that as `-AccountId`. Exit 1 (`NoCredentials`) → do not create VPC/NAT. All later `aws` commands in this section inherit the region.

### 3.1 VPC / RDS

1. Open the Amazon VPC console in **eu-north-1** ([create a VPC](https://docs.aws.amazon.com/vpc/latest/userguide/create-vpc.html)): https://eu-north-1.console.aws.amazon.com/vpc/home?region=eu-north-1 — **Create VPC** → **VPC and more** ([VPC with private subnets and NAT](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-example-private-subnets-nat.html)). 2 Availability Zones, **2 public** subnets (ALB), **2 private** subnets (ECS + RDS). NAT gateways: **In 1 AZ** for first live (private tasks must reach OpenAI/Composio/Meta). Do not choose NAT **None**. Keep **DNS hostnames** and **DNS resolution** enabled (RDS endpoint). Do not put the ALB only in private subnets. Copy the vpc-id and four subnet ids.
2. Create three security groups in that VPC. Print **GroupId** only. Ingress only after the stamp step — leave default egress (tasks need UDP 53 for VPC DNS). Do **not** open 8000 or 5432 to `0.0.0.0/0`.

```
aws ec2 create-security-group --group-name mia-alb --description "Mia ALB" --vpc-id vpc-YOUR_VPC --query GroupId --output text
aws ec2 create-security-group --group-name mia-tasks --description "Mia ECS tasks" --vpc-id vpc-YOUR_VPC --query GroupId --output text
aws ec2 create-security-group --group-name mia-rds --description "Mia RDS" --vpc-id vpc-YOUR_VPC --query GroupId --output text
```

3. Stamp ids (never the secret JSON, never `.env`):

```
powershell -File deploy/fill-placeholders.ps1 -AccountId 123456789012 -Region eu-north-1 -VpcId vpc-... -SubnetPublicA subnet-... -SubnetPublicB subnet-... -SubnetPrivateA subnet-... -SubnetPrivateB subnet-... -SgAlb sg-... -SgTasks sg-... -SgRds sg-...
powershell -File deploy/assert-local-stamped.ps1 -Stage network
```

Exit 1 means a token is still in `deploy/local/` — do not authorize. Re-run fill with the missing ids. The assert script does **not** call AWS and does **not** read `.env`.

4. Authorize from the stamped copies. `mia-alb`: inbound 80 and 443 from `0.0.0.0/0`. `mia-tasks`: inbound 8000 from the ALB SG only. `mia-rds`: inbound 5432 from the tasks SG only.

```
aws ec2 authorize-security-group-ingress --cli-input-json file://./deploy/local/sg-alb-ingress.json
aws ec2 authorize-security-group-ingress --cli-input-json file://./deploy/local/sg-tasks-ingress.json
aws ec2 authorize-security-group-ingress --cli-input-json file://./deploy/local/sg-rds-ingress.json
```

5. RDS subnet group + PostgreSQL **16** in the **private** subnets (`EngineVersion` `16`; RDS picks a current 16.x minor, `AutoMinorVersionUpgrade` on). RDS generates the master password in Secrets Manager (`ManageMasterUserPassword`). Do **not** put that password in git or chat. Public access **off**.

```
aws rds create-db-subnet-group --cli-input-json file://./deploy/local/rds-subnet-group.json
aws rds create-db-instance --cli-input-json file://./deploy/local/rds.json
aws rds wait db-instance-available --db-instance-identifier mia
aws rds describe-db-instances --db-instance-identifier mia --query "DBInstances[0].Endpoint.Address" --output text
aws rds describe-db-instances --db-instance-identifier mia --query "DBInstances[0].MasterUserSecret.SecretArn" --output text
```

Read the managed secret **on this machine only** ([RDS + Secrets Manager](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/rds-secrets-manager.html)). Never paste `SecretString` in chat:

```
aws secretsmanager get-secret-value --secret-id SECRET_ARN --query SecretString --output text
```

Username is `mia_admin`. URL-encode `@` `:` `/` `?` in the password. Put the DSN into `mia/prod` in §3.2. Parameter `rds.force_ssl=1` if the console offers it.

### 3.2 The key box

1. Copy `deploy/mia-prod.secret.example.json` to a **local** file named `mia-prod.secret.json` (gitignored). Fill every value. Unused keys stay `""`.
2. Production `MIA_DATABASE_URL` (the CA is in the image):

```
postgres://USER:PASSWORD@HOST:5432/mia?sslmode=verify-full&sslrootcert=/etc/ssl/certs/rds-global-bundle.pem
```

3. Secrets Manager → Store a new secret → Other type of secret → plaintext JSON from that file. Name: **`mia/prod`**. KMS default or a CMK you control.
4. Delete the filled local JSON when the console shows the secret created. Never paste values in chat. Never commit the filled file.
5. If you use a CMK, add `kms:Decrypt` on that key to the task **execution** role (not the task role).

### 3.3 Image

GitHub Actions `ci.yml` job `image` already `docker build`s this Dockerfile on every push (no ECR login, no push). Local PowerShell (`;` not `&&`). Replace `REGION` and `ACCOUNT_ID`.

```
aws ecr create-repository --repository-name mia --region REGION
aws ecr get-login-password --region REGION | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com
docker build -f deploy/Dockerfile -t mia:latest .
docker tag mia:latest ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/mia:latest
docker push ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/mia:latest
```

### 3.4 IAM + task definition

`deploy/local/` already has ACCOUNT_ID / REGION from the stamp. Every `secrets.valueFrom` JSON key **must exist** in `mia/prod` (use `""` for unused).

```
aws iam create-role --role-name miaTaskExecutionRole --assume-role-policy-document file://./deploy/local/iam-ecs-task-trust.json
aws iam attach-role-policy --role-name miaTaskExecutionRole --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam put-role-policy --role-name miaTaskExecutionRole --policy-name ReadMiaProdBoxOnly --policy-document file://./deploy/local/iam-task-execution-secrets.json
aws iam create-role --role-name miaTaskRole --assume-role-policy-document file://./deploy/local/iam-ecs-task-trust.json
```

Task **execution** role reads the box (`mia/prod*`). Task role stays empty this slice — adapters call OpenAI/Composio/Meta over HTTPS. If you used a CMK on the secret, add `kms:Decrypt` on that key to the **execution** role only.

CloudWatch log group **before** the first task starts:

```
aws logs create-log-group --log-group-name /ecs/mia
aws logs put-retention-policy --log-group-name /ecs/mia --retention-in-days 30
```

Cluster + register the task definition. **Do not** `create-service` yet — the load balancer target group does not exist.

```
aws ecs create-cluster --cluster-name mia
aws ecs register-task-definition --cli-input-json file://./deploy/local/ecs-task-definition.json
```

### 3.5 HTTPS + DNS

Request ACM in the **same region** as the ALB. DNS validation. Copy `CertificateArn` from `request-certificate`, add the validation CNAME in DNS, **then** wait — `create-listener` rejects a pending cert.

```
aws acm request-certificate --cli-input-json file://./deploy/local/acm-certificate.json
aws acm wait certificate-validated --certificate-arn CERT_ARN
```

Create the load balancer and target group **before** stamping hashes. `deploy/local/alb.json` and `alb-target-group.json` only need VPC/subnet/SG from the first stamp.

```
aws elbv2 create-load-balancer --cli-input-json file://./deploy/local/alb.json
aws elbv2 wait load-balancer-available --load-balancer-arns LOAD_BALANCER_ARN
aws elbv2 create-target-group --cli-input-json file://./deploy/local/alb-target-group.json
```

Copy `LOAD_BALANCER_ARN` from `create-load-balancer`. From `describe-load-balancers` / `describe-target-groups` copy the ALB id hash (`app/mia/HASH`), target-group hash, `DNSName`, and ALB `CanonicalHostedZoneId`. Re-run `deploy/fill-placeholders.ps1` with those plus `-CertId` and `-Route53ZoneId`. Do not edit `*.example.json`. Then:

```
powershell -File deploy/assert-local-stamped.ps1 -Stage alb
```

Exit 1 means HASH or DNS tokens remain — do not create listeners. Attributes, listeners, Route 53, ECS service, and CloudWatch alarms need that second stamp.

```
aws elbv2 modify-load-balancer-attributes --cli-input-json file://./deploy/local/alb-attributes.json
aws elbv2 modify-target-group-attributes --cli-input-json file://./deploy/local/alb-target-group-attributes.json
aws elbv2 create-listener --cli-input-json file://./deploy/local/alb-listener-https.json
aws elbv2 create-listener --cli-input-json file://./deploy/local/alb-listener-http-redirect.json
```

Target group is **IP** (Fargate `awsvpc`), HTTP:8000, health `/health/live` (not `/`). Deregistration delay **30s**. Idle timeout **120s** (default 60s 504s a sales compose). Uvicorn `--timeout-keep-alive 130` stays **above** that idle timeout so the ALB does not reuse a closed keep-alive ([ALB 502](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-troubleshooting.html#http-502-issues)). HTTPS:443 forwards; HTTP:80 redirects `HTTP_301` to HTTPS. TLS policy is `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09` ([ALB SSL policies](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/describe-ssl-policies.html)) — do not omit `SslPolicy` on CLI or AWS falls back to `ELBSecurityPolicy-2016-08`. Deploy readiness is `/health/ready` (service), not the ALB ping.

`AliasTarget.HostedZoneId` is the **ALB** canonical zone from `describe-load-balancers`, not the Route 53 hosted zone. Copy the Route 53 zone id (AssafWeb DNS):

```
aws route53 list-hosted-zones-by-name --dns-name assafweb.com --query "HostedZones[0].Id" --output text
```

Then:

```
aws route53 change-resource-record-sets --cli-input-json file://./deploy/local/route53-mia.json
```

Landing-page Vercel `NEXT_PUBLIC_MIA_BASE_URL=https://mia.assafweb.com` and redeploy.

### 3.6 First boot

1. One-off migrate **before** the service (same task definition, same private subnets/SG as `deploy/local/ecs-service.json`). `mia-migrate` only — never send. Paste the stamped subnet and SG ids into the network string:

```
aws ecs run-task --cluster mia --launch-type FARGATE --platform-version 1.4.0 --task-definition mia --overrides file://./deploy/local/ecs-migrate-overrides.json --network-configuration "awsvpcConfiguration={subnets=[subnet-...,subnet-...],securityGroups=[sg-...],assignPublicIp=DISABLED}"
aws ecs wait tasks-stopped --cluster mia --tasks TASK_ARN
aws ecs describe-tasks --cluster mia --tasks TASK_ARN --query "tasks[0].containers[0].exitCode" --output text
```

Copy `TASK_ARN` from `run-task` (`tasks[0].taskArn`). `wait tasks-stopped` is not success — the printed **exit code must be `0`**. Any other value (or `None`) is a failed migrate; read CloudWatch `/ecs/mia` and do **not** `create-service`. Prod API **does not** `create_all` on boot — `/health/live` must bind without waiting on schema. Existing RDS still needs this migrate once.

2. Create the service (target group ARN already pasted). Platform **1.4.0**. `assignPublicIp` stays **DISABLED**. Desired count **1**. Execute-command **off**. Health-check grace **120s** (ALB interval 30s × unhealthy 3, plus import). Deployment circuit breaker **enable + rollback** ([ECS rolling update](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-type-ecs.html)). First `create-service` has nothing to roll back to — a failed first boot still fails; later image pushes roll back.

```
aws ecs create-service --cli-input-json file://./deploy/local/ecs-service.json
aws ecs wait services-stable --cluster mia --services mia
aws cloudwatch put-metric-alarm --cli-input-json file://./deploy/local/cloudwatch-alb-unhealthy.json
aws cloudwatch put-metric-alarm --cli-input-json file://./deploy/local/cloudwatch-alb-5xx.json
```

`UnHealthyHostCount` uses statistic **Minimum** for two datapoints ([ALB CloudWatch metrics](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-cloudwatch-metrics.html)). The 5xx alarm pages SNS when `MIA_ALB_5XX_SNS_TOPIC_ARN` is stamped into `AlarmActions` (`deploy/cloudwatch-alb-5xx.example.json`). Empty ARN is fail-closed: do not put-metric-alarm with a blank action. Stamp `app/mia/HASH` and `targetgroup/mia/HASH` from `describe-load-balancers` / `describe-target-groups` (not the full ARN).

3. `GET https://mia.assafweb.com/health` → 200, `"env": "prod"`, `"demo": false`, `"postgres": true`, `"public_https": true`. `/docs` is 404.
4. If `/health/ready` is 503, migrate did not finish — do not point Meta webhooks yet.
5. EventBridge Scheduler for persist-only jobs — **not** send. Same private network as migrate. After `/health` is green (`deploy/local/` already has ACCOUNT_ID):

```
aws iam create-role --role-name miaSchedulerRole --assume-role-policy-document file://./deploy/local/iam-scheduler-trust.json
aws iam put-role-policy --role-name miaSchedulerRole --policy-name RunMiaPersistJobs --policy-document file://./deploy/local/iam-scheduler-ecs.json
aws scheduler create-schedule --cli-input-json file://./deploy/local/eventbridge-due-scan.json
aws scheduler create-schedule --cli-input-json file://./deploy/local/eventbridge-reconcile.json
```

Replace REGION / ACCOUNT_ID / subnets / SG first. Cadence is baked in: due-scan `rate(15 minutes)`, reconcile `rate(1 hour)`. Target `Input` matches `deploy/local/ecs-due-scan-overrides.json` and `deploy/local/ecs-reconcile-overrides.json`. Execute-command stays off. Until those schedules exist, do not pretend cron is running.

**Lambda:** not first live. Do not put LangGraph on Lambda. Do not use a Lambda as the key box — Secrets Manager is the box. After first live is healthy, webhook ingress can move to Lambda (signature + fast ACK + SQS FIFO) with the graph still on Fargate.

**Not production:** Vercel, Cloudflare Workers, trycloudflare, copying `.env` onto a VPS. Do not add Caddy or systemd unit files — ALB terminates TLS.

Schedule (cron or equivalent; persist-only):

```
uv run mia-migrate
uv run mia-due-scan
uv run mia-reconcile
uv run mia-reconcile --inspect
```

Never send from these CLIs.

---

## 4. Identity (required before any live channel)

- [ ] `MIA_WHATSAPP_OWNER_PHONES` = Assaf’s WhatsApp ids as Meta sends them (digits, no `+`). Empty = nobody is owner.
- [ ] `MIA_WHATSAPP_VERIFY_TOKEN` + `MIA_WHATSAPP_APP_SECRET` for inbound HMAC (ADR-016: Meta stays the inbound transport).
- [ ] `MIA_WHATSAPP_PHONE_NUMBER_ID` is required for send (Graph or Composio). Graph `MIA_WHATSAPP_ACCESS_TOKEN` stays for inbound STT media even when send is Composio. Production ECS `MIA_WHATSAPP_SENDER=composio` (Assaf ADOPT 2026-08-22) plus Composio key+user+Active WhatsApp. Never both senders. Do not subscribe a Composio WhatsApp inbound trigger — none exists.
- [ ] **One** Instagram sender. Allowlist: `direct` (Graph send, default) or `composio`. Inbound stays Meta webhook. Not a v1 sales inbox. Flip production to `composio` only after staging send is tested. Never dual-send Graph + Composio.
- [ ] Composio: `MIA_COMPOSIO_API_KEY` + `MIA_COMPOSIO_USER_ID` + `MIA_COMPOSIO_WEBHOOK_SECRET` (prefix required). Restart, then `/health` `composio` and `composio_webhook` true. User id must match Composio debug `@user_id` (Cursor plugin OAuth is a different user unless they match).
- [ ] Composio Project Settings **OAuth user verification URL is empty**. Do not leave `https://your-app.com/composio/verify` — Mia has no verify route; that field blocks dashboard connections. White-labeling is a different page (logo + own Google OAuth client per toolkit).

---

## 5. Register production webhooks

Use `{MIA_PUBLIC_BASE_URL}` from step 3 — not the tunnel.

- [ ] Telegram owner bot `setWebhook` → `POST {base}/v1/telegram/webhook` with `secret_token` matching `MIA_TELEGRAM_WEBHOOK_SECRET`; numeric `MIA_TELEGRAM_OWNER_USER_IDS` only
- [ ] Meta WhatsApp webhook + verify
- [ ] Meta Instagram webhook + verify (insights/analytics; not a v1 sales inbox)
- [ ] Composio trigger `GMAIL_NEW_GMAIL_MESSAGE` → `POST {base}/v1/composio/webhook`
- [ ] Confirm no Meta icebreaker overlapping Mia

---

## 6. Website widget (Assaf on assafweb.com)

```html
<script
  src="{MIA_PUBLIC_BASE_URL}/v1/website/widget.js"
  data-mia-api="{MIA_PUBLIC_BASE_URL}"
  defer
></script>
```

`data-mia-api` is required when the host loads the script via Next.js `<Script>` (or any loader where `document.currentScript` is null). AssafWeb `AskMiaWidget` must pass the same HTTPS origin as `NEXT_PUBLIC_MIA_BASE_URL`.

`GET /v1/website/widget.js` is `Cache-Control: no-cache` so contrast/script fixes reach the browser without a stuck cache. Hard-refresh still works.

Local look (not assafweb.com): `GET http://192.168.1.193:8000/v1/website/preview` after the API is bound to `0.0.0.0`. Do not use `127.0.0.1` while a leftover loopback listener exists. Opening `widget.js` in the browser shows source — that is the script, not the chat.

Host funnel attrs: `data-mia-section`, `data-mia-cta`, `form[data-mia-form]`. CORS must allow `https://www.assafweb.com` and `https://assafweb.com`.

---

## 7. Staging writes (Assaf-gated)

Do these on a throwaway calendar / staging thread. Adapter map: `docs/ARCHITECTURE.md` and ADR-015 in `docs/DECISIONS.md`.

- [ ] Calendar write OAuth on the Composio Google account, then `MIA_CALENDAR_WRITE=true` **only for staging book/PATCH**. Reads work without this flag.
- [ ] One WhatsApp or Instagram staging conversation if you switch `MIA_AUTOMATION_MODE=auto_approved` (prospect DMs). Default production stays `shadow`.
- [ ] Provider calendar **delete** stays denied (R5). Cancel locally; Assaf deletes in Google Calendar.
- [ ] Leave **unchecked**: follow-up send, Gmail send, LinkedIn post, Meta campaign write, instruction activation, HYBRID, Lambda/SQS/AgentCore.

---

## 8. Day-2 operations

Full steps: `docs/RUNBOOK.md`.

| Need | Action |
| --- | --- |
| Emergency stop | `MIA_KILL_SWITCH=true` + restart; `/health` `"killed"` (`/health/live` stays `ok` if the process is up) |
| Stop prospect DMs only | keep `MIA_AUTOMATION_MODE=shadow` + restart |
| Stop calendar provider writes | `MIA_CALENDAR_WRITE=false` + restart |
| Human takeover | Owner WhatsApp phrase **and** `lead_<12 hex>` |
| Stale webhooks | `uv run mia-reconcile --inspect` (no replay) |

---

## 9. Still not production-complete (honest)

- AWS ingress / SQS / AgentCore / WAF (Secrets Manager box is ADR-014; not the later split)
- Dashboards (Gate F). ALB 5xx alarm **can page** via SNS topic ARN `MIA_ALB_5XX_SNS_TOPIC_ARN` (placeholder in `.env.example`; stamp `deploy/cloudwatch-alb-5xx.example.json`). Unhealthy-host alarm stays console-only. This PR does not create the topic or attach it live.
- Dead-letter replay
- Frozen STT benchmark audio
- Versioned knowledge RAG
- `cost_usd` (stays 0 until a price table exists)
- HYBRID automation mode

Those are new work. Do not skip to them to look “more production.”
