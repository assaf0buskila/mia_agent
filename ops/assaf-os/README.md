# Assaf OS — unified Sheet foundation

This directory contains a local, undeployed foundation for one dedicated Google Sheets workbook:

- `schema.js` is the canonical Hebrew schema shared by Node and Apps Script. It marks every field as user input, calculated, or system-owned and keeps one stable `person_id` across lead promotion.
- `core.js` validates transport records in integer agorot and produces pure monthly snapshots by person, path, and currency, plus all-time work and client rollups. Client ILS hourly value is total ILS cash divided by all recorded minutes on ILS work; it is never an average of path rates. It never treats agreed work value or ad-platform usage as received cash.
- `UnifiedSheets.gs` is an operator-only installer. `setupUnifiedWorkbook(id, confirmation)` and `preflightUnifiedWorkbook(...)` are dry-run only. The mutating `installUnifiedWorkbook(...)` requires the exact workbook ID and confirmation `ASSAF_OS_UNIFIED_V1`.

The installer creates new tabs only. If any target tab already exists, it refuses the entire install before changing anything. Existing unrelated sheets and their data remain untouched. It adds no trigger, endpoint, secret, formula, sample row, remote synchronization, or automated send.

Amounts shown in Sheets are human-readable currency values. Any future OS/MIA transport must convert once at the boundary to safe integer agorot and must keep each currency separate. The current core groups financial results by currency and leaves shared costs unallocated.

Identifiers, phone numbers, email addresses and other text inputs are installed with plain-text formatting before use, preserving leading zeroes. Refunds must reference the original received-payment ID; the core rejects missing, mismatched, pre-dated, or cumulatively excessive refunds. Expense totals are explicitly `all_time`; they are not presented as monthly figures.

## Current boundary

This is a foundation slice, not a complete operating system. `לקוחות` and `עבודות` define protected calculated columns, and `core.js` supplies the corresponding rollup values, but no Sheet formulas or synchronizer populate those cells yet. The calculated `סיכום` tab is also an empty protected projection target. Direct row edits do not yet receive IDs, revisions, immutable-field enforcement, approval receipts, or conflict reconciliation. The existing legacy raw-cell-drift rule therefore remains the safe rule: an integration must fail closed until migration and direct-edit reconciliation are implemented and tested. Apps Script protections reduce accidental edits but are not an authorization boundary.

No web API is included. Future MIA/OS writes must use the existing exact, immutable, expiring proposal and approval model, revalidate the target, and persist idempotent receipts before this workbook can be called synchronized.

Run local tests without Google access:

```powershell
node --test tests/assaf-os/sheet-core.test.js
```

