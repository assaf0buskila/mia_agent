# Live-staging acceptance report

**Date:** 2026-08-22  
**Status:** HTTPS live-staging host accepted in shadow. Not a grant of gated writes.  
**Authority:** Assaf chat → `AGENTS.md` → Bible/`docs/PRD.md` → code  
**Related:** `docs/PRODUCTION_BUILD.md`, `docs/RUNBOOK.md`, `docs/EXTERNAL_SETUP_CHECKLIST.md`

End state claimed: **ECR + ECS Fargate + RDS PostgreSQL 16 + Secrets Manager `mia/prod` + ALB/ACM at `https://mia.assafweb.com`**, shadow posture, health green, one shadow website E2E lead.

This file is evidence from live probes on 2026-08-22. It does not invent provider metrics.

---

## 1. Verdict

| Gate | Result |
| --- | --- |
| `GET https://mia.assafweb.com/health` | **200** — `env=prod`, `demo=false`, `postgres=true`, `public_https=true`, `kill_switch=false` |
| `GET https://mia.assafweb.com/health/live` | **200** `{"status":"ok"}` |
| `GET https://mia.assafweb.com/health/ready` | **200** `{"status":"ok"}` |
| `GET https://mia.assafweb.com/docs` | **404** (prod OpenAPI unmounted) |
| `GET https://mia.assafweb.com/v1/website/widget.js` | **200**, `Cache-Control: no-cache` |
| HTTP:80 | **301** → `https://mia.assafweb.com:443/health` |
| ALB target | **healthy** (IP `10.42.10.129:8000`, `eu-north-1a`) |
| Shadow E2E website lead | **`lead_0e24e7906e4d`** (session `web_b672592398a64cd3`; NBA `reflect`; handoff token prefix `mia1_`) |
| Gated writes | **off** — see §4 |

`CapabilityId.AWS_RUNTIME` remains **specified** in `app/core/capabilities.py` (`port=app.infra` does not exist; Lambda/SQS/WAF/AgentCore not this slice). The host itself is proven.

---

## 2. Region conflict (open ADOPT)

This AWS project is the **new AWS experience**. Regional resources can only be created in **`eu-north-1`**. Bible / `docs/PRODUCTION_BUILD.md` still pin first live to **`il-central-1`**.

Staging was built in **eu-north-1**. Assaf must **ADOPT** `eu-north-1` or move to an account that can use `il-central-1`. This report does not rewrite the Bible region.

Account: `535252061205`. Plan at deploy time: FREE.

---

## 3. Live resources (eu-north-1)

| Resource | Fact |
| --- | --- |
| VPC | `vpc-00c3befe7ccfc6127` (`10.42.0.0/16`) |
| Public subnets | `subnet-0b1b231958949a1ef` (1a), `subnet-03f33c9190be3fd7c` (1b) |
| Private subnets | `subnet-0f014b846c77bc2df` (1a), `subnet-0d5e07807bf138dbd` (1b) |
| NAT | `nat-0ef6fa54d22df1f4a` (one AZ, ADR-014) |
| ALB SG | `sg-0304bd2e7fc0deccf` (80/443) |
| Task SG | `sg-04dbd4714b6c00dc7` (8000 from ALB) |
| RDS SG | `sg-0077bf17bd700a124` (5432 from tasks) |
| RDS | identifier `mia`, Postgres 16, `db.t4g.micro`, private, backup retention **1 day** (Free tier rejected 7) |
| RDS endpoint | `mia.cfwiacoquk89.eu-north-1.rds.amazonaws.com` (not a secret) |
| ECR | `535252061205.dkr.ecr.eu-north-1.amazonaws.com/mia:latest` |
| ECS | cluster/service `mia` / `mia`; Fargate 0.5 vCPU / 1 GB; desired 1 running 1; `assignPublicIp=DISABLED`; execute-command **off**; platform **1.4.0**; task definition revision **5** |
| ALB | `arn:aws:elasticloadbalancing:eu-north-1:535252061205:loadbalancer/app/mia/86d2f055ae5eddda` |
| ALB DNS | `mia-1315187717.eu-north-1.elb.amazonaws.com` |
| Target group | `arn:...:targetgroup/mia/5030c2001f49e604` (IP, `/health/live`) |
| ACM | **ISSUED** `arn:aws:acm:eu-north-1:535252061205:certificate/81d1d33b-db27-48aa-a41a-1f150dc1c7a6` (`mia.assafweb.com`) |
| HTTPS listener | port 443, policy `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09` |
| HTTP listener | port 80, `HTTP_301` to HTTPS |
| DNS | Vercel: `mia.assafweb.com` CNAME → ALB DNS; apex CAA includes `0 issue "amazon.com"` plus existing Let’s Encrypt / Sectigo / Google |
| Secret | `mia/prod` (ECS injects `MIA_*`; unused keys empty) |
| Logs | `/ecs/mia` retention 30d |
| EventBridge | `mia-due-scan` rate(15 min), `mia-reconcile` rate(1 hour), ENABLED, persist-only |
| Alarms | `mia-unhealthy-hosts`, `mia-alb-elb-5xx` (no SNS) |

