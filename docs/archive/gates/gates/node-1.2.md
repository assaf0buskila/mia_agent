# Gates: Owner Telegram capabilities

Scope: Voice and requested Google/LinkedIn reads work through the production-shaped owner path.

- [x] G1: Telegram voice leaf is verified.
  EVIDENCE: Parent reran the production-shaped and existing Telegram/STT regression set; 90 tests passed. Leaf ledger is 3/3 met.
- [x] G2: Google and LinkedIn integration leaf is verified against the expanded Sheets read/write and AssafWeb KPI contract.
  EVIDENCE: Parent inspected the fail-closed allowlist, RAW literal-write, normalized KPI, policy, and idempotency paths, then reran 146 focused tests successfully on 2026-08-28. Live provider proof remains isolated in node 1.4.
- [x] G3: Combined owner graph and Telegram tests pass.
  CHECK: uv run pytest -q tests/unit/test_telegram_owner_graph.py tests/unit/test_vnext_owner_voice.py
  EXPECT: /passed/
  EVIDENCE: Covered inside the parent 90-test Telegram/STT regression run on 2026-08-28; all passed.
