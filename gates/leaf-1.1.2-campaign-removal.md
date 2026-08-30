# Gates: Finish ADR-039 campaign removal

Scope: Remove residual campaign, pacing, prelaunch, and paid-analytics behavior already rejected by ADR-039 while preserving generic safety infrastructure.

- [x] G1: Owner routing no longer exposes or misroutes campaign-spend analytics.
  CHECK: rg -n "ACTION_CAMPAIGN_WRITE|RESOURCE_CAMPAIGN|TRIGGER_SPEND_THRESHOLD|build_campaign_recommendation_event|CAMPAIGN_RECOMMENDATION|campaign_pacing|campaign_prelaunch|campaign_recommendation|apply_campaign_write_approval|list_owner_tasks_by_trigger" app --glob '!app/core/capabilities.py'
  EXPECT: /^$/
  EVIDENCE: 2026-08-28: no matches. `tests/unit/test_owner_tasks.py` proves campaign spend/pause are NOTE + unavailable, while Instagram content remains ANALYTICS.
- [x] G2: Dead campaign/pacing/prelaunch persistence and mirror code is removed or explicitly retained with a current use proved by tests.
  EVIDENCE: 2026-08-28: removed campaign ORM rows/store accessors, campaign approval persistence, events, briefs/weeklies fields, due-scan threshold logic, and Sheets budget/performance mirror. Retained UTM campaign and meta_campaign_id attribution fields only; they remain lead-source evidence.
- [x] G3: Owner routing, commitments, due-scan, and capability tests pass.
  CHECK: uv --cache-dir .uv-cache run pytest -q tests/unit/test_owner_tasks.py tests/unit/test_commitments.py tests/unit/test_due_scan_worker.py tests/unit/test_approvals.py tests/unit/test_events.py tests/unit/test_adversarial_identity.py tests/unit/test_content_ideas.py tests/unit/test_lead_reviews.py tests/unit/test_owner_briefs.py tests/unit/test_owner_calendar.py tests/unit/test_owner_distinct_replies.py tests/unit/test_owner_weeklies.py tests/unit/test_webpage_scrape_adversarial.py tests/unit/test_seo_scrape_adversarial.py tests/e2e/test_preprod_stories.py -p no:cacheprovider
  EXPECT: /passed/
  EVIDENCE: 2026-08-28 fresh HEAVY rerun: 309 passed. The old test_owner_inbound.py gate target did not exist; this replacement runs every test in all affected owner, commitment, due-scan, approval, event, adversarial, and preprod-story files.

- [x] G4: Cleanup retains the HEAD non-campaign test contract and passes static checks.
  CHECK: uv --cache-dir .uv-cache run pytest --collect-only tests/unit/test_owner_tasks.py tests/unit/test_commitments.py tests/unit/test_due_scan_worker.py tests/unit/test_approvals.py tests/unit/test/events.py tests/unit/test_adversarial_identity.py tests/unit/test_content_ideas.py tests/unit/test_lead_reviews.py tests/unit/test_owner_briefs.py tests/unit/test_owner_calendar.py tests/unit/test_owner_distinct_replies.py tests/unit/test_owner_weeklies.py tests/unit/test_webpage_scrape_adversarial.py tests/unit/test_seo_scrape_adversarial.py tests/e2e/test_preprod_stories.py -p no:cacheprovider
  EXPECT: /309 tests collected/
  EVIDENCE: The four previously collapsed files now collect 113 tests: approvals 28, commitments 24, due-scan 8, owner tasks 53. Every test removed from their HEAD versions is tied to campaign approval, paid analytics, spend-threshold, pacing, or prelaunch behavior; explicit negative routing/approval tests replace the deleted paid-campaign expectations. `uv --cache-dir .uv-cache run ruff check app tests` and `git diff --check` both passed.

- [x] G5: Full-suite ADR-039 inventory contracts contain no stale campaign expectations.
  CHECK: uv --cache-dir .uv-cache run pytest -q tests/unit/test_evals.py tests/unit/test_execution_policy.py tests/unit/test_task_classes.py -p no:cacheprovider
  EXPECT: /passed/
  EVIDENCE: 2026-08-28 fresh HEAVY rerun: 80 passed. Routing remains exactly 20 unique supported cases and `run_routing_eval` reports 20 passed, 0 failed. Meta Ads is asserted unsupported and fail-safe (`HUMAN_ONLY`, R5, approval-required, fail-closed), and the task-class inventory contains only current non-model classes.
