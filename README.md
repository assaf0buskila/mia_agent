# Mia v2

Mia is Assaf's private Telegram assistant and the public sales demonstration on
assafweb.com. Both experiences use model reasoning, public website knowledge and
durable conversation history. Telegram additionally exposes owner-authorized tools
and explicit lasting memory. Replies are text; Telegram accepts voice and images.

The current product contract and implementation evidence live in [MIA_V2.md](MIA_V2.md).
[AGENTS.md](AGENTS.md) governs development; [TASKS.md](TASKS.md) tracks release work.
These replace historical project instructions.

## Code layout

- `app/api/`: authenticated Telegram ingress and credential-bound website API.
- `app/surfaces/`: owner and website reasoning entrypoints and shared CRM access.
- `app/graph/`, `app/tools/`: owner reasoning loop, typed tools and capability registry.
- `app/services/`: exact owner approvals, durable CRM and synchronization rules.
- `app/integrations/`: provider, Composio, transcription and external adapters.
- `app/brain/`: sourced knowledge, explicit owner memory and retrieval.
- `app/db/`, `migrations/`: canonical history and durable application records.
- `app/workers/`: Telegram turns, CRM delivery/import, owner reminders and maintenance.
- `app/web/`: the website widget. `tests/` contains behavioral and isolation checks.
- `deploy/`, `scripts/`: container, infrastructure examples and release/probe helpers.

## Local development

Use Python 3.12 and uv. Configure names from `.env.example`; never commit real keys.
Tests ignore `.env` and use injected adapters. Production secrets stay in AWS Secrets
Manager and are injected into Fargate.

```powershell
uv sync --frozen --group dev
$env:MIA_ENV = 'test'
uv run pytest --basetemp=.cache/pytest-local
uv run ruff check app tests
node --check app/web/ask_mia.js
uv run python scripts/assert_origin_bind.py
uv run uvicorn app.main:app --reload
```

For PostgreSQL checks, set `MIA_TEST_POSTGRES_URL` to an isolated disposable test
database. Fixtures create isolated schemas; never point tests at production.
Apply deployment migrations with `mia-migrate`; production startup never runs
`create_all`. Refresh configured public sources with `mia-ingest-knowledge`.

## Runtime rules

Owner access uses numeric Telegram IDs. Verified reads run directly; permitted
external writes require immutable, expiring approval and current-target validation.
Unknown effects stay unavailable. Conversation history saves automatically; lasting
memory requires an explicit request. Website visitors cannot access owner tools or
private memory. Possessing a phone/email never grants conversation access.

Contacts and Activity are durable database records with an editable Google Sheets
view. Contact capture commits independent delivery jobs before acknowledging capture.
The CRM worker handles delivery and Sheet imports; conflicting edits require owner
resolution. The manual WhatsApp contact link remains available. Agent WhatsApp and
Baileys transports are retired.

## Release and live acceptance

The authorized release order is implementation and cleanup, mechanical checks,
independent HEAVY review, exact-SHA CI/image verification, migration, rollout, then
health and readiness verification. Keep the previous task/image for rollback. The
release checklist and actual deployment evidence belong in TASKS.md and MIA_V2.md.
Do not infer live model, media or delivery quality from mocked tests.

Assaf tests natural Telegram conversation, Hebrew voice/image context, explicit memory,
approval/rejection and multiple proposals; website exploratory/pricing/strong-intent
conversations, voluntary contact capture and continued chat; then Telegram lead delivery,
Contacts/Activity rows, Sheet edits and conflict resolution. Use identified test contacts.
