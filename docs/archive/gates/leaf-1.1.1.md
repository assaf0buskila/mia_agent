# Gates: Phase A discover actual architecture

Scope: Inventory what really runs today from code, not old docs.

- [x] G1: Entrypoint routers documented from app/main.py
  CHECK: uv run python -c "from pathlib import Path; t=Path('app/main.py').read_text(encoding='utf-8'); assert 'telegram_router' in t and 'website_router' in t; from app.api.telegram import router; print(router.prefix + '/webhook')"
  EXPECT: telegram/webhook
  EVIDENCE: /v1/telegram/webhook

- [x] G2: Sales LangGraph still exists as strangler inner graph
  CHECK: uv run python -c "from pathlib import Path; print('ORCH' if 'StateGraph' in Path('app/graph/orchestrator.py').read_text(encoding='utf-8') else 'NO')"
  EXPECT: ORCH
  EVIDENCE: ORCH
