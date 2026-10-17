# AssafWeb OS P0A — reviewed implementation and release gates

## Authority and state

P0A was authorized for local Mia implementation, isolated test databases and independent
review. The owner subsequently authorized a clean commit, feature-branch push and Pull
Request against `master`. Merge, deployment, live database changes, Sheets writes,
external messages, cloud settings, historical reconciliation, website and Leo changes
remain outside this authorization.
Base: `660160d79002404e979dbfd88cf5d297e0055758`, repository
`assaf0buskila/mia_agent`, branch `codex/mia-p0a-crm`.

Current production is on Google Cloud; `ops/gcp/README.md` is the deployment runbook.
Older AWS statements in the historical documents are not deployment instructions.

## Existing-data decisions that remain open

The accepted read-only audit observed five canonical contacts versus two Sheet Contact
IDs, seven activities versus four Sheet Activity IDs, and two conflicted Contact jobs.
It also observed 20,350 historical `missing_contact` rows representing five observations,
last created September 30, plus two October 10 `missing_sheet_row` issues and an older
destination identity collision. These are dated observations, not refreshed P0A results.

No customer data was imported into this worktree. A fresh exact-ID reconciliation has
not been run against production during local implementation. Missing rows alone do not
prove deletion, intent, or an application defect. Required owner decisions include:

- Whether each missing Contact/Activity row should be restored, archived or investigated.
- Whether destination identity collisions represent the same person; never merge by phone
  resemblance alone.
- Whether the two October 7 form events were genuine enquiries or authorized tests.
- Whether old pre-cutoff website records should ever be imported, under separate approval.
- Whether any historical issue should be archived; old/open is not synonymous with resolved.

Dataset A (42 historical sends, zero human replies/meetings) and dataset B (52 frozen,
never-sent prospects according to the owner) remain separate and untouched. No outbound
workflow is activated and no prospect is promoted to an inbound lead.

## Diagnostic privacy and read-only contract

The report may contain counts, internal IDs, revisions, row numbers, safe issue/status
codes and explicit uncertainty. It must not contain names, phone numbers, emails,
business descriptions, visitor text, raw provider errors, credentials or payloads.
Malformed untrusted metadata must not escape through an alleged ID or timestamp.

Only Contacts/Activity read methods are permitted. Never call `ensure_crm_workspace`,
`sync_from_sheets`, imports, conflict resolution, workers or existing owner CRM searches
to obtain a diagnostic snapshot. Some older owner reads synchronize Sheet edits.

The CLI does not read `.env`, provision tables or run migrations. Database reads are
rollback-only with PostgreSQL READ ONLY isolation or SQLite query-only protection.
Sheets and the DB cannot share a distributed snapshot: the report must expose this
uncertainty and distinguish incomplete/unavailable reads from genuinely missing rows.

Production execution instructions are a handoff, not authorization to execute them.
Supply credentials only via the approved runtime environment. A database role restricted
to reads adds a second guard. Never put a DSN or secret on a command line.

From this checkout, inspect the executable interface:

```powershell
uv run python -m scripts.crm_reconcile_readonly --help
```

For an existing, disposable local SQLite fixture, with its DSN already provided through
`MIA_DATABASE_URL` (the CLI never creates a missing file):

```powershell
uv run python -m scripts.crm_reconcile_readonly --environment test
```

Only after separate authorization for live **reads**, the PostgreSQL plus Sheets command
is the following. Redirect stdout into an owner-private destination, not public logs:

```powershell
uv run python -m scripts.crm_reconcile_readonly --environment prod --allow-production-readonly --allow-network-database-readonly --with-sheets --max-rows 5000
```

