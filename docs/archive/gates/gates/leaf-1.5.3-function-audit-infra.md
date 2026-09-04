# Gates: Capabilities, core, database, integrations, workers, and scripts function audit

Scope: Read-only audit of the 68 remaining function-bearing files under `app/capabilities`, `app/core`, `app/db`, `app/evals`, `app/integrations`, `app/tools`, `app/workers`, and `scripts`.

- [x] G1: All 68 assigned files are opened and listed once in `gates/evidence/function-audit-infra.md`.
  EVIDENCE: The current-tree function-bearing inventory was rebuilt for the eight assigned directory groups, every file was opened in full, and the numbered per-file matrix in `gates/evidence/function-audit-infra.md` contains exactly 68 unique rows.
- [x] G2: Every assigned file receives KEEP, SIMPLIFY, MERGE, or REMOVE with concrete symbols, callers/tests, risk, and benefit.
  EVIDENCE: All 68 matrix rows contain a disposition plus named symbols, direct caller/test evidence, behavior risk, and expected benefit. Eight bounded `SIMPLIFY` findings are ranked; the audit explicitly finds no justified whole-file `MERGE` or `REMOVE`.
- [x] G3: Findings protect typed ports, provider pins, capability policy, secret boundaries, migration/deployment safety, and deterministic evaluation contracts.
  EVIDENCE: The ranked findings tighten a typed async port, notification retry behavior, ECS secret/command scope, eval-family completeness, and dead exact duplicates. The no-change section explicitly retains provider pins, capability/risk/approval policy, secret redaction, migration state, explicit resource IDs, disabled WhatsApp inbound, and deterministic eval semantics.
- [x] G4: No code, test, product doc, deployment file, or shared gate is edited by the audit leaf.
  EVIDENCE: This leaf edited only `gates/evidence/function-audit-infra.md` and `gates/leaf-1.5.3-function-audit-infra.md`. No recommendation was implemented, no live provider/database/deployment/probe was invoked, and no secret value or `.env` content was inspected.
