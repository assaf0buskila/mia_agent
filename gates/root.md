# Gates: Mia reliability and simplification

Scope: All requested Mia repairs are implemented, live owner capabilities are proven, and the codebase is smaller or demonstrably simpler without architecture drift.

- [x] G1: Truth/configuration and accepted campaign-removal work are complete.
  EVIDENCE: Both leaves and node 1.1 are complete; parent combined truth/campaign run passed 171 tests, fresh HEAVY affected run passed 309, and final docs alignment records no live/deploy claim.
- [ ] G2: Telegram voice works through the live input path and returns text through OwnerGraph.
  EVIDENCE: pending
- [ ] G3: Authorized Sheets read/write plus Search Console, GA4, and LinkedIn owner capabilities are proven through the capability layer.
  EVIDENCE: pending
- [x] G4: Website knowledge retrieval occurs once per customer message.
  EVIDENCE: Capability-count regression and combined graph/API/safety run passed; node 1.3 records 14 focused and 119 combined tests.
- [x] G5: The largest code seams have a measured remove/keep/refactor disposition and accepted bounded refactors are complete.
  EVIDENCE: All 22 live Python files at or above 500 lines were classified. The accepted helper extraction reduced owned production source by 70 lines and both oversized request functions by 90 AST lines; parent verified 99 focused tests.
- [x] G6: Full lint, test, deterministic eval, safety, and independent review gates pass.
  EVIDENCE: Final current-tree run passed all 2,375 collected tests; Ruff and origin binding passed; 233/233 deterministic evals plus routing 20/20 passed; second independent HEAVY rereview found no unresolved P0/P1/P2.
- [x] G7: Every production/script Python function file receives a measured cleanup disposition, and accepted final simplifications are independently verified.
  EVIDENCE: Phase 1.5 closes with exact 164/164 disposition coverage and `gates/evidence/function-cleanup-heavy-twenty-third-review.md`: persistent 126 + 42 matrices, fresh 168-case four-effect review, full 2,475-test regression, Ruff, origin binding, 273/273 evals, diff-check, and no unresolved P0/P1/P2.
