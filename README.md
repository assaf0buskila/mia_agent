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

The standalone AssafWeb OS owner integration is local and disabled by default.
See [the integration contract and release gates](docs/OWNER_OS_LOCAL_INTEGRATION.md)
and [the authentication threat model](docs/OWNER_OS_AUTH_THREAT_MODEL.md).
Its PostgreSQL session migration and dedicated roles require separate approval;
merged backend or mobile code is not evidence that the owner app is deployed.

Production runs on the GCP VM `mia` in `me-west1-a`. Settings come from Secret Manager
`mia-env`; PostgreSQL and runtime database credentials stay on the VM. Never copy `.env`.
The complete runbook is `ops/gcp/README.md`.

1. Merge to `master` and wait for **Mia v2 checks** on the exact SHA.
2. Pause scheduled jobs and create a verified database backup.
3. **Owner-session database, before the first image that contains V2-08 (and before any
   later image if the step has not run yet).** `mia-migrate` does NOT reach `assaf_os_auth`.
   It migrates only the `mia` database. The live owner session table is in `assaf_os_auth`,
   so its schema change is a separate step that needs the owner's go. Run it in this order:
   1. Back up `assaf_os_auth`. `sudo mia backup` dumps only `mia`, so also run this on the VM:
      ```sh
      f=/var/lib/mia/backups/assaf_os_auth-$(date -u +%Y%m%dT%H%M%SZ).dump
      sudo docker exec mia-postgres-1 pg_dump -U mia -d assaf_os_auth --format=custom \
        | sudo tee "$f" >/dev/null && sudo chmod 600 "$f" && echo "$f"
      ```
   2. Dry run. It changes nothing and must print `"status":"session_chains_dry_run"`:
      ```powershell
      python ops/assaf-os/provision_auth.py --migrate-session-chains
      ```
   3. Only with the owner's explicit go, apply it. It is one transaction, and running it
      again changes nothing:
      ```powershell
      python ops/assaf-os/provision_auth.py --migrate-session-chains --apply
      ```
   4. Require `"status":"session_chains_applied"` with `"role_posture":"pass"`. This means
      the schema check (7 columns, 2 constraints) and the runtime-role posture check both
      passed. Any `"status":"failed"` stops the release: do not deploy the image.
   5. Only then deploy (step 5). The new columns are additive and nullable, so the running
      image is unaffected. A new image without them fails closed with
      `503 owner_auth_unavailable`.
4. For a narrow setting change, preserve the full live secret:
   ```powershell
   .\ops\gcp\push-settings.ps1 -ProjectId mia-assafweb -PatchSecret -Set @{ KEY = "value" }
   ```
5. From a clean checkout, deploy the exact SHA:
   ```powershell
   .\ops\gcp\deploy.ps1 -ProjectId mia-assafweb -Ref $SHA
   ```
   The VM builds that commit, fetches settings, runs `mia-migrate` (database `mia` only), and
   starts the app only after migration succeeds.
6. Confirm `/health` reports the exact SHA, `/health/ready` returns 200, and `sudo mia status`
   shows the expected HTTPS and job state before resuming jobs.
7. Smoke-check owner authentication. `$OS_ORIGIN` is the configured `MIA_OWNER_OS_ORIGIN`:
   ```powershell
   curl.exe -s -o NUL -w "%{http_code}\n" -H "Origin: $OS_ORIGIN" https://mia.assafweb.com/v1/owner/auth/session
   curl.exe -s -D - -o NUL -X POST -H "Origin: $OS_ORIGIN" https://mia.assafweb.com/v1/owner/auth/refresh
   ```
   The first must print `401`. The second sends no cookie. It must answer `401`, with a
   `Set-Cookie` that deletes `__Host-mia-owner` (`Max-Age=0`) and no `Server-Timing` header.
   A `503` means the `assaf_os_auth` step did not run: run step 3, or redeploy the previous
   SHA.

Historical AWS deploy evidence and gotchas remain in `HANDOFF.md`; they are not the current
production procedure.
