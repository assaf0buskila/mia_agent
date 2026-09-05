# Mia

Mia is the AssafWeb AI operator. She sells for AssafWeb on the website and runs
AssafWeb's back office for Assaf on Telegram. Hebrew-native. Two surfaces, one Contacts
CRM, house Composio tools that actually run.

The Telegram side is deliberately Dude-like — warm, short, tool-calling, no ceremony.
That is the owner UX, not the product.

## Two surfaces

**Website Mia** sells. A glass Hebrew widget on assafweb.com. She answers published
product facts first, in the visitor's language, then works toward an offer and asks for
a phone or email. Nothing reaches the CRM, Telegram or WhatsApp until she has one.
She does not invent prices, metrics or delivery dates.

**Owner Mia** operates. Private Telegram for Assaf, numeric user-id allowlist only.
Sheets CRM, Gmail reads and drafts, Instagram insights, LinkedIn, GA, GSC, Calendar.
Text and voice in, text out. Writes go through approvals; high-risk ones stay gated.

## Run it

```
uv sync --group dev
uv run pytest
uv run ruff check app tests
uv run uvicorn app.main:app --reload
```

Package manager is **uv**. Fill `.env` from `.env.example`. Never commit `.env`, never
copy it onto Fargate, and keep example files free of real phones and tokens. Production
keys live in AWS Secrets Manager `mia/prod`.

## Which doc to read

Read these four, in this order, before touching code. Then read only the code your task
touches.

| Doc | Use |
| --- | --- |
| [`AGENTS.md`](AGENTS.md) | How to work in this repo, and what is never allowed |
| [`docs/PRODUCT.md`](docs/PRODUCT.md) | What Mia is and how each surface must behave |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Loops, who calls whom, the brain, the CRM, the runtime |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | ADR index. Open a single record only when your task touches it |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Deploy, migrate, roll back, stop, alarms, restore |

Do not load every ADR. Historical build documents are not in the tree; they are in git
history.
