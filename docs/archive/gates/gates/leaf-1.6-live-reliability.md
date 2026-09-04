# Gates: Live owner reliability closure

Scope: Close the current production gaps for Telegram voice, website-to-WhatsApp owner notification, Composio reads, and the exact allowlisted Google Sheet without adding a runtime agent.

- [ ] G1: Telegram voice failures are content-free and deduplicated, and a fresh live owner voice note returns one text reply through OwnerGraph.
  CHECK: uv --offline --cache-dir .uv-cache run pytest -q -p no:cacheprovider tests/unit/test_telegram.py tests/unit/test_vnext_owner_voice.py tests/unit/test_transcribe.py
  EXPECT: /passed/
  EVIDENCE: Local failure/retry, commit-before-reply, one-OwnerGraph, and text-only regressions pass in the frozen 2,577-test tree and final HEAVY review. The fresh live owner voice note is still pending.

- [ ] G2: A real website WhatsApp CTA click yields exactly one truthful Telegram owner notification, while pre-click and retry behavior cannot create false or duplicate claims.
  CHECK: uv --offline --cache-dir .uv-cache run pytest -q -p no:cacheprovider tests/unit/test_website.py tests/unit/test_website_handoff_brief.py tests/unit/test_ask_mia_widget.py tests/unit/test_vnext_finalization.py
  EXPECT: /passed/
  EVIDENCE: Local hot-HANDOFF/CTA ordering, forced claim race, legacy/global/per-recipient compatibility, partial rejection, pre-send commit failure, and post-rejection commit recovery pass in the frozen tree and final HEAVY review. Browser/Telegram live acceptance is still pending.

- [ ] G3: Composio identity and no-data schemas are repaired; exact-Sheet read plus Gmail, Calendar, LinkedIn, GA4, and Search Console read probes succeed without exposing credentials.
  CHECK: uv --offline --cache-dir .uv-cache run pytest -q -p no:cacheprovider tests/unit/test_sheets.py tests/unit/test_ga4.py tests/unit/test_search_console.py tests/unit/test_linkedin.py
  EXPECT: /passed/
  EVIDENCE: `gates/evidence/phase16-live-predeploy.md` records the sanitized pre-deploy identity repair, exact-Sheet read, Gmail/Calendar/LinkedIn reads, and completed-28-day GA4/GSC provider successes. Repaired short-window adapters still require the post-deploy re-probe, so this gate remains open.

- [x] G4: The complete current tree passes full pytest, Ruff, diff-check, and a fresh HEAVY review with no unresolved P0/P1/P2 findings.
  CHECK: uv --offline --cache-dir .uv-cache run ruff check app tests
  EXPECT: All checks passed
  EVIDENCE: `gates/evidence/phase16-local-release.md`: 2,577 tests, Ruff, JS syntax, origin bind, 273/273 deterministic evals, diff-check, and final independent RELEASE PASS with P0/P1/P2 all zero.

- [ ] G5: One exact-SHA release is deployed stable with rollback enabled, and public health plus live acceptance probes pass.
  EVIDENCE: pending

- [ ] G6: The exact Sheet `1HW8mnc9GFXraS6oG5VIxFcJvZq9gMDJBFRxY2mpVOhI` is the production allowlisted target; read is proven and any write occurs only from a fresh authenticated owner Telegram request.
  EVIDENCE: `gates/evidence/phase16-live-predeploy.md` records production ID equality and the bounded adapter read with no cell content. Write remains pending an authenticated owner Telegram request.
