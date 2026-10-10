# AssafWeb OS internal reminders — local implementation

Canonical source: `owner_reminders` in the existing PostgreSQL database. Independent
of Calendar, Sheets, lead follow-up drafts and customer outreach. Numeric Telegram
owner identity and a private chat are required. No customer transport is used.

## Existing functionality reused

- `LeadStore.try_claim_owner_notification_recipient`: conflict-safe recipient claims.
- `commit_owner_notification_delivery_state`: commit before any provider call.
- `record_owner_notification_recipient_delivery_outcomes_durably`: accepted receipts,
  known rejection release, ambiguous-claim retention.
- `deliver_owner_telegram`: numeric owner recipient intersection, accepted/rejected/
  ambiguous outcomes. The optional keyboard argument preserves all existing callers.
- Canonical CRM contacts/identities, existing Telegram webhook authentication,
  owner tool registry, application lifespan and PostgreSQL migrations.

## Workflow

Examples: `מיה, תזכירי לי מחר ב-11:30 לחזור לדני לגבי הצעת המחיר.` and
`Remind me tomorrow at 11:30 to prepare an invoice`.
Times are parsed from the current owner's actual message, never a model-supplied
timestamp/contact value. Supported dates: today/tomorrow/next week (Hebrew/English),
YYYY-MM-DD, DD/MM/YYYY; HH:MM in 24-hour format, English AM/PM, and numeric durations
in minutes/hours. Bare next week means seven days from today; a weekday phrase
requires an explicit date, rather than silently ignoring the weekday.
Unsupported expressions require a complete restatement. Missing,
conflicting or past times do not create a row. Jerusalem DST gaps are refused and
overlaps require an explicit matching UTC offset.

Creation confirms only after commit. Duplicate owner/event keys reuse the same row.
Contact name/phone/email/ID must resolve exactly and uniquely in the canonical CRM.
Ambiguous/unmatched names require `contact:<canonical ID>` or explicit `ללא שיוך` /
`unlinked`. A reminder without a named contact is explicitly confirmed as unlinked.

`התזכורות שלי` / `my reminders` lists active IDs and revisions, ten per page;
`התזכורות שלי עמוד 2` / `my reminders page 2` accesses later rows. Text changes include the ID
and displayed `גרסה: N` / `revision: N`: complete/cancel/snooze/reschedule. Notification
buttons complete or snooze 15 minutes. Open Contact reads a private CRM card, without
opening a customer conversation. Old revisions and terminal states cannot be revived.
`reminder_snapshot` exposes owner-scoped read-only counts and upcoming reminders for
future dashboard integration; no public dashboard endpoint is added.

## Model and scheduling

UTC `TIMESTAMPTZ` scheduled/created/updated fields plus the IANA timezone
`Asia/Jerusalem`; nullable CRM foreign key; immutable owner/source-event key; active /
completed / cancelled lifecycle; incrementing revision; separate pending / dispatching /
accepted / rejected / unknown delivery state. Each revision has a distinct receipt key.

The existing application process polls every 10 seconds using an in-process thread.
No extra deployed service, database, scheduler or cloud timer is required. SQL CAS on
the current active revision serializes worker/owner races. Dispatch state and receipt
are committed together before sending. Owner mutations during dispatch are refused
with an explanation; a cancellation before dispatch prevents sending. Cancellation
after accepted/uncertain dispatch changes future lifecycle but cannot recall a message.

The delivery target is the creating owner's private numeric Telegram chat, not all
configured owners. Owner removal from the allowlist stops new dispatches. Kill switch,
demo mode and delivery feature flag pause notifications.

Late policy: every still-active pending reminder remains due after downtime and is
delivered with its original local scheduled time and whole-minute lateness. No quiet
discard or arbitrary cutoff. Up to 100 due rows per cycle; cadence targets minute-level
delivery, not a hard SLA under outages or heavy backlog. Known rejection retries after
at least 60 seconds. Lost acknowledgements, malformed responses and crashed dispatch
claims become/stay unknown and are never automatically resent. Stale dispatch becomes
unknown after 60 seconds. Explicit owner snooze/reschedule creates a new occurrence;
it does not assert that an earlier unknown occurrence was delivered or not delivered.

## Backward compatibility and migration

The old owner_tasks date-only model, daily aggregated notification key, `mia-due-scan`
entrypoint and the GCP `mia-due-scan.timer` 15-minute configuration are unchanged.
No automatic upgrade
of old tasks is safe: those rows lack a time and may describe conditional follow-up
work. The owner must explicitly create any precise reminder they want.

Additive migration: `20261011_owner_reminders.sql`. Run against an approved release
before starting that release; the new schema participates in readiness checks. No
existing tasks/CRM data/receipts are rewritten. PostgreSQL tests exercise SQL migration
without ORM create_all and compare the actual schema with the mapped columns.

## Rollout requirements — owner approval required

