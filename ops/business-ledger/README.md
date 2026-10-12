# AssafWeb OS Business — isolated financial ledger

The product name is **AssafWeb OS Business**. The environment remains **TEST, synthetic data only**. Renaming does not activate LIVE data or permissions. MIA PostgreSQL remains the CRM authority; the separate Google workbook is the financial authority. No financial database or CRM import is introduced.

## Current package

The bound Google service combines `core.js`, `finance-core.js`, `Code.gs`, `finance-adapter.gs`, `finance-forms.gs` and `crm-projection.gs`. `appsscript.json` enables the existing Advanced Sheets service. The projection helper is a bounded trusted TEST operation, with no scheduler or frontend/model write route.

The existing private workbook has nine protected Hebrew RTL tabs with Rubik: עסקאות, חיובים צפויים, תשלומים שהתקבלו, הוצאות, פרויקטים, הגדרות, יומן שינויים, קונפליקטים, תצוגת CRM. Its title is AssafWeb OS Business; Settings must retain `mode=TEST` and the exact workspace.

Backend routes under `/v1/owner/os/business`:

| Method | Path | Behavior |
| --- | --- | --- |
| GET | root | Accepted deal snapshot |
| GET | `/finance` | Financial records, counts, coverage and totals |
| GET | `/receipts/{request_id}` | Owner-bound committed receipt |
| POST | `/deals` | Confirmed deal create/update; committed receipt |
| POST | `/schedules` | Finite charges for an accepted deal revision |
| POST | `/payments` | Manual confirmation of received money |
| POST | `/expenses` | Immutable original expense |
| POST | `/projects` | Basic project create/update |

The mobile BFF exposes matching fixed paths under `/api/business`. All routes are disabled by default. The server requires exact TEST, approved numeric owner, matching workspace and owner-session scopes; mutations also require Origin and CSRF validation. Provider URL and HMAC key remain server-side. Quick Lead, reminder writes, outreach and delivery workers stay disabled.

## Financial behavior

Every amount is the **total for the entire deal**, including monthly/installment plans. A plan has an explicit finite count, at most 36. Integer agorot and remainder distribution preserve the exact total. Monthly dates preserve the original anchor through short months. There are no automatic collections or perpetual charges.

`business-ledger.finance.v1` reports observed time, coverage, counts and accepted records. Complete financial coverage distinguishes deal value, scheduled charges, manually received payments, outstanding amounts, expenses and net cash. Unknown, stale, unavailable or incomplete sources are never zero: unknown coverage returns null totals. Net cash is not accounting profit or verified provider revenue.

Payments require an accepted charge revision, cannot exceed its outstanding amount and require `confirmed_received=true`. Posted payments and original expenses cannot be silently edited. Audited corrections/adjustments remain unavailable. Financial fields of a scheduled deal cannot change; safe title/notes edits require its current revision.

## Existing operator setup

Reuse the approved private TEST workbook, bound script and deployment. Do not recreate resources, copy customers, reuse production secrets or request keys in chat. Required Script Properties: `TEST_WORKBOOK_ID`, `TEST_WORKSPACE_ID`, `OWNER_NUMERIC_IDS`, `LEDGER_HMAC_SECRET`, `OPERATOR_OWNER_MAP`. Only names belong in source-controlled examples. The operator-controlled Google identity maps to an approved numeric owner before menu writes.

Reload the sheet and use **AssafWeb OS Business** → **יצירה או עדכון עסקה** or **חיובים, תקבולים, הוצאות ופרויקטים**. The **הגדרת סביבת בדיקה** installer requires the coherent existing nine-tab schema and refuses missing tabs, duplicate aliases, invalid mode or mismatched workspace.

The financial form persists a server-side owner/workspace-bound two-minute proposal and revalidates it before dispatch. Mobile also shows exact confirmation. A metadata-only pending marker precedes each business POST. Ambiguous responses lock all business writes across reopening/reload. Recovery only reads the same request's receipt. Missing receipts never clear markers or authorize another send; acknowledgement requires a matching committed receipt.

One script lock covers validation/commit. Business rows, immutable accepted audit snapshots and durable receipts commit atomically in one Advanced Sheets `batchUpdate`. Raw cell drift fails closed. Protections reduce mistakes but cannot prevent owner edits. Finite row and serialized-response limits are enforced before writes; existing receipts remain retrievable at capacity.

