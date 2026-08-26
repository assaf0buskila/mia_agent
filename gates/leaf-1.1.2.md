# Gates: 1.1.2 Composio ports and pins

Scope: Every typed port vs PRELOADED_TOOLS vs what Telegram can actually call.

- [x] G1: PRELOADED_TOOLS count and write vs read split
  CHECK: uv run python -c "from app.tools.registries.mia_preloaded_tools import PRELOADED_TOOLS; w=sum(1 for t in PRELOADED_TOOLS if t.write); print(len(PRELOADED_TOOLS), w, len(PRELOADED_TOOLS)-w)"
  EXPECT: 20 5 15
  EVIDENCE: 20 5 15

- [x] G2: Each integration module named with its execute slug(s)
  EVIDENCE: gmail.py pins FETCH_EMAILS/FETCH_MESSAGE/CREATE_DRAFT/SEND_DRAFT; PRELOADED_TOOLS missing FETCH_EMAILS+CREATE_DRAFT+SEND_DRAFT; 20 catalog rows 5 writes
