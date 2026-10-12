# Foundation status — 2026-10-12

## Result

Local foundation is implemented and **not deployed**. It defines one canonical Hebrew workbook schema, pure financial/time snapshot calculations, lead-to-client promotion semantics, and an explicit operator-only installer.

## Evidence

- `node --test tests/assaf-os/sheet-core.test.js`: **PASS — 23 passed, 0 failed**.
- `node --check ops/assaf-os/schema.js`: **PASS**.
- `node --check ops/assaf-os/core.js`: **PASS**.
- `git diff --check`: **PASS for this slice**; the command emitted only an unrelated existing `uv.lock` line-ending warning.
- Independent HEAVY review first found four issues (protection permissions, misleading fixed ₪ formatting, fractional-minute validation, unsafe hourly overflow). All four were fixed. Re-review: **PASS**, including independent mock probes of protection, validation, and number formatting.

No test contacted Google or changed a workbook. No trigger, web endpoint, external send, secret, deployment, commit, or push was created.

## Known limitations / gates

- Google Sheets runtime behavior is **UNKNOWN** until an operator explicitly runs preflight and a controlled install against the exact intended workbook ID.
- The installer is fail-closed when a target tab already exists and never overwrites existing tabs or data. Apps Script has no cross-sheet transaction, so an unexpected provider failure during first installation can leave a partial set of newly created empty tabs; rerun then refuses until an operator inspects the workbook.
- `לקוחות` and `עבודות` now contain explicit protected calculated rollup columns, including ILS-only money labels and foreign-currency flags. `core.js` calculates their source values, but no Sheet formula or synchronizer populates those cells yet. `סיכום` is likewise an empty protected calculated projection.
- Direct edits are not yet synchronized with OS/MIA: IDs, revisions, immutable fields, conflict handling, approval receipts, and edit-trigger reconciliation remain to be implemented and tested.
- The schema records `write_source` and `request_id`, while `core.js` provides promotion replay semantics. There is no write API. Any future MIA write still requires an exact, immutable, expiring approved proposal and target revalidation.
- Raw-cell drift remains fail-closed. No migration or compatibility claim is made for the legacy ledger.
- Ads platform usage is retained as source data only and is excluded from paid expense totals. Agreed work value is never counted as received income.
- Multi-currency rows stay separated. Shared costs stay unallocated.

