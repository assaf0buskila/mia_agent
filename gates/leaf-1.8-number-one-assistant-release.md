# Gates: Mia number-one-assistant release

Scope: Complete, independently verify, merge, and deploy the owner-capability, managed-CRM, Instagram, and website-handoff package.

- [x] G1: The configured Mia spreadsheet is self-maintaining off the request path and every represented business-domain movement has an idempotent projection or activity record.
  CHECK: .venv\Scripts\pytest.exe -p no:cacheprovider --basetemp .pytest-tmp/gate18-sheets tests/unit/test_sheets.py tests/unit/test_owner_sheets.py tests/unit/test_due_scan_worker.py
  EXPECT: /[0-9]+ passed/
  EVIDENCE: -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html | 98 passed, 56 warnings in 2.61s

- [x] G2: Gmail, Calendar, and LinkedIn exact approval writes are durable before provider execution and ambiguous outcomes cannot auto-replay.
  CHECK: .venv\Scripts\pytest.exe -p no:cacheprovider --basetemp .pytest-tmp/gate18-writes tests/unit/test_approved_write_crash_safety.py tests/unit/test_owner_calendar_writes.py
  EXPECT: /[0-9]+ passed/
  EVIDENCE: -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html | 5 passed, 2 warnings in 0.95s

- [x] G3: Website WhatsApp handoff notification and bounded Instagram analytics behavior pass their regression suites.
  CHECK: .venv\Scripts\pytest.exe -p no:cacheprovider --basetemp .pytest-tmp/gate18-edge tests/unit/test_website_handoff_owner_notify.py tests/unit/test_instagram_insights.py tests/unit/test_owner_live_tools.py
  EXPECT: /[0-9]+ passed/
  EVIDENCE: -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html | 76 passed, 20 warnings in 4.40s

- [x] G4: The complete current tree passes whole-tree Ruff, full pytest, and diff-check without weakening tests.
  EVIDENCE: Final parent rerun after all four blocker repairs: 2,675 passed with zero failures in 77.07s; `ruff check app tests` reported All checks passed; `git diff --check` exited 0; origin-bind reported ok. An earlier suite-order-sensitive notification assertion failed after the functional behavior passed; it was replaced with an exact-row non-consumption assertion, then 31 focused owner-audit tests and the full suite passed.

- [x] G5: A fresh HEAVY reviewer reports no unresolved release-blocking P0/P1/P2 issue, including the previously identified blocker classes.
  EVIDENCE: Post-repair fresh HEAVY reviewer verdict `PASS`; 25 reviewer-focused tests passed. The review traced exact-turn LinkedIn approval IDs through ToolResult, AgentOutcome, OwnerBrainResult, OwnerGraph, and Telegram markup; verified an unrelated newer approval cannot hijack the button; verified PostgreSQL `resource_id` widening for 43/44-character IDs; and rechecked Instagram terminal fallback, read-only audit notifications, crash safety, managed CRM maintenance, and website notification idempotency.

- [ ] G6: The exact reviewed commit is present on the fetched target branch after push and merge.
  EVIDENCE: pending

- [ ] G7: Production runs the exact released image/task revision with healthy sanitized configuration, Gmail approval-send enabled, and CRM maintenance completed.
  EVIDENCE: pending
