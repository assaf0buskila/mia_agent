# ADR-014 First AWS production: Fargate + RDS + Secrets Manager box

- **Status:** accepted
- **Date:** 2026-08-22
- **Assaf:** ADOPT (AWS production; keys in a box, not pasted into Mia)

**Context**
Bible §29 wants AWS for ingress, secrets, and runtime. Assaf wants production on AWS and must not put provider keys in git, chat, or a host `.env` file. A custom Lambda that “holds the keys” still has to give those keys to whoever calls OpenAI/Composio. LangGraph on Lambda-only is rejected in the Bible.

**Decision**
1. **Key box** is AWS Secrets Manager secret `mia/prod` (KMS). Assaf creates the JSON keys (`MIA_*` SECRET fields). Never commit values. Never paste values in chat.
2. **ECS Fargate** injects those JSON keys as container environment variables at task start ([Pass Secrets Manager secrets through Amazon ECS environment variables](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/secrets-envvar-secrets-manager.html), platform 1.4.0+). Mia’s adapters keep reading `Settings()` from env. Mia’s **source code and git never contain keys**.
3. **RDS PostgreSQL** is the system of record. App uses `psycopg` (`postgresql+psycopg://`).
4. **HTTPS** is ALB + ACM on `https://mia.assafweb.com`. Landing-page `NEXT_PUBLIC_MIA_BASE_URL` is that origin.
5. **Lambda** is **not** the sales graph and **not** the key box. Lambda webhook ingress (fast ACK → SQS FIFO → Fargate) stays the next AWS slice after first live is healthy. AgentCore stays a later benchmark (ADR not written until measurements exist).

**Consequences**
- **Security:** Task execution role may `secretsmanager:GetSecretValue` on `mia/prod` only. Logs still redact. GraphState still forbids secrets. Rotating the secret requires a new Fargate deployment (ECS does not hot-reload env secrets).
- **Reliability:** One FastAPI process still verifies webhooks and runs LangGraph until the Lambda/SQS split ships.
- **Cost/lock-in:** Always-on Fargate, not Lambda-per-request for conversations. Provider-neutral domain layer unchanged.
- **Migration/files:** `deploy/Dockerfile`, `deploy/ecs-task-definition.example.json`, `psycopg[binary]`, `app/db/session.py` DSN pin. `CapabilityId.AWS_RUNTIME` stays **specified** until ALB+RDS are actually running.

**Alternatives considered**
Keys in laptop `.env` copied onto a VPS — rejected for production. Custom Lambda as the only place keys live, Mia calling Lambda per OpenAI request — rejected this slice (duplicates Secrets Manager; becomes a tool gateway). AgentCore now — rejected until a frozen runtime benchmark exists (historical notes in `docs/archive/RUNTIME_DECISION_PLAN.md`). Vercel/Cloudflare Workers for the graph — rejected.
