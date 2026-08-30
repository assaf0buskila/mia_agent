# Gates: Minimality audit and bounded refactor

Scope: Identify what to remove, rewrite, split, or retain; implement only high-confidence simplifications that preserve behavior.

- [x] G1: Every large live Python file has a KEEP, REMOVE, SPLIT, or REWRITE disposition with evidence and dependency impact.
  EVIDENCE: `gates/evidence/minimality-audit.md` inventories all 22 live Python files at or above 500 physical lines, with concrete symbols, callers/tests, and dependency impact.
- [x] G2: At least one high-confidence complexity reduction is implemented with a measurable before/after and tests, unless the audit proves no safe reduction exists.
  EVIDENCE: duplicated 103-line sales-mirror blocks were replaced by `mirror_sales_turn`; owned production source fell 2,863 -> 2,793 lines (-70), `process_inbound_texts` 584 -> 494 AST lines, and `process_website_message` 356 -> 266. Focused gate: 99 passed.
- [x] G3: No new technology, database, runtime agent, or ambient authority is introduced.
  EVIDENCE: the change is one deterministic helper over the existing `LeadStore`, `SheetsPort`, risk, idempotency, and tool-outcome paths. Ruff passed on all owned Python files; no package, graph, principal, model, provider, schema, or deployment file changed.
