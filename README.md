# Mia

Mia is two products sharing one FastAPI app:

- **Owner assistant** — Assaf's private Telegram bot. It reads and writes his connected
  tools (Gmail, Sheets, Calendar through Composio), with an approval step for every write.
- **Website sales chat** — the public widget on assafweb.com. It answers from public
  knowledge only, and turns a volunteered phone or email into a CRM lead delivered to
  Telegram.

Hebrew by default, follows the visitor's language. Voice and images in, text out.

## How a request flows

**Owner (Telegram)**
`app/api/telegram.py` → `app/workers/telegram_owner.py` → `app/surfaces/owner.py`
→ `app/domain/owner/brain.py:answer_owner` → `app/graph/owner_agent.py`.
Tools live in `app/tools/registries/owner_tools.py`. Writes become approval proposals;
the Telegram buttons resolve in `app/api/telegram.py:_handle_callback`.

**Website**
`app/web/ask_mia.js` (served at `/v1/website/widget.js`) → `app/api/website.py`
→ `app/surfaces/site_v2.py:run_site_v2_turn`.
Contact capture: a server regex finds a phone/email in the visitor's own text, a consent
classifier confirms it was volunteered for follow-up, then
`app/services/crm_v2.py:capture_site_lead` writes the contact plus durable delivery jobs.
`app/workers/crm_delivery.py` (5-second poll, started in-process by
`app/workers/crm_runtime.py`) sends the Telegram lead brief and syncs Sheets.

## Code layout

| Path | What it is |
|---|---|
| `app/api` | HTTP ingress: Telegram webhook, website API. `/health` is in `app/main.py` |
| `app/surfaces` | One entry point per conversation surface: `owner`, `site_v2`, `crm` |
| `app/domain` | Business rules. `app/domain/owner` is the owner brain |
| `app/graph`, `app/tools` | Owner tool loop and typed tools |
| `app/services` | CRM (`crm_v2`), approvals, notifications |
| `app/integrations` | LLM providers, Composio, Sheets, Gmail, transcription |
| `app/brain` | Knowledge retrieval and explicit owner memory |
| `app/db`, `migrations` | SQLAlchemy models, `LeadStore`, SQL migrations |
| `app/workers` | CRM delivery, due scan, migrate, knowledge ingest, webhook registration |
| `app/web` | The widget |
| `tests/unit` | The suite: pytest plus `widget_behavior.test.js` under node |
| `deploy`, `scripts` | Dockerfile; `deploy_ecs_revision.py`, `assert_origin_bind.py`, probes |

## Run it locally

```bash
uv sync --frozen --group dev
MIA_ENV=test uv run pytest --basetemp=.cache/pytest-local
uv run ruff check app tests
node tests/unit/widget_behavior.test.js
uv run uvicorn app.main:app --reload      # widget preview at /v1/website/preview
```

Settings are `MIA_*` environment variables (`app/core/config.py`); the names are in
`.env.example`. Tests never read `.env`. PostgreSQL tests need `MIA_TEST_POSTGRES_URL`
pointing at a disposable database; without it they skip (7 today). Apply migrations
with `uv run mia-migrate`; production never runs `create_all`.

## Deploy

Production is ECS Fargate — cluster and service `mia`, region `eu-north-1`, images in
ECR repository `mia`. Secrets come from Secrets Manager. Never copy `.env` anywhere.

1. Merge to `master` and wait for **Mia v2 checks** to pass on the exact SHA.
2. From a clean checkout at that SHA:
   ```bash
   git archive $SHA | docker build -f deploy/Dockerfile \
     --provenance=false --sbom=false --platform linux/amd64 \
     --build-arg MIA_BUILD_SHA=$SHA -t <ecr>/mia:v2-$SHA -
   ```
   The flags matter: `deploy_ecs_revision.py` rejects OCI image indexes, which is what
   a default Docker 29 build produces.
3. Push, then read the digest from `aws ecr describe-images`.
4. `uv run python scripts/deploy_ecs_revision.py --v2-release --image-uri <ecr>/mia@<digest> --sha $SHA`
   It verifies the image label and env match `$SHA` through the ECR API (no local Docker
   needed), requires `HEAD == $SHA` with a clean tree, and registers the next `mia:N`.
5. `aws ecs update-service --cluster mia --service mia --task-definition mia:N`, wait stable.
6. Re-pin scheduler `mia-due-scan` to `mia:N`. Leave `mia-reconcile` disabled.
7. Confirm `https://mia.assafweb.com/health` reports `deployment.commit_sha == $SHA`.

Rollback is `update-service` to the previous revision. Machine-specific gotchas
(Docker Desktop, the credential helper, `aws login` expiry) are in `HANDOFF.md`.
