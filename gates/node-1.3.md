# Gates: Graph and code simplification

Scope: Remove duplicated work and reduce the largest accidental complexity while preserving the accepted architecture.

- [x] G1: Client knowledge leaf is verified.
  EVIDENCE: Parent-verified regression proves one capability retrieval per customer message and no legacy second lookup; the focused client-knowledge run passed 14 tests.
- [x] G2: Minimality leaf is verified with measured before/after evidence.
  EVIDENCE: Shared `mirror_sales_turn` reduced the three owned production files by 70 lines and both oversized request functions by 90 AST lines. Parent reran the 99-test focused suite plus Ruff and diff-check successfully.
- [x] G3: Graph, API, and safety regression tests pass together.
  CHECK: uv --offline --cache-dir .uv-cache run pytest -q -p no:cacheprovider tests/unit/test_visitor_knowledge.py tests/unit/test_website_client_graph.py tests/unit/test_vnext_graph_functions.py tests/unit/test_website.py tests/unit/test_vnext_principal.py tests/unit/test_sheets.py tests/unit/test_vnext_inbound_client.py
  EXPECT: /passed/
  EVIDENCE: Parent combined run passed 119 tests on 2026-08-28.
