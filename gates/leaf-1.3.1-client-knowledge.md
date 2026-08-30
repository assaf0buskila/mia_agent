# Gates: Single website knowledge retrieval

Scope: ClientGraph owns knowledge retrieval exactly once and the inner sales flow consumes the retrieved data without a second lookup.

- [x] G1: A regression test proves one knowledge capability execution per website message.
  CHECK: uv run pytest -vv -p no:cacheprovider tests/unit/test_visitor_knowledge.py -k website_client_graph_executes_knowledge_once
  EXPECT: /passed/
  EVIDENCE: 2026-08-28 — 1 passed, 6 deselected. The HTTP route invokes ClientGraph; the regression counted exactly one execute_capability call and observed only the capability retrieval path.
- [x] G2: No live website path passes a second knowledge lookup into the legacy inner graph.
  EVIDENCE: 2026-08-28 — `rg -n "_website_knowledge_lookup|knowledge_lookup=" app/api/website.py app/agents/client/graph.py` returned no matches. ClientGraph calls `build_graph(store, reply_port=reply_port)` without the legacy callback; website no longer defines or injects one.
- [x] G3: Published-facts answer-then-ask behavior remains covered and passing.
  CHECK: uv run pytest -q -p no:cacheprovider tests/unit/test_visitor_knowledge.py
  EXPECT: /passed/
  EVIDENCE: 2026-08-28 — 7 passed. Includes the published-facts visitor knowledge and live ClientGraph composition coverage.
