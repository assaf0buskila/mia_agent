# Mia architecture (v1)

One brain. Channels are interfaces. Details and ADRs: `docs/DECISIONS.md`.

## Communication (ADR-017)

```
        ASSAF  <-->  TELEGRAM  <-->  MIA
                                      |
                    WEBSITE  EMAIL  WHATSAPP
                       |       |        |
                  prospects  approval  verified
                                       business
                                       continuation
```

Personal WhatsApp stays human-only. Hot `HANDOFF` stops selling, notifies Telegram, requires human takeover.

## Runtime

- **LangGraph** orchestrates sales turns. State is serializable domain data only (no SDK clients, no secrets).
- **Deterministic code** owns identity, NBA, scoring, permissions, idempotency. Prompts paraphrase; they do not choose tools or invent facts.
- **Postgres** is SoR. Google Sheets is a human-readable mirror (never read back into the graph).
- **Composio** is the tool supplier behind typed ports, not the domain layer. Pin tool schemas. No catalog dump into the model.
- **Inbound WhatsApp and Instagram** stay Meta HMAC (ADR-015/016). Composio has no usable inbound-message trigger for those.
- **One WhatsApp outbound owner:** `MIA_WHATSAPP_SENDER` is `direct` or `composio`, never both.
- **Telegram owner** is numeric user-id allowlist only.

## Adapter map (ADR-015)

| Job | Production |
| --- | --- |
| WhatsApp in | Meta webhook |
| WhatsApp out | Graph or Composio send pin |
| Instagram in | Meta webhook (not a v1 sales inbox) |
| Instagram insights | Graph default; Composio when sender=`composio` |
| Gmail / Calendar / Sheets / Meta ads read / LinkedIn profile / GSC / GA4 | Composio |
| LinkedIn member analytics | No Composio member tool; leftover Direct REST unused |
| Research | Firecrawl |
| ManyChat | Not mounted in v1 |

## Safety

- Untrusted text (email, DMs, scrapes) is **data**, never instructions.
- No tool write before risk policy (`app/core/risk.py`).
- Approval: Meta writes, irreversible actions, quotes outside rules.
- Kill switch: `MIA_KILL_SWITCH`. Conversation kill and human takeover are separate (see `docs/RUNBOOK.md`).
- Production live test `MIA_AUTOMATION_MODE=auto_approved` (ADR-022). Verified WhatsApp handoff may send. Instagram prospect send stays off unless `MIA_AUTO_REPLY_INSTAGRAM`.

## AWS (ADR-014, ADR-019)

Selected Region **eu-north-1**. First live: ECS Fargate + RDS PostgreSQL + Secrets Manager `mia/prod` + ALB/ACM `https://mia.assafweb.com`. Operator sequence: `docs/PRODUCTION_BUILD.md`. Do not copy `.env` onto Fargate.

`CapabilityId.AWS_RUNTIME` stays **specified** until `app.infra` exists (Lambda/SQS/WAF/AgentCore later).