1. Finish local verification and independent HEAVY review; commit only with authority.
2. Obtain CI green at the exact clean release SHA, including PostgreSQL tests.
3. Approve migration/deployment explicitly. Current production is the GCP VM `mia`
   in `me-west1-a`, project `mia-assafweb`, using `ops/gcp/README.md`. Historical
   ECS task revisions, ECR and EventBridge procedures do not apply.
4. Keep `MIA_REMINDERS_ENABLED=false` and `MIA_REMINDER_DELIVERY_ENABLED=false`.
   Preserve the full `mia-env` Secret Manager value using `ops/gcp/push-settings.ps1`
   with `-PatchSecret` for any separately approved setting change. Never read/copy env
   files or credentials. Pause existing jobs (`sudo mia jobs pause`) and make a
   verified backup (`sudo mia backup`) before the approved release.
   `ops/gcp/deploy.ps1 -ProjectId mia-assafweb -Ref <exact CI-green SHA>` builds that
   commit on the VM; Compose runs `mia-migrate` before starting `app`. Verify migration
   success, `/health` SHA, `/health/ready` and `sudo mia status` before resuming jobs.
5. Separately approve enabling creation, then delivery in the existing application.
   Apply approved flag changes through `-PatchSecret`, then `sudo mia settings` to
   restart the existing app. Leave the legacy due-scan systemd timer untouched.
   `sudo mia jobs pause` pauses one-shot jobs, NOT the in-process reminder thread;
   reminder flags and an app restart are needed to pause that thread.
   Verify one owner on a real phone,
   callback revisions, local time and receipt outcome before broader enablement.
6. Monitor due backlog, unknown states and lateness through the read-only snapshot.
   Real Telegram/model/mobile timing remains an external acceptance gate.

## Rollback

First disable `MIA_REMINDER_DELIVERY_ENABLED`, then `MIA_REMINDERS_ENABLED`. An already
committed dispatch may finish; wait for it and retain its receipt. Revert to the prior
approved application SHA using `ops/gcp/deploy.ps1 -ProjectId mia-assafweb -Ref <prior SHA>`
if needed, after confirming both flags off through Secret Manager patch + `sudo mia settings`.
Keep the additive table and all receipts;
do not drop or erase them. Existing daily due-scan behavior continues unchanged.
Re-enabling processes the overdue active backlog under the late policy, so inspect it
and obtain owner approval first. Unknown states are not reset for a retry.

## Future AssafWeb OS integration contract

These are internal Python interfaces, locally implemented, not deployed HTTP APIs.
`ReminderService(session).snapshot(owner_id=<verified owner>, now=<UTC>)` returns
owner-scoped lifecycle counts, due_pending/unknown/dispatching counts and up to five
future active reminders. The tool projection exposes only ID, text preview, timestamp,
contact ID and revision. It does not import or synchronize Sheets.

`list(owner_id=..., limit=..., offset=...)` lists active reminders from PostgreSQL.
`get(owner_id=..., reminder_id=...)` reads a single owner-scoped row. Contact IDs refer
to canonical `crm_contacts`, not Sheet row numbers. `get_by_source_event` supports
durable Telegram idempotency. `change` needs owner_id, reminder_id, expected_revision,
action and, for reschedule/snooze, scheduled_at. All mutations commit before success
and refuse stale revisions, terminal states and an in-progress dispatch.

The existing private Telegram webhook establishes owner identity using webhook-secret
verification and numeric ID allowlisting. A browser cannot reuse that as a dashboard
session. This change adds no browser auth exchange, cookie session, dashboard endpoint,
CRM edit/activity API or new external approval adapter. OS integration is blocked until
a server-authenticated owner session and HTTP boundary exist and are independently
reviewed. Never trust an owner_id supplied by the browser or expose a bot secret there.
No claims of deployed availability are made. Both reminder feature flags default off.

### Standalone iPhone PWA authentication prerequisite

