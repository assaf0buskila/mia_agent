# Assaf OS release preparation — 2026-10-12

Deployment evidence and remaining acceptance as of 13 October are recorded in `ops/assaf-os/LIVE_STATUS.md`: Apps Script version 10 (rollback version 9), Cloudflare Worker `c2237976` (rollback `0f52dbef`), and the latest public MIA health there is `5672cc1` (status `ok`; `345d0a7` is the earlier owner-import release). These IDs are historical: the last verified Worker and MIA IDs are in `docs/CLAUDE_CODE_HANDOFF.md`, section "Last verified production and access" (it records the installed Apps Script version as unverified, so Script version 10 at `bda48fa` remains the latest Script record), and MIA's live commit is whatever `/health` reports as `deployment.commit_sha`. Actual native/OS round trip, one human Telegram-approved test, populated OS phone/call/WhatsApp rendering and four real legacy learning rows are verified. The fifth, real browser/form intake and owner/device timing/UX acceptance remain open. The Hebrew closure checklist is `docs/ASSAF_OS_FINISH_PLAN.md`. The preparation snapshot below is historical; its disabled-gate and undeployed statements are not current operational status.

The owner selected Cloudflare for the OS, one existing Google Sheets workbook
for business data, and Apps Script for the shared record/approval service. Direct
Sheet edits, OS edits, and MIA proposals use the same workbook. The owner explicitly
confirmed Telegram approval followed by Sheet/OS update. On 12 October the owner
also confirmed keeping the existing paid MIA VM and removing only the old paid OS
staging VM. No new server is part of this release.

## Verified current state

- Old `owner-os-staging` VM, its boot disk and its reserved external IP are deleted.
  Exact evidence is in `ops/assaf-os/CLEANUP_REPORT.md`; this does not mean all Google
  Cloud billing is zero. The existing `mia` VM is still running.
- Google Auth branding and the Web client `Assaf OS Web` were created in the
  existing `tomibot-telegram` project after explicit Google policy approval. The
  owner is the only test user. OAuth client secrets were not downloaded or read.
- After separate explicit owner approval, `assaf_os_auth` and limited NOINHERIT
  role `assaf_os_session` were created inside the existing MIA PostgreSQL container.
  Five opaque technical session tables and actual effective-role posture passed.
  The existing Secret Manager received only OS additions; all activation gates
  remain false. No MIA settings reload or backend deployment has occurred yet.
- The previous bound Apps Script source was preserved locally with SHA-256
  `717d6fa7baf137a9a240371c33b241402745972d69f9f20cb07d8b938010abd3`.
  The reviewed unified source was saved to the existing bound project and copied
  back from the actual editor for exact source comparison. The HMAC handoff,
  installer, trigger permission, webapp deployment and actual native Sheet flow
  remain pending.

## Local evidence

- Full backend suite: 3,087 passed, 108 skipped. Skipped disposable PostgreSQL
  coverage must run in CI; it is not production acceptance.
- Apps Script suite: 52 passed, including 19 executed actual `.gs` lifecycle tests
  with Google-shaped Date, formula, grid, batch and native-edit behavior.
- Frontend/Worker suite: 117 passed. Actual Script-to-frontend cross-contract test
  passed separately. Independent HEAVY corrected-delta review passed.
- Ruff, widget behavioral check, source syntax and diff checks passed.

## Remaining release gates

The new browser CI replaces retired legacy Today/contact/reminder UI assertions
with real unified-product flows through BFF, PostgreSQL sessions and actual Apps
Script dispatcher. Run both Chromium and WebKit against the final pinned backend
SHA, and deploy MIA only from a clean CI-green exact SHA. Cloudflare publication,
real Google sign-in, authorized Sheet installation, native edit refresh and one
owner-reviewed Telegram proposal must be observed before claiming end-to-end live.

When the unified binding is enabled, owner legacy CRM/Sheet/business writers and
their side-effecting read-sync surfaces are hidden and blocked before provider or
store effects. Old pending approvals cannot write the old workspace. Public
website CRM intake remains unchanged; this release does not claim its historical
leads have been migrated. Ad reporting is conditional on verified existing
connections and has no automatic import or spend/action path.

Native Sheet locks do not lock human editing. Invalid input produces issues and
partial/null API calculations; the last successful native calculated projection
may remain visible. A new original payment needs its assigned ID before a refund
can reference it. Approval receipts, never model wording, establish success.

## Rollback

Keep the exact old MIA image/revision `a213c9a7fbdf5586e13cb38ffdbc4646f59c4541`
and the reviewed new revision. Disable only the new OS gates before rollback;
preserve all unrelated current settings. New sessions may be revoked as part of
an auth rollback. Script deployment versions preserve the previous webapp code;
business rows are never reset or deleted automatically. Recreating the deleted
staging server/IP is not a rollback step.