Required existing runtime variable names: `MIA_DATABASE_URL`, `MIA_COMPOSIO_API_KEY`,
`MIA_COMPOSIO_USER_ID`, `MIA_SHEETS_SPREADSHEET_ID`, `MIA_TELEGRAM_OWNER_USER_IDS`.
Confirm the workbook identity before running; the existing adapter can fall back to the
locked workbook if its override is empty. No Telegram token is needed for diagnostics.
The command does not load `.env`. Omitting `--with-sheets` produces DB-only health and
explicit `not_requested` Sheet sections. `postgres://` and `postgresql://` use the same
psycopg3 normalization as the application.
Run the module from an approved source checkout with its locked dependencies and a
reachable read-only connection. The existing application image copies `app` and
`migrations`, not this standalone `scripts` directory; do not assume the CLI is already
installed inside that image. P0A does not alter the deployment image or open DB ports.

The current Composio adapter has no trustworthy Sheet grid extent. It scans each bounded
range, including ranges after empty pages, but reports global coverage as **partial**.
Observed IDs, duplicates and differences are useful evidence; absence outside that range
is unknown. It never labels a Contact/Activity missing from the whole Sheet on a short or
empty range response. No new provider capability is presumed or activated by P0A.

Database aggregates cover the full retained issue/outbox dataset. Detail lists remain
bounded with explicit truncation metadata; historical duplicate issue rows are separated
from distinct open/resolving observations. An old open issue is still open until there
is evidence/approved resolution, even when its current Sheet cause is unobserved.

## Private operational health

`crm_operational_health` is an owner-only read tool. It verifies the numeric Telegram
actor against the existing owner allowlist, reads the database without synchronizing
Sheets, and exposes sanitized operational status. It is absent from visitor tools and
does not add a public endpoint. Requesting a health report is an explicit owner read,
not an automatic Telegram alert.

After an approved deployment, the owner can request `מצב CRM`, `בדיקת CRM` or `CRM health`.
These standalone commands bypass the model and existing Sheet-sync search path. The
registered diagnostic is excluded from the normal model tool payload; mixed action
requests retain their existing routing and approval requirements.

State uses the existing `crm_worker_state` table. Heartbeat/cycle/projection timestamps
start only after this version actually runs; an absent value is unknown, not healthy.
Confirmed outbox rows describe historical provider acceptance, not continuing Sheet
presence or owner read status. Rejected intake attempts cannot be reconstructed from
committed receipts after transaction rollback; report them as unmeasured, not zero.

Telemetry failure must not change send outcomes or cause a resend. Retain recipient
claims, confirmed receipts and ambiguous-delivery behavior. No message-ID storage is
introduced.

Notification diagnostics measure the expected **primary** handoff per form/site scope
and configured numeric recipient. Pending/failed jobs, missing jobs, missing receipts,
ambiguous/current claims and conservative legacy claims retain safe affected IDs.
Unparseable source scopes are explicitly unknown. Later update-message jobs are counted
in the full outbox, but missing update intent cannot be reconstructed from existing
tables and is reported as unmeasured. These measures do not prove the owner read a ping.

## Disabled phone comparison policy

`MIA_CRM_ISRAELI_PHONE_NORMALIZATION_ENABLED=false` remains the default.
The helper recognizes valid local Israeli mobile `05...` and explicit `+972...`
representations as the same comparison key, preserves other explicit international
numbers, and rejects malformed/extended input before legacy digit stripping.
This is syntax validation, not proof of assignment, ownership or consent.

Contact fields continue to use the existing storage behavior; the feature affects
identity comparison keys, not a historical rewrite. Public-writer field protection
continues to apply. Bare country codes are not guessed to be international.

Do not enable against incompatible historical identity keys. Enabled identity writers fail
closed if the preflight finds a persisted key that would change or become invalid.
The form ingress exposes this as its existing 409 capture-conflict response; no new
contact, receipt or notification is created. Existing valid receipts still replay.
Malformed new Israeli input under the enabled policy returns sanitized 422 at form
ingress. Capture, Sheet import and conflict resolution use the same canonical collision
key and locking, while preserving the approved contact field's representation.

Activation prerequisites, requiring separate production approval:

