# Mia

Assaf’s public Dude-clone demo. Hebrew-native. Named Mia. Two surfaces, one Contacts CRM. Composio actually runs.

**Start here:** [`AGENTS.md`](AGENTS.md) → [`docs/PRODUCT.md`](docs/PRODUCT.md) → [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) → [`docs/DECISIONS.md`](docs/DECISIONS.md)

| Doc | Use |
| --- | --- |
| [`AGENTS.md`](AGENTS.md) | How to work in this repo |
| [`docs/PRODUCT.md`](docs/PRODUCT.md) | What Mia is |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Two loops, Contacts CRM, channels |
| [`docs/WIRING.md`](docs/WIRING.md) | Who calls whom |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | ADRs that still constrain |
| [`docs/PRODUCTION_BUILD.md`](docs/PRODUCTION_BUILD.md) | Operator: AWS go-live |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Emergency stop for high-risk writes |

Do not read `docs/archive/` unless you need history.

## Surfaces

1. **Telegram owner** — Dude talk. House Composio tools. Writes Contacts + Activity without asking for a URL.
2. **Website visitors** — identify-then-sell on the glass Hebrew widget at `https://mia.assafweb.com/v1/website/widget.js`. No Contacts row, WhatsApp handoff, or Telegram ping until phone or email exists.

## CRM

Locked spreadsheet `1HW8mnc9GFXraS6oG5VIxFcJvZq9gMDJBFRxY2mpVOhI`. Live tabs **Contacts** and **Activity** only. Archive tabs are gone.

Contacts A1:N1: `שם | טלפון | אימייל | תאריך | עסק | מקור | שפה | מה רוצים | סטטוס | סיכום שיחה | הבא | נוצר | עודכן | פינג לאסף`

Activity: `מתי | מי | ערוץ | מה עשתה | תוצאה`

No `lead_` ids. No row without phone or email.

## Composio

House entity (`MIA_COMPOSIO_USER_ID`). Same apps as Cursor: Sheets, Gmail, Instagram, LinkedIn, GA, GSC, Calendar, WhatsApp. Reads run. Mail send only if the owner asked. No social publish. Assaf sends customer WhatsApp. If `/health` is true, Telegram must not say disconnected.

## Local

```
uv sync --group dev
uv run pytest
uv run ruff check app tests
uv run uvicorn app.main:app --reload
```

Fill `.env` from `.env.example`. Never commit `.env`. Never copy it onto Fargate.
