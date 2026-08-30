# Gates: Truth and dead-surface alignment

Scope: Runtime configuration, operational truth, and accepted ADR-039 removal agree.

- [x] G1: Both child leaves have complete evidence and no overlapping unfinished edits.
  EVIDENCE: Truth/configuration and ADR-039 campaign-removal leaves are complete. A fresh HEAVY reviewer repaired collapsed tests and classified every campaign deletion; no source edits remain active in either leaf.
- [x] G2: Focused config, probe, and owner-routing tests pass together.
  CHECK: uv --offline --cache-dir .uv-cache run pytest -q -p no:cacheprovider tests/unit/test_health.py tests/unit/test_scripts.py tests/unit/test_owner_tasks.py tests/unit/test_commitments.py tests/unit/test_due_scan_worker.py tests/unit/test_approvals.py tests/unit/test_events.py
  EXPECT: /passed/
  EVIDENCE: Parent combined run passed 171 tests on 2026-08-28. The broader HEAVY campaign-affected run also passed 309 tests.
