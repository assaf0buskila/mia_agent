# Gates: Exhaustive function-file cleanup

Scope: Audit every function-bearing production/script Python file, then implement only high-confidence behavior-preserving simplifications.

- [x] G1: All 164 baseline function-bearing files are covered exactly once by the three audit leaves.
  EVIDENCE: Parent rebuilt the current inventory and compared it with the three evidence matrices: 23 + 73 + 68 rows, 164 unique paths, zero missing, extra, or duplicate paths. See `gates/evidence/function-audit-api.md`, `gates/evidence/function-audit-domain.md`, and `gates/evidence/function-audit-infra.md`.
- [x] G2: Every audited file has a concrete KEEP, SIMPLIFY, MERGE, or REMOVE disposition with symbol/caller/test/risk evidence.
  EVIDENCE: All three leaf ledgers are 4/4 checked; each matrix records one disposition and the required symbol, caller/test, risk, and benefit evidence for every assigned path.
- [x] G3: Cross-file findings are synthesized without reversing accepted ADRs or changing Mia's one-owner-agent runtime shape.
  EVIDENCE: `PLAN.md` Phase 1.5 accepts two bounded implementation waves while preserving the two-graph/shared-core contract and one production OwnerGraph agent; the first three implementation leaves retain those boundaries.
- [x] G4: Every accepted cleanup is implemented with measured before/after evidence and focused regression proof, or the synthesis explicitly proves that no further safe edit earns its risk.
  EVIDENCE: `gates/evidence/function-cleanup-synthesis.md` and the implementation evidence files record every accepted edit, rejected/deferred finding, parent rerun, and all twenty-two review-repair waves. Current measurements are 164 function files, 1,646 definitions, 42,537 physical lines, 37,805 nonblank lines, and 36 strict C901 findings; no unavailable physical baseline is invented.
- [x] G5: Full current-tree regression, deterministic evals, lint, diff-check, and a fresh HEAVY review pass after cleanup.
  EVIDENCE: `gates/evidence/function-cleanup-heavy-twenty-third-review.md` records 2,475/2,475 pytest, whole-tree Ruff, origin-bind ok, 273/273 evals, diff-check exit 0, exact 164/164 inventory, and no unresolved P0/P1/P2.
