# Gates: API, channels, graphs, and services function audit

Scope: Read-only audit of the 23 function-bearing files under `app/api`, `app/channels`, `app/agents`, `app/graph`, `app/services`, plus `app/main.py`.

- [x] G1: All 23 assigned files are opened and listed once in `gates/evidence/function-audit-api.md`.
  EVIDENCE: `gates/evidence/function-audit-api.md` records the exact 23-file inventory and 133 definition lines; every assigned file was opened in full.
- [x] G2: Every assigned file receives KEEP, SIMPLIFY, MERGE, or REMOVE with concrete symbols, callers/tests, risk, and benefit.
  EVIDENCE: the per-file table in `gates/evidence/function-audit-api.md` has exactly one disposition row for each inventory entry, followed by ranked bounded findings.
- [x] G3: Findings explicitly protect thin channels, two graphs, one runtime owner agent, principal propagation, idempotency, and current website/Telegram contracts.
  EVIDENCE: the ranked findings harden edge-minted principals and claim/retry behavior; the explicit no-change section preserves both graphs, thin channels, the single owner loop, voice/callbacks, and the published website surface.
- [x] G4: No code, test, product doc, deployment file, or shared gate is edited by the audit leaf.
  EVIDENCE: this leaf changed only `gates/evidence/function-audit-api.md` and `gates/leaf-1.5.1-function-audit-api.md`; the pre-existing dirty worktree was preserved.
