# Gates: Fresh HEAVY review and final regression

Scope: Independently challenge correctness, security, simplification, and the claimed completion evidence.

- [x] G1: Fresh reviewer reports no unresolved P0/P1/P2 findings.
  EVIDENCE: Second independent HEAVY rereview passed after two bounded remediation checks. Exact real-handler Hebrew inventory/income collisions and the prior English read-style collision were denied before FakeSheetsPort; explicit Hebrew/English mutations remained authorized. The complete review found no unresolved P0/P1/P2. See `gates/evidence/final-heavy-rereview.md`.
- [x] G2: Full test suite passes with environment-only exclusions explicitly justified.
  CHECK: powershell -NoProfile -ExecutionPolicy Bypass -Command "uv --offline --cache-dir .uv-cache run pytest -q -p no:cacheprovider --basetemp .pytest-tmp/full-final-4"
  EXPECT: /passed/
  EVIDENCE: Final post-remediation current-tree run exited 0 on 2026-08-28 with 2,375 tests collected. The process-scoped PowerShell bypass was required for three deployment-script tests and made no permanent policy change; the workspace-owned basetemp prevented Windows temp-directory setup failures.
- [x] G3: Deterministic eval suites and origin-binding assertion pass.
  EVIDENCE: `scripts/eval_diff.py` reported 0 failures across sales 51, buyer 43, website handoff 15, safety 20, objection 20, extract 30, writing 33, and gold 21. Routing eval passed 20/20 through the full suite. `scripts/assert_origin_bind.py` reported `origin-bind: ok`; full Ruff reported `All checks passed!`.
- [x] G4: Root ledger contains evidence for every user requirement and no pending item is presented as done.
  EVIDENCE: Root G2 and G3 remain explicitly pending for live Telegram/provider/AWS/deployment proof; completed root items cite local evidence and do not claim those live blockers are closed.
