# Runtime decision plan

**Date:** 2026-08-22  
**Status:** First AWS **host** accepted (ADR-014). AgentCore / Lambda-graph still **DEFER** until the frozen benchmark.  
**Eventual ADR (after benchmark):** `docs/adr/ADR_RUNTIME_SELECTION.md` — do not write that file until measurements exist.  
**Related:** ADR-014 in `docs/DECISIONS.md`, Adjustment C, `docs/PRE_PRODUCTION_GAP_REPORT.md`, `docs/PERFORMANCE_BUDGET.md`, `docs/PRODUCTION_BUILD.md`

## CURRENT BIBLE DIRECTION

AWS owns production ingress, queues, secrets, and runtime (`docs/PRD.md` §29). Assaf ADOPT (2026-08-22) first live **host**: ECS Fargate + RDS PostgreSQL + Secrets Manager box `mia/prod` + ALB/ACM on `https://mia.assafweb.com`. `CapabilityId.AWS_RUNTIME` stays **specified** until that ALB+RDS path actually runs. `app.infra` does not exist. AgentCore is a candidate, not a lock-in (`docs/PROVIDER_MATRIX.md`).

## Current implementation (candidate 1 — still the process)

FastAPI process: `uvicorn app.main:app`. Graph, adapters, and webhooks share the process. Production that process runs **on Fargate** (ADR-014), not on a VPS `.env`. Local laptop still uses `.env`. SQLite is local-only; production is RDS via `MIA_DATABASE_URL` from the box.

## Candidate 2 (still not chosen)

Amazon Bedrock AgentCore Runtime (LangGraph-capable, versioned deploys, model-flexible per control-file citations). Re-check official docs before any migrate. **Do not implement AgentCore this slice.**

## Split that is already decided in the control file (not first live)

| Job | Preferred shape | Today | First live (ADR-014) |
| --- | --- | --- | --- |
| Key box | Secrets Manager `mia/prod` | Laptop `.env` | SM JSON; ECS injects `MIA_*` |
| Webhook receipt, signature, fast ack, enqueue | Lambda (or equivalent) → SQS FIFO | In-process FastAPI | Still in-process on Fargate. Lambda ingress is the **next** AWS slice |
| Long LangGraph / model / tools | Benchmarked worker (Fargate **or** AgentCore) | Same process as webhook | Same process on Fargate |
| Scheduled due-scan / reconcile | EventBridge / short job | Local CLI `mia-due-scan`, `mia-reconcile` | Same CLIs until a scheduled task exists |

**Lambda is not the key box.** A Lambda that holds keys and hands them to Mia duplicates Secrets Manager and becomes a tool gateway. Rejected (ADR-014).

## Frozen benchmark workload (required before AgentCore ADR)

Same scenario on Fargate vs AgentCore:

1. Qualified lead message  
2. Load lead + sales state  
3. One database read  
4. Default conversation model  
5. One mock or staging tool read  
6. Persist  
7. Return response  

Also: owner daily brief, calendar availability, campaign analysis request, one async-shaped research request.

Measure: cold start, warm, P50, P95, error rate, timeout, deploy/rollback, session isolation, traces, secrets, networking, monthly cost, ops burden, lock-in.

## Allowed ADR outcomes (Assaf chooses after benchmark)

- keep Fargate as the LangGraph runtime  
- migrate graph to AgentCore  
- Lambda ingress plus Fargate worker  
- Lambda ingress plus AgentCore  
- defer the split until volume justifies it  

**Recommendation until benchmark: KEEP Fargate for LangGraph. DEFER AgentCore.** Ingress split (ack vs work) is the next AWS slice after first live is healthy — not a stealth AgentCore build.

## What this plan forbids

Implementing `app.infra`, AgentCore agents, SQS, WAF, or moving sales reasoning into a vendor agent framework **this slice**. First live Docker/ECS/RDS/SM templates in `deploy/` are the accepted host, not those later pieces.
