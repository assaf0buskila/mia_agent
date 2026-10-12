# Shared runtime status — 2026-10-12

## Result

The shared Google Sheets/Apps Script runtime is implemented and deployed to the existing workbook. The real edit trigger is authorized and an actual browser Sheet edit has been accepted with a system-generated ID, revision and source. Existing MIA is deployed and the shared connection is enabled. Cloudflare serves the reviewed product assets; its upstream connection fix passed exact-revision CI, was deployed and returned a real authentication challenge successfully. See `LIVE_STATUS.md` for current evidence and remaining acceptance checks.

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

- `node --test tests/assaf-os/sheet-core.test.js tests/assaf-os/runtime.test.js tests/assaf-os/apps-script-runtime.test.js`: **PASS — 64 passed, 0 failed** at backend `ad1918345b17db83e2a24c6f387dc14feb0f5dee`.
- Node syntax checks for `schema.js`, `core.js`, `runtime.js`, `UnifiedSheets.gs`, and `UnifiedRuntime.gs`: **PASS**.
- Test data is synthetic. Nineteen integration-style cases load the actual `.gs` operation dispatcher in a Node VM with Google-shaped Sheet, timezone-aware Date, formula, grid and Advanced Sheets batch mocks.

## Boundaries and unknowns

- **PASS:** existing Apps Script deployment version 6, Advanced Sheets service, edit-trigger authorization, actual native task acceptance, signed schema/snapshot and invalid-signature rejection.
- **PENDING:** live Cloudflare owner sign-in, OS write/read acceptance and actual human Telegram approval through MIA.
- **UNKNOWN:** physical owner/device acceptance.
- Script locks do not exclude human workbook edits. Fingerprint conflicts surface for owner review; there is no cross-channel atomic lock.
- Apps Script installation itself is not transactional. Preflight rejects known tab conflicts first. If a provider failure leaves exact-schema empty runtime tabs, it returns explicit partial-install diagnostics for operator inspection and manual cleanup; it does not overwrite or delete them.
- Receipt recovery prevents automatic replay after an ambiguous approval result. A missing receipt remains unknown and must block a new write attempt.
- The original local foundation stage performed no external mutations. The later owner-authorized deployment and cleanup are recorded in `LIVE_STATUS.md` and `CLEANUP_REPORT.md`; no real customer or financial test data has been inserted.
