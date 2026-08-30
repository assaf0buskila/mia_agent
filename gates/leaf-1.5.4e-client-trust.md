# Gates: Client authority, single retrieval path, and health truth

Scope: Require request-derived client principals, remove the test-only legacy retrieval hook, and report ADR-042 owner Sheets readiness without breaking existing health keys.

- [x] G1: ClientGraph requires a caller-minted `Principal.client`; website, prospect inbound, and due-scan callers pass it explicitly.
  EVIDENCE: `compile_client_graph(..., *, principal: Principal)` has no default and rejects owner principals; website/inbound pass request-minted client principals and due-scan mints/passes its client principal explicitly.
- [x] G2: The inner sales graph has no `knowledge_lookup` hook; ClientGraph is the single knowledge capability owner and kill-switch behavior remains proven there.
  EVIDENCE: `rg knowledge_lookup app/graph` returned no matches; `retrieve_knowledge` returns before capability execution when killed; focused visitor-knowledge tests pass.
- [x] G3: `/health` retains compatibility keys and adds truthful configuration readiness for owner Sheets read/update/append without live provider calls.
  EVIDENCE: `owner_integrations` retains mirror-ID semantics for `sheets_mirror` and derives ADR-042 read/update/append from Composio plus `allowed_sheets_spreadsheet_ids()`; health tests cover absent and allowlist-only configuration without provider calls.
- [x] G4: Focused client graph, website, inbound, principal, visitor-knowledge, health, and due-scan tests pass with Ruff and diff-check.
  EVIDENCE: 2026-08-28 repair: `has_website_prospect_message` and inactivity aggregation now bind both lead and conversation/session, so an empty returning session cannot borrow another session's message. Real LeadStore regressions prove no inbox row, recipient claim, or transport call for that empty session; a messaged session remains eligible. Focused finalization/due suite: 29 passed; broad finalization/due/handoff/client/migration suite: 133 passed; Ruff owned paths and `git diff --check` passed.