Read-only inspection of the separate Quick Lead branch found legacy Telegram Login
Widget HMAC verification, not Mini App initData. Telegram's
[legacy Login Widget](https://core.telegram.org/widgets/login-legacy) supports websites;
the current [Telegram Login documentation](https://core.telegram.org/bots/telegram-login)
also describes browser authorization-code flow with PKCE. A standalone PWA does not
automatically receive Telegram Mini App credentials. The inspected branch has no
server-session exchange or iPhone frontend, so usable mobile authentication is incomplete.

Required future flow: register the approved HTTPS login/redirect URLs, bind login to a
server-generated one-use state and nonce/PKCE, validate Telegram proof/tokens server-side
(including freshness, signature, issuer/audience as applicable), enforce numeric owner
allowlist, then rotate into an opaque server session in the existing PostgreSQL database.
Use a Secure/HttpOnly/SameSite cookie, bounded session expiry/revocation, Origin and CSRF
checks for writes, and explicit immutable approval for CRM writes. Never store bot secrets
or persistent Telegram proof in browser storage. Prove Safari and installed-PWA redirect,
cookie continuity, logout/replay/CSRF behavior on a real iPhone before calling it usable.
Quick Lead is inherited from current master; this reminder PR does not implement its
future browser-session exchange.

## PR integration evidence

Branch: `codex/internal-smart-reminders`. Current combined integration base:
`e18518744119e50328a28f83b72447fb55b900cd` (Quick Lead #98 and Privacy #96).
Master CI run `38094781776` passed before integration. The previously unfinished
Privacy merge was completed, then current origin/master was merged normally, without
force push or changes to another worktree.

- Telegram webhook validates the full private numeric-owner context before opening a
  database session or acknowledging any callback, including `rm:*`. Recognized reminder
  tokens use the lazy authorized DB path; malformed tokens do not open a session.
  HTTP regressions cover rejection before DB/ACK/business and valid Complete/Snooze/
  Contact actions with SQLite and disposable PostgreSQL, including replay revisions.
- Quick Lead router, signed-login authentication, atomic persistence and associated
  tests are inherited unchanged from master. Its standalone iPhone PWA session remains
  explicitly incomplete and requires the secure server-session flow in
  `docs/QUICK_LEAD_CAPTURE.md`; that limitation does not block backend integration.
- CI preserves all master jobs (including the full Quick Lead PostgreSQL suite), GCP
  checks and widget behavior, and adds reminder PostgreSQL and boundary regressions.
- Reminder flags remain default-off. The existing daily due-scan worker and GCP timer
  remain unchanged. Deployment and rollback requirements target the current GCP VM.
- Final committed-SHA tests, combined independent review and CI are recorded in PR #97.
  Production migrations, feature activation and real-phone delivery still require
  separate owner approval. PR #97 must not be merged as part of this preparation.

## Verification evidence

Historical development snapshot full suite: **2619 passed**, 884 warnings, 112.64 seconds.
After its final
Hebrew verb-boundary fix, all four reminder suites were rerun: **82 passed**,
11 warnings, 14.94 seconds. PostgreSQL
reminder migration and concurrency tests used a temporary localhost PostgreSQL
container and fake delivery functions. Ruff (`app tests`) and widget behavioral
checks passed. `git diff --check` passed. The warnings include Python/asyncio
deprecations; they are not proof of production compatibility or real delivery.

Independent HEAVY verdict: **PASS for local implementation**, no remaining confirmed
blocking findings. Initial independent focused tests passed 44 cases with PostgreSQL;
an independent delayed-backlog reproduction verified the dispatch-clock race fix.
Final in-memory regressions verified compound/conflicting duration rejection,
Hebrew/English weekday clarification, Hebrew verb boundaries, delayed duplicate
confirmation, explanatory-question nonmutation and two-hour snooze. The reviewer's
final pytest rerun awaited sandbox approval and was aborted, so it adds no test
evidence. The full-suite/82-case results above are implementation validation.

Review findings fixed before acceptance: stale batch timestamps, rejected-backlog
starvation, duplicate replay after due, silent partial time parsing, hidden later
reminders, unsafe question/prefix intent matching and ignored supplied snooze time.
The temporary local PostgreSQL test container was stopped and automatically removed.
Deployment, live Telegram delivery, real-phone buttons and actual timing remain
unverified acceptance gates requiring explicit owner approval.
No deployment, live reminder, Telegram send, Calendar write, or cloud change is
authorized by this implementation task.

PR preparation added compound-date refusal and atomic rejected-delivery cooldown
revalidation, with no-persistence/race regressions and an enabled-runtime
failure-recovery/shutdown test. The HEAVY reviewer independently reproduced both
original defects and verified their fixes in memory. The release gate is the complete
suite against disposable PostgreSQL, Ruff, widget and diff checks on the final committed
revision; current results and exact SHA are reported in the PR and handoff response.
Historical development results above do not substitute for that release gate.

## Exact changed files

- `.github/workflows/ci.yml`
- `.env.example`
- `app/api/telegram.py`
- `app/core/config.py`
- `app/capabilities/types.py`
- `app/db/session.py`
- `app/db/reminders.py`
- `app/domain/owner/brain.py`
- `app/domain/owner/request_routing.py`
- `app/domain/two_state.py`
- `app/domain/reminder_time.py`
- `app/main.py`
- `app/graph/owner_agent.py`
- `app/services/notifications.py`
- `app/services/reminders.py`
- `app/surfaces/owner.py`
- `app/tools/owner/types.py`
- `app/tools/owner/reminders.py`
- `app/tools/registries/owner_tools.py`
- `app/workers/reminders.py`
- `migrations/20261011_owner_reminders.sql`
- `tests/unit/test_reminders.py`
- `tests/unit/test_reminder_owner.py`
- `tests/unit/test_reminder_worker.py`
- `tests/unit/test_reminder_postgres.py`
- `tests/unit/test_reminder_telegram_boundary.py`
- `docs/INTERNAL_REMINDERS.md`
