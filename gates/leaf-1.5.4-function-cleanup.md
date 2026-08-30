# Gates: Function-audit synthesis, bounded implementation, and review

Scope: Reconcile all 164 file dispositions, implement only justified simplifications, and verify the complete current tree.

- [x] G1: The three evidence files cover exactly the 164-file baseline with no duplicate or missing path, and synthesis ranks every non-KEEP finding.
  EVIDENCE: `gates/evidence/function-cleanup-synthesis.md` records 23 + 73 + 68 unique paths, zero coverage defects, and reconciles all 21 ranked findings.
- [x] G2: Accepted edits preserve behavior and reduce measured duplication, branch count, function size, or dead surface; rejected/deferred findings record why cleanup risk exceeds benefit.
  EVIDENCE: synthesis and four implementation evidence files record 1,622 -> 1,614 definition lines, 8 -> 1 phrase matchers, seven dead/rejected helpers removed, four duplicate fallbacks removed, and strict C901 restored from an interim 38 to the 37 baseline; explicit no-change conclusions retain risky policy seams.
- [x] G3: Focused tests cover every changed behavior seam and pass without weakened assertions.
  EVIDENCE: parent reruns passed 68 owner-boundary, 170 approval, 286 mechanical, and 108 delivery/client-trust tests; the stale-caller repair run passed 164 tests and kept explicit delivery/authority assertions.
- [x] G4: Full pytest, Ruff, origin binding, deterministic evals, routing eval, and diff-check pass on the final tree.
  EVIDENCE: current repaired tree: 2,475 pytest passed; Ruff `app tests scripts` passed; origin-bind ok; all ten eval families passed 273/273 including calendar and routing 20/20; diff-check exit 0.
- [x] G5: A fresh HEAVY reviewer with no implementation context finds no unresolved P0/P1/P2 and confirms all 164 files were accounted for.
  EVIDENCE: `gates/evidence/function-cleanup-heavy-twenty-third-review.md` passes after 126 + 42 persistent cases, a fresh 168-case four-effect matrix, full 2,475-test regression, Ruff, origin binding, 273/273 evals, diff-check, and exact 164/164 inventory reconciliation.