The signed POST permits one HTTPS response redirect to `script.googleusercontent.com`, converted to GET without original body/credentials. No automatic POST retry. Google socket timeout is 15 seconds per socket operation, not a total deadline. Ledger BFF/browser abort deadlines are 35/40 seconds; auth/CRM deadlines are unchanged. Browser cancellation does not prove a backend write failed.

## Evidence and focused checks

The existing Google deployment was updated to **v4** on 2026-10-12 01:24 Israel time, with unchanged URL/account/access. Reviewed combined six-file SHA-256: `0f22a6f73b7816ad2e1d7f6cda83f8b7f5736a4a0beec6c7b40fa449eedd6109`. Workbook/script use the Business title. This proves Google TEST service deployment, not a new GCP/iPhone application deployment.

Actual A/B tests have committed synthetic creation/edit receipts, replay and stale-revision rejection. The read-only end-to-end finisher passed through mobile client/BFF, owner session, disposable PostgreSQL and Google TEST. One real finance read took 9.629 seconds, motivating the narrow deadline change. Slice C passed finite schedules, partial manual payment, project/expense, stale/overpayment rejection and injected lost-response receipt-only recovery. Its stable-ID marker is retained; never reset it or rerun original writers.

Slice E passed the actual TEST MIA proposal/approval dispatch, exact project target revalidation, one signed write, injected lost-response recovery and a committed receipt. This used the canonical approval store locally; it is not a real production Telegram/model turn. Slice D passed canonical form-intake capture/replay/conflict on disposable PostgreSQL, protected Owner OS contacts/activity reads, one signed CRM View update and receipt recovery read. The view contains one synthetic contact, projection revision 1, no phone/email, and does not alter financial totals. This is not an HTTP test of the public website or a production CRM sync.

The v4 financial form was exercised in actual Chrome: existing project fields prefilled, exact two-minute preview retained title/status/deadline, a notes-only synthetic edit committed at project revision 3, and its durable receipt was acknowledged. The subsequent current OwnerClient -> BFF -> MIA -> real TEST finance read finisher passed: exact edited project fields rendered, zero business POSTs, unchanged financial counts/totals and campaign marker, logout then session 401. Logs: `.cache/business-ledger-test/{ab-readonly,slice-c,slice-d,slice-e,sheet-readonly}-results.log`. All five selected actual tests returned exit 0. These programmatic client/view checks do not establish physical Safari acceptance.

Offline checks, external opt-ins OFF:

```powershell
$env:MIA_ENV='test'
node --test tests/business-ledger/core.test.js tests/business-ledger/finance-core.test.js tests/business-ledger/adapter_runtime.test.js tests/business-ledger/finance-adapter.test.js tests/business-ledger/finance-forms.test.js
& '..\owner-os-integration\.venv\Scripts\python.exe' -m pytest tests/unit/test_business_ledger.py tests/unit/test_business_ledger_finance.py tests/unit/test_business_ledger_transport.py tests/unit/test_business_ledger_actions.py -q --basetemp=.cache/ledger-focused-unique > .cache/ledger-focused-unique.log 2>&1
```

Use unique test directories and preserve full pytest summaries. Restricted Windows loopback tests may need approved local-only execution. Google synthetic campaigns require explicit operator approval, exact resource pins and explicit case selection. Local dummy authentication/HTML tests do not establish real Telegram/model/iPhone acceptance.

## Remaining release gates

Independent MIA proposal/approval, projection, readonly-fixture privacy/cleanup and combined intake reviews passed. The controlled Sheet-to-app read finisher and combined intake compatibility checks passed. The imported bounded Unicode excerpt change passed 25 PostgreSQL intake tests, 13 existing Owner API PostgreSQL tests (one unrelated BFF case deselected) and 12 reader tests; the fresh reviewer independently ran the 12 readers plus 24 boundary probes. Ruff, widget and 27 focused Google adapter/form/projection tests passed. Current cross-product state and ordered release gates are in `CLOSEOUT_STATUS.md`; reusable scope prompts remain in `WEBSITE_MIA_PROMPTS.md`. The website contract work is now merged as tests/docs-only PR #64, without proving public website runtime configuration or live delivery.

New backend/mobile work remains local and isolated. Commit/push, CI and private GCP deployment of newly tested SHAs require the applicable explicit release approval. No PR merge, public Vercel change, production MIA/customer data, DNS, P0B or production-secret change. Retain a tested rollback revision and revoke staging sessions on rollback. A separate LIVE-data gate and iPhone Safari/Add to Home Screen acceptance remain before real business use.