Failed ACM cert `d6bb2b51-2444-4b72-baa8-1692e61ebd5f` (`CAA_ERROR`) is unused. Do not attach it.

DNS is Vercel (`ns1.vercel-dns.com` / `ns2.vercel-dns.com`), not Route 53. Apex name for CAA must be **empty** in the Vercel UI (`@` is rejected as `Invalid name parameter`).

---

## 4. Shadow posture (still in force)

Task definition environment (non-secret flags):

| Flag | Value |
| --- | --- |
| `MIA_ENV` | `prod` |
| `MIA_DEMO_MODE` | `false` |
| `MIA_AUTOMATION_MODE` | `shadow` |
| `MIA_KILL_SWITCH` | `false` |
| `MIA_CALENDAR_WRITE` | `false` |
| `MIA_GMAIL_SEND` | `false` |
| `MIA_META_WRITE` | `false` |
| `MIA_AUTO_FOLLOWUP` | `false` |
| `MIA_AUTO_REPLY_INSTAGRAM` | `false` |
| `MIA_BROWSER_AUTOMATION` | `false` |
| `MIA_DYNAMIC_TOOL_DISCOVERY` | `false` |
| `MIA_INSTAGRAM_SENDER` | `direct` |
| `MIA_PUBLIC_BASE_URL` | `https://mia.assafweb.com` |

`/health` risk: `R4_meta_writes=approval`, `R5_destructive=deny`.  
`/health` providers: `sales_llm=false`, `composio=false`, `whatsapp_ingest=false` (keys unused in the box; not a failure of this host).

Not enabled this slice: prospect sends, Gmail send, Meta writes, follow-up send, instruction activation, R5 calendar provider delete, Meta/Gmail production webhooks.

---

## 5. Safe staging tests

| Test | Evidence |
| --- | --- |
| Process up | `/health/live` 200 |
| Schema + Postgres | `/health/ready` 200, `/health` `postgres=true` |
| TLS + public URL | `/health` `public_https=true` over **HTTPS** |
| Prod surface | `/docs` 404 |
| Widget cache | `widget.js` `Cache-Control: no-cache` |
| HTTP redirect | `curl -sI http://mia.assafweb.com/health` → 301 |
| Website session | `POST /v1/website/sessions` → `lead_0e24e7906e4d` |
| Graph NBA | `POST .../messages` → `next_action=reflect` (canned; sales LLM off) |
| Handoff | `POST .../handoff` → opaque `mia1_` token (raw token not logged here) |

Earlier HTTP-only shadow lead `lead_bf3ee20773af` is not this HTTPS proof. The HTTPS lead is `lead_0e24e7906e4d`.

---

## 6. Remaining operator steps (not blockers of this host)

1. Assaf **ADOPT** `eu-north-1` or migrate to `il-central-1`.
2. Vercel landing `NEXT_PUBLIC_MIA_BASE_URL=https://mia.assafweb.com` and redeploy (not set this slice).
3. Fill unused `mia/prod` keys only when Assaf wants live LLM/Composio; never paste values in chat.
4. Point Meta / Gmail webhooks at `https://mia.assafweb.com` only after those ingest secrets are in the box.
5. Optional: delete the mistaken Vercel CAA on name `a` (`a.assafweb.com`). Apex `@` / empty name already has `amazon.com`.
6. Do not flip `MIA_AUTOMATION_MODE` off shadow. Do not set calendar/Gmail/Meta/follow-up write flags.

---

## 7. What this is not

- Not production Meta/WhatsApp/Gmail send.
- Not AgentCore, Lambda graph, SQS, or WAF.
- Not a Bible rewrite of region to `eu-north-1`.
- Not `AWS_RUNTIME=alive` in the capability registry.
