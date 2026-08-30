# Gates: Dead surface, duplicate helpers, and operator-script cleanup

Scope: Remove only proven-unreferenced symbols and rejected discovery remnants; consolidate exact private duplication; tighten release scripts; make eval diff complete.

- [x] G1: The four domain/brain dead symbols, duplicate unused event counter, and rejected Sheets name-discovery helpers/tests are removed after fresh reference searches.
  EVIDENCE: `gates/evidence/function-cleanup-mechanical.md` (measured cleanup and fresh reference searches)
- [x] G2: Eight identical owner-task phrase helpers become one behavior-equivalent private matcher; Search Console duplicate fallback expressions are removed.
  EVIDENCE: `gates/evidence/function-cleanup-mechanical.md` (measured cleanup)
- [x] G3: `eval_diff` includes calendar and routing; ECS revision cannot inject plaintext env; migration runner is pinned to `mia-migrate`.
  EVIDENCE: `gates/evidence/function-cleanup-mechanical.md` (operator-script assertions)
- [x] G4: Focused domain, brain, Sheets, Search Console, eval, and script tests pass with Ruff and diff-check.
  EVIDENCE: `gates/evidence/function-cleanup-mechanical.md` (four verification passes)