1. Run the sanitized read-only readiness report.
2. Review all noncanonical/invalid keys and cross-contact equivalence groups.
3. Back up and restore-test the database.
4. Pause intake/import writers for an explicitly approved identity migration.
5. Resolve collisions through the current revision/approval safeguards. Never silently
   merge contacts or replace a verified identity.
6. Verify all persisted keys and the field-to-identity mapping, then enable and restart
   the consistent runtime policy. Do not mix old/new comparison policies concurrently.

No migration is included or executed by P0A. Deployment with the flag disabled requires
no schema migration and preserves legacy phone behavior.

## Verification and release plan

Run required pytest with a unique basetemp and redirected output, Ruff and the existing
node widget test. PostgreSQL checks use an isolated disposable local database/schema.
Tests never read `.env` or call live Sheets, Telegram, website or cloud providers.

Obtain a fresh HEAVY independent review over the complete final diff, including new
files. Preserve the exact test logs and review verdict. Commit, feature-branch push and
Pull Request preparation are explicitly authorized. Merge and production release still
require separate approval.

For a subsequently approved production release: require a clean checkout at the exact
CI-green revision, verified backup, GCP runbook deployment, exact-SHA health/readiness
checks and private CRM health inspection. A real enquiry or synthetic owner ping
requires explicit authorization; local tests are not live delivery or phone acceptance.

Acceptance: diagnostics cannot write; privacy adversarial tests pass; receipt/activity
coverage is traceable; heartbeat and unresolved jobs are visible; delivery ambiguity
remains protected; the disabled flag leaves existing behavior intact.

Rollback: restore the previous compatible GCP application image under separate approval.
Leave all contacts, activities, receipts, outbox rows and worker-state observations intact.
Never clear unknown claims or turn confirmed jobs back into pending. Keep phone policy
disabled; no data rollback is required for the local P0A version.

The separate website handoff is `docs/P0B_WEBSITE_HANDOFF.md`.

## Accepted implementation validation — 2026-10-10

- Full final suite: **2,647 passed, 0 failed, 0 skipped**, including real PostgreSQL
  integration/concurrency and plain-DSN read-only CLI checks. Local PostgreSQL 16 was
  disposable, loopback-only, separate from all existing containers and production.
- Command: `MIA_ENV=test uv run pytest --basetemp=.cache/p0a-release-local -p no:cacheprovider`,
  with `MIA_TEST_POSTGRES_URL` set only to the disposable local database. Output was
  redirected to `.cache/p0a-release-local.log`. Runtime: 106.29 seconds.
- Ruff: `uv run ruff check app tests scripts/crm_reconcile_readonly.py` passed.
- Existing `node tests/unit/widget_behavior.test.js` passed.
- Origin binding, existing GCP settings merge unit checks and `git diff --check` passed.
  GCP checks were local mocks; no cloud settings were read or changed.
- Independent HEAVY review: **APPROVED for local P0A; no remaining blocking findings**.
  Final independent run: 26 passed; preceding broader independent run: 172 passed.
  Reviewer independently reproduced the repaired failures and verified SQLite and
  PostgreSQL reject an injected table creation in the diagnostic transaction.
  The review verdict and local test evidence were checked before PR preparation.
- 911 dependency deprecation warnings remain in the full-suite log; no safety test
  was weakened, retired or skipped. This implementation validation preceded PR CI.
- **Not performed:** live production reconciliation, live Sheets reads/writes, real
  Telegram send/receipt/read verification, cloud deployment/configuration, historical
  data correction or phone-key migration. These are approval-dependent acceptance
  actions, not skipped tests disguised as successful checks.
- The original checkout was preserved. No production record was altered. Subsequent
  PR preparation reruns the required checks and records exact results separately.

## PR preparation validation — 2026-10-10

- Required full suite rerun: **2,647 passed, 0 failed, 0 skipped**, with 911 existing
  dependency deprecation warnings, in **106.16 seconds**.
