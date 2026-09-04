# Fresh HEAVY review: ADR-039 campaign removal

Date: 2026-08-28

Outcome: PASS after repair. The original cleanup was not acceptable because four broad test files had been replaced by token smoke tests. Non-campaign coverage is restored and the remaining deletions are confined to ADR-039 paid-campaign, pacing, prelaunch, and LinkedIn-post-analytics behavior.

## Collateral repaired

- Restored the HEAD non-campaign tests in `tests/unit/test_approvals.py`, `tests/unit/test_commitments.py`, `tests/unit/test_due_scan_worker.py`, and `tests/unit/test_owner_tasks.py`.
- The restored files now collect 113 tests instead of 12 smoke tests: approvals 28, commitments 24, due-scan 8, owner tasks 53.
- Preserved generic approval payload hashes, expiry, resource binding, approval ids, first-write-wins behavior, canonical-event idempotency, and lead approval decisions.
- Preserved deterministic due-date parsing in Hebrew and English, conditional sales follow-ups, due-scan persistence, reminder deduplication, kill-switch behavior, CLI counts, and the no-customer-send boundary.
- Preserved owner routing in Hebrew and English, adversarial owner identity, voice/text separation, Gmail/calendar/research/lead/website routing, organic Instagram analytics, and owner task idempotency.
- Replaced the deleted paid-campaign positive expectations with explicit negative guards: campaign spend and pause requests are unavailable and cannot queue an approval; organic Instagram analytics remains live.
- Reworked the generic multi-type routing test to use two live capabilities (`research` plus `sales`) rather than deleted ads behavior.
- Replaced the stale `campaign_write` stub in the pending-approvals formatter test with the live generic `website_edit` approval resource.
- Removed `LeadStore.list_owner_tasks_by_trigger`, a dangling campaign-only method that referenced the deleted `ALLOWLISTED_OWNER_TASK_LIST_TRIGGERS` constant and would have raised `NameError` if called.

## Production hunk classification

| File | Removed/changed hunk classification |
| --- | --- |
| `app/db/models.py` | Deliberate: campaign recommendation, pacing, performance, prelaunch ORM rows and pacing/prelaunch brief columns only. |
| `app/db/store.py` | Deliberate: campaign CRUD, campaign approval persistence/decision, pacing/prelaunch persistence, and brief/weekly campaign fields. Preserved generic approval/store methods. The switch from the removed pacing event allowlist to `COUNTABLE_EVENT_TYPES` preserves current daily/weekly KPI counting. Removed the dangling campaign trigger-list method during this review. |
| `app/domain/approvals.py` | Deliberate: campaign ids, intents, resource/action constants, queue/decision branch, and campaign acknowledgments. Generic lead, website, and Gmail approval hashing, expiry, binding, and decision paths remain. |
| `app/domain/commitments.py` | Deliberate: `spend_threshold`, spend/budget evaluation, and spend-trigger scanning. Due-date and conditional sales follow-up behavior remains. |
| `app/domain/events.py` | Deliberate: campaign recommendation enum/builder and campaign-write provider-event special case. Other canonical events and approval-required payloads remain. |
| `app/domain/owner_briefs.py` | Deliberate: campaign pacing/prelaunch fields, lookup, validation, formatting, and persistence arguments only. Lead, meeting, handoff, message, follow-up, engine-health, and cancellation metrics remain. |
| `app/domain/owner_weeklies.py` | Deliberate: campaign pacing/prelaunch fields, lookup, validation, formatting, and persistence arguments only. Weekly sales and operations KPIs remain. |
| `app/domain/owner_tasks.py` | Deliberate: paid-campaign analytics keywords, campaign approval routing, and spend-threshold acknowledgments. Organic Instagram analytics and all non-campaign routing remain. |
| `app/domain/owner_reads.py` | Deliberate comment correction only; generic resource fallback for non-lead approvals remains. |
| `app/domain/policies/execution_policy.py` | Deliberate removal of Meta ads, campaign analysis, pacing, and prelaunch policy pins. |
| `app/domain/policies/task_classes.py` | Deliberate removal of campaign interpretation task-class pin. |
| `app/workers/due_scan.py` | Deliberate removal of unused paid-spend inputs; date-based owner tasks, prospect follow-ups, website inactivity finalization, and owner reminders remain. |
| `app/evals/datasets/routing_v1.json` | Deliberate removal of the campaign-pause approval case only. |
| `app/integrations/sheets.py` | Campaign budget/performance mirror hunks are deliberate. The concurrent ADR-042 owner Sheets implementation was audited as out of this cleanup's edit boundary and was not modified here. |

## Test hunk classification

