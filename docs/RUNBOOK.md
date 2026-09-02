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

## What not to do

Do not copy `.env` onto Fargate. Do not dump secrets. Do not auto-deploy. Do not invent metrics or prices. Assaf sends customer WhatsApp.