- Command: `MIA_ENV=test uv run pytest --basetemp=.cache/p0a-pr-20261010 -p no:cacheprovider`.
  `MIA_TEST_POSTGRES_URL` pointed only at a new disposable, loopback-only PostgreSQL 16
  container with no persistent volume. Output: `.cache/p0a-pr-20261010.log` (ignored).
- `uv run ruff check app tests scripts/crm_reconcile_readonly.py` passed.
- `node tests/unit/widget_behavior.test.js`, `uv run python scripts/assert_origin_bind.py`,
  and `pwsh -NoProfile -File ops/gcp/tests/test-settings.ps1` passed. Settings tests
  use local mocks and perform no cloud change.
- The complete 23-file candidate set was inspected, including every untracked source,
  test and document. No unrelated changes, generated artifacts, local machine paths,
  credentials or real customer records are included. Privacy tests use synthetic data.
- No schema/migration, deployment, cloud, notification transport or recipient-claim
  implementation file changes are included. New configuration is additive and defaults
  to disabled. Existing identity/receipt/safety tests were retained.
- A fresh independent HEAVY publication review examines the complete final diff.
  The PR records its final verdict and CI results for the exact pushed commit.
- PR CI uses Python 3.12 and PostgreSQL 18 with pgvector, complementing the local
  PostgreSQL 16 run. Verify all `checks`, `postgres` and `container` jobs before
  considering the branch ready for a separately approved merge.
- No production acceptance action or historical reconciliation was performed.

## Exact change inventory

All paths below are repository-relative.

| File | Reason |
| --- | --- |
| `app/services/crm_diagnostics.py` | Sanitized SELECT-only reconciliation, complete aggregates, bounded evidence and Hebrew operational view. |
| `scripts/crm_reconcile_readonly.py` | Explicit environment/production read gates, read-only DB transaction and existing Sheets read adapter. |
| `app/workers/crm_delivery.py` | Best-effort heartbeat, cycle/delivery/projection timestamps in existing worker-state table. |
| `app/services/phone_identity.py` | Pure opt-in local Israeli/E.164 comparison helper. |
| `app/services/crm_v2.py` | Disabled policy, readiness guard, canonical locking/collision checks in all identity writers. |
| `app/services/form_intake.py` | Map migration conflict and invalid phone without partially committing intake. |
| `app/api/form_intake.py` | Sanitized 422 for enabled invalid-phone rejection. |
| `app/core/config.py`, `.env.example` | Name and disabled default for the opt-in phone policy. |
| `app/domain/two_state.py` | Private owner diagnostic registered outside visitor tools. |
| `app/domain/owner/request_routing.py` | Narrow explicit health commands and owner inventory entry. |
| `app/surfaces/owner.py` | Authenticated deterministic health route; normal routing unchanged. |
| `app/tools/owner/crm.py` | Read-only health handler with principal and numeric allowlist checks. |
| `app/tools/registries/owner_tools.py` | Register diagnostic without growing the normal model tool payload. |
| `tests/unit/test_crm_diagnostics.py` | Privacy, aggregates, Sheet gaps, correspondence, receipts and worker recovery. |
| `tests/unit/test_crm_diagnostics_cli.py` | Executable CLI, adapter wiring, forbidden writes and real PostgreSQL read-only DSNs. |
| `tests/unit/test_crm_health_owner_route.py` | Auth denial, deterministic health and unchanged ordinary routing. |
| `tests/unit/test_phone_identity.py` | Invalid/ambiguous/repeated/concurrent input and import/conflict-resolution regressions. |
| `tests/unit/test_form_intake.py` | Add atomic enabled invalid-phone 422 checks; retain every existing assertion. |
| `.github/workflows/ci.yml` | Include new real-PostgreSQL tests in the existing CI job. |
| `HANDOFF.md` | Record review, PR preparation and release authorization boundaries. |
| `docs/P0A_CRM_RUNBOOK.md` | Execution, privacy, validation, migration/activation, acceptance and rollback handoff. |
| `docs/P0B_WEBSITE_HANDOFF.md` | Separate website specification only; website files untouched. |