| File(s) | Classification |
| --- | --- |
| `tests/unit/test_approvals.py` | Restored all generic tests. Remaining deleted tests exercise campaign approval rows, campaign expiry/tamper, campaign resource isolation, or campaign idempotency only. |
| `tests/unit/test_commitments.py` | Restored all due-date, condition, sales follow-up, invalid-timezone, and no-message-port tests. Remaining deletions are paid analytics/spend-threshold only. |
| `tests/unit/test_due_scan_worker.py` | Restored all generic worker, reminder, kill-switch, CLI, and capability-aliveness tests. Remaining deletion is the campaign-pacing spend-threshold story only. |
| `tests/unit/test_owner_tasks.py` | Restored all non-campaign classifier, persistence, voice/text, owner/prospect, calendar, Gmail, and idempotency tests. Remaining deletions are campaign budget/pause/spend-threshold expectations only. |
| `tests/unit/test_events.py` | Deliberate removal of campaign recommendation builder tests only. |
| `tests/e2e/test_preprod_stories.py` | Deliberate removal of the campaign approval persistence segment. The generic high-risk feature flags/tool boundary and all other preprod stories remain. |
| `tests/unit/test_adversarial_identity.py`, `tests/unit/test_content_ideas.py`, `tests/unit/test_lead_reviews.py`, `tests/unit/test_owner_briefs.py`, `tests/unit/test_owner_calendar.py`, `tests/unit/test_owner_distinct_replies.py`, `tests/unit/test_owner_weeklies.py`, `tests/unit/test_webpage_scrape_adversarial.py` | Behavior-preserving substitutions from paid-campaign analytics to organic Instagram analytics, generic approval events, or live website approval resources. No test was collapsed. |

## Deliberate behavior still removed

- Campaign approval queue/approve/reject/resource binding.
- Meta paid-campaign spend/budget routing and acknowledgments.
- Spend-threshold commitments and due-scan evaluation.
- Campaign recommendation canonical events.
- Campaign recommendation, pacing, performance, and prelaunch persistence.
- Campaign pacing/prelaunch fields in owner daily and weekly briefs.
- Campaign budget/performance Sheets mirrors.
- Campaign execution-policy and task-class pins.

Attribution fields such as UTM campaign and `meta_campaign_id` remain because they describe lead-source evidence, not an ads-management capability.

## Verification evidence

1. Full affected-file run:
   `uv --cache-dir .uv-cache run pytest -q tests/unit/test_owner_tasks.py tests/unit/test_commitments.py tests/unit/test_due_scan_worker.py tests/unit/test_approvals.py tests/unit/test_events.py tests/unit/test_adversarial_identity.py tests/unit/test_content_ideas.py tests/unit/test_lead_reviews.py tests/unit/test_owner_briefs.py tests/unit/test_owner_calendar.py tests/unit/test_owner_distinct_replies.py tests/unit/test_owner_weeklies.py tests/unit/test_webpage_scrape_adversarial.py tests/unit/test_seo_scrape_adversarial.py tests/e2e/test_preprod_stories.py -p no:cacheprovider --basetemp .pytest-tmp/campaign-heavy-final`
   Result: PASS, 309 tests.
2. Collection audit of the same complete file list.
   Result: `309 tests collected`; restored broad-file counts are approvals 28, commitments 24, due-scan 8, owner tasks 53.
3. `uv --cache-dir .uv-cache run ruff check app tests`
   Result: `All checks passed!`
4. `git diff --check`
   Result: PASS (no whitespace errors; Git emitted only Windows LF/CRLF conversion warnings).
5. Residual-symbol search from gate G1.
   Result: no matches under `app/` outside the intentionally retained capability-spec registry exception.

## Post-review full-suite drift repair

The later full-suite gate surfaced three stale ADR-039 inventory assumptions. They were repaired without changing production policy code:

- Replaced the deleted campaign-pause routing case with `rt_sales_follow_up_en`, a supported deterministic sales follow-up. The routing dataset remains exactly 20 unique, PII-safe cases; direct harness result is 20 passed, 0 failed.
- Updated the Meta Ads execution-policy test to assert the accepted unsupported fail-safe contract: `HUMAN_ONLY`, R5, approval-required, fail-closed, and full confidence.
- Removed the stale `CAMPAIGN_INTERPRETATION` enum reference from the task-class test. The replacement asserts the exact current set of `model_source="none"` task classes and separately verifies the code-owned subset.

Verification: `uv --cache-dir .uv-cache run pytest -q tests/unit/test_evals.py tests/unit/test_execution_policy.py tests/unit/test_task_classes.py -p no:cacheprovider --basetemp .pytest-tmp/campaign-drift-focused` passed all 80 tests. The routing harness reported `{'cases': 20, 'unique_ids': 20, 'passed': 20, 'failed': 0}`.

No `.env` file or secret value was inspected. No live API was called. No active GSC, GA4, LinkedIn, owner-tools, config, or Sheets implementation file was edited by this review.
