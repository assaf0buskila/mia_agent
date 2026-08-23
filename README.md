# Mia

AssafWeb’s AI Growth & Sales Operator.

**Start here:** [`docs/PROJECT_MAP.md`](docs/PROJECT_MAP.md)

| Doc | Use |
| --- | --- |
| [`AGENTS.md`](AGENTS.md) | Operating rules for coding agents |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | v1 channels and adapters |
| [`docs/BRAIN_ARCHITECTURE.md`](docs/BRAIN_ARCHITECTURE.md) | Memory, knowledge, retrieval, owner agent, voice |
| [`docs/PRD.md`](docs/PRD.md) | Short living contract |
| [`docs/BUILD_STATUS.md`](docs/BUILD_STATUS.md) | What is alive / next |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | ADRs |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Kill switch, flags, health |
| [`docs/PRODUCTION_BUILD.md`](docs/PRODUCTION_BUILD.md) | AWS go-live sequence |
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | Current slice only |

Do not read `docs/archive/` unless you need history.

## Local

```
uv sync --group dev
uv run pytest
uv run ruff check app tests
uv run uvicorn app.main:app --reload
```

Fill `.env` from `.env.example`. Never commit `.env`. Never copy it onto Fargate.

Live: `https://mia.assafweb.com` (eu-north-1).
