# AssafWeb OS P0B — separate website repository handoff

This is a specification only. No website file, production queue, setting or scheduler
was changed by P0A. Implementation and release require a new explicit authorization.

Repository: `assaf0buskila/assaf-landingPage`. Establish the current exact `master/main`
state and its own instructions in a new isolated worktree before implementing; the
accepted audit's website revision `c8bb52fc3b87e295458d8621dc23ec414fc3e7c4` is a dated
baseline, not an instruction to overwrite later work.

## Objective

Every genuinely submitted website form remains durable, reaches the existing Mia form
ingress, and has observable rejection/retry state without relying on the owner's email.
Use the existing Supabase intake queue; it is not a competing business CRM.

## Exact existing paths to inspect/change

- `lib/lead-crm.ts`: preserve original source ID/payload, improve bounded transient
  recovery, distinguish captured/replayed from Telegram delivery, quarantine permanent
  400/409/422 outcomes and expose sanitized queue metrics.
- `lib/lead-store.ts`: service-only queue operations and lease compare-and-set; report
  pending, leased, synced and conflicted counts/oldest age where actually measurable.
- `app/api/ops/crm-sync/route.ts`: reuse authenticated POST; no unauthenticated run or
  private data in responses/logs. An audit must never invoke this mutating endpoint.
- `app/api/cron/keepalive/route.ts` and `vercel.json`: examine actual plan/schedule limits
  before selecting recovery frequency. Keepalive also performs retention deletes; do
  not use it as an innocuous health read or indiscriminately increase its invocation.
- `supabase/migrations/20261008000000_crm_delivery.sql`: retain cutoff, lease ownership,
  SKIP LOCKED and guarded finish semantics; add a separate additive migration only if
  the approved measurements cannot use existing state.
- `scripts/lead-crm.test.mjs`, `scripts/lead-crm-postgres.test.mjs`: existing tests to
  extend, with disposable PostgreSQL for real queue concurrency.
- `docs/form-crm-runbook.md`: update activation, diagnostic access, acceptance and rollback.

All these paths belong to the website repository. P0A does not edit that checkout.

## Immutable contract

Keep `/v1/internal/assafweb/form-leads` and its dedicated shared-secret authentication.
The Supabase row ID is Mia's `source_id`; the browser attempt/event ID is a separate
deduplication layer. Preserve submitted time, original stored payload and cutoff across
all retries. Never mint a new source ID to escape a conflict or timeout.

Mia acknowledges only a committed receipt with canonical Contact and Activity IDs.
Captured/replayed is not proof of Telegram delivery. A 409 may leave no Mia receipt,
Activity or issue because the transaction rolls back; the website must preserve and
expose that original event for owner resolution. No automatic identity merge.

Do not replay pre-cutoff records, rewind activation time, import old leads, or treat
test/internal submissions as genuine production enquiries. Native platform lead forms
and Leo integration are separate scope.

## Recovery and private visibility

Replace daily-only recovery with an explicitly approved frequency supported by the
actual hosting setup. Prefer the existing authenticated queue operation. A two-minute
GCP timer is not implemented or authorized by P0A; any cloud/schedule change needs its
own approval and rollback plan. Monitor oldest event age as well as retry counts.

Use bounded backoff for transient failures and a separate conflicted state for permanent
rejections. Do not retry unknown Telegram deliveries from the website: it only owns
intake delivery, while Mia owns notification receipts.

Queue health access must be authenticated and sanitized. Include rejected/conflicted
counts where measured, without visitor fields, credentials or provider error bodies.

## Required tests and acceptance

- First submission, unchanged browser retry and changed-input event behavior.
- Lost Mia response after commit: same source replays one receipt and one original ping.
- Concurrent claim, lease expiration and a late worker finishing after lease transfer.
- 409 retains the source event, is visible and is not endlessly resent.
- Network/5xx recovery preserves IDs/payload and clears the queue after service recovery.
- Invalid/missing receipt never becomes synced.
- Cutoff, test/internal exclusion and campaign attribution remain intact.
- Queue errors/logs remain sanitized; no unauthenticated operational execution.

Targets for later approval/testing: normal-path handoff within two minutes, backlog
recovery within ten minutes after service restoration. These are proposed acceptance
targets, not verified production guarantees. Only an explicitly approved live enquiry
can prove the full website -> Mia -> Telegram path.

Rollback: disable only the new recovery mechanism or restore the prior compatible
website version; preserve queued rows, cutoff, attempts, conflict state and source IDs.
Never clear synced state or resend accepted records. Separately approve any production
queue correction or historical import.
