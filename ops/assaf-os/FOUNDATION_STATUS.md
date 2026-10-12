# Shared runtime status — 2026-10-12

## Result

The local shared Google Sheets/Apps Script runtime is implemented. It remains undeployed and has not contacted or changed Google, Cloudflare, MIA production, or the real workbook.

## Implemented

- strict inbound and outbound HMAC envelopes with pinned numeric owner, workspace, workbook, and `TEST`/`LIVE` mode;
- server-bound `OS`, `MIA`, and decision-only `TELEGRAM_APPROVAL` actor contexts;
- schema, snapshot, proposal, proposal lookup, approval, rejection, and receipt operations;
- immutable ten-minute proposals with stable browser/Telegram decision IDs, revision/raw target and dependency revalidation, payload-bound cross-channel replay claims, and durable receipts;
- one Advanced Sheets batch for field-limited approved business changes, accepted journal, protected calculations, proposal state, replay record, and receipt;
- input-only schema validation, exact minor/native money conversion, complete original/refund graph constraints, expense source deduplication, stable-person lead promotion, and row/size bounds;
- installed-edit-trigger and coverage-aware snapshot-read reconciliation for complete native edits, with exact header/formula checks, `Asia/Jerusalem` Date normalization, validated in-memory ID allocation, shared approval/read/trigger semantic projection gates, nullable unsafe calculations, field-limited system writes, revisions, accepted snapshots, and visible issues;
- monthly paid expenses/net cash and per-paid-work time evidence, with known minutes retained but unsafe hourly rates null when any paid work lacks time;
- additive compatibility for the existing exact two-column Hebrew `הגדרות` sheet while preserving all other tabs/data;
- no-argument configured preflight/install wrappers, non-secret `TEST`/`LIVE` binding configuration, and explicit partial-install diagnostics without automatic cleanup or overwrite.

## Local verification

- `node --test tests/assaf-os/sheet-core.test.js tests/assaf-os/runtime.test.js tests/assaf-os/apps-script-runtime.test.js`: **PASS — 52 passed, 0 failed**.
- Node syntax checks for `schema.js`, `core.js`, `runtime.js`, `UnifiedSheets.gs`, and `UnifiedRuntime.gs`: **PASS**.
- Test data is synthetic. Nineteen integration-style cases load the actual `.gs` operation dispatcher in a Node VM with Google-shaped Sheet, timezone-aware Date, formula, grid and Advanced Sheets batch mocks.

## Boundaries and unknowns

- **UNKNOWN:** real Apps Script deployment, Advanced Sheets permissions, edit-trigger authorization, and real-workbook behavior.
- **UNKNOWN:** final Cloudflare owner product and MIA/Telegram end-to-end integration until their separate changes are integrated and exercised.
- **UNKNOWN:** physical owner/device acceptance.
- Script locks do not exclude human workbook edits. Fingerprint conflicts surface for owner review; there is no cross-channel atomic lock.
- Apps Script installation itself is not transactional. Preflight rejects known tab conflicts first. If a provider failure leaves exact-schema empty runtime tabs, it returns explicit partial-install diagnostics for operator inspection and manual cleanup; it does not overwrite or delete them.
- Receipt recovery prevents automatic replay after an ambiguous approval result. A missing receipt remains unknown and must block a new write attempt.
- No external send, customer outreach, secret write, deployment, trigger installation, commit, or push occurred in this implementation task.
