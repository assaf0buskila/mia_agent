# Gates: Domain and brain function audit

Scope: Read-only audit of the 73 function-bearing files under `app/domain` and `app/brain`.

- [x] G1: All 73 assigned files are opened and listed once in `gates/evidence/function-audit-domain.md`.
  EVIDENCE: Independent `rg` inventory reproduced 73 files (8 brain + 65 domain); the evidence table contains 73 unique numbered rows and records 15,217 current lines.
- [x] G2: Every assigned file receives KEEP, SIMPLIFY, MERGE, or REMOVE with concrete symbols, callers/tests, risk, and benefit.
  EVIDENCE: `gates/evidence/function-audit-domain.md` records 64 KEEP + 9 SIMPLIFY + 0 MERGE + 0 REMOVE, with named symbols, direct/indirect caller and test evidence, risk, and expected benefit per row.
- [x] G3: Findings distinguish deliberate deterministic policy/lexicons from accidental duplication and preserve Postgres ownership, safety, and accepted customer/owner behavior.
  EVIDENCE: Ranked findings isolate approval validation drift, invalid JSON truncation, cancellation claim completion, four dead symbols, and identical private phrase helpers; explicit no-change conclusions retain Postgres, deterministic lexicons/NBA, typed safety registries, provider-write verification, and accepted channel behavior.
- [x] G4: No code, test, product doc, deployment file, or shared gate is edited by the audit leaf.
  EVIDENCE: This leaf's write operations were limited to `gates/evidence/function-audit-domain.md` and this leaf gate. No application, test, product, deployment, PLAN, root/node/shared gate, or other evidence file was written.
