# Mia

AssafWeb’s AI Growth & Sales Operator.

**Start here:** [`AGENTS.md`](AGENTS.md) → [`docs/PRODUCT.md`](docs/PRODUCT.md) → [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) → [`docs/DECISIONS.md`](docs/DECISIONS.md)

| Doc | Use |
| --- | --- |
| [`AGENTS.md`](AGENTS.md) | How to work in this repo |
| [`docs/PRODUCT.md`](docs/PRODUCT.md) | What Mia is |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Two graphs, channels, policy |
| [`docs/WIRING.md`](docs/WIRING.md) | Who calls whom (short map) |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | ADRs that still constrain |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Operator: kill switch, flags, health |
| [`docs/PRODUCTION_BUILD.md`](docs/PRODUCTION_BUILD.md) | Operator: AWS go-live |

Do not read `docs/archive/` unless you need history. The VNext rebuild spec is `docs/archive/MIA_REBUILD.MD`.

## Local

```
uv sync --group dev
uv run pytest
uv run ruff check app tests
uv run uvicorn app.main:app --reload
```

Fill `.env` from `.env.example`. Never commit `.env`. Never copy it onto Fargate.
