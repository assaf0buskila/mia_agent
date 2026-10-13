# Local PostgreSQL transaction prototypes

## Seven-entity extension, local only

The extension is isolated in `codex/os-primary-seven-entities-local`, based on the
original clean prototype commit `a8e46d4662362df8536438265cfeef7f0bc76d9e`. The original
branch and other worktrees remain intact. The approved architecture is recorded in
the existing `performance/integration/DATABASE_FIRST_MIGRATION_PLAN.md` in the parent
task directory. This extension covers synthetic domain parity and transactions;
it does not complete the plan's importer, recovery, mirror, or activation stages.

`app/prototypes/assaf_os_ledger.py` adds a separate twelve-table metadata set:
workspace, seven domain tables, proposal, receipt, event, and pending outbox. It
inherits the original disposable-loopback/default-off/numeric-principal guards.
It is never imported by runtime application code, registered as a migration,
exposed through routes, or connected to Google. The injected principal remains
test context, not real authentication, session/CSRF, policy, kill-switch, or existing
owner approval-envelope integration.

The seven entity names are `leads`, `clients`, `works`, `payments`, `time`, `expenses`,
and `tasks`. Canonical input roles, Hebrew enums, contact normalization, references,
refund graph validation, source deduplication, and immutable paths come from the
existing local `schema.js`, `runtime.js`, and `UnifiedRuntime.gs`. The frozen JSON
input schema has a Node drift test. New IDs are generated only for new records;
references accept the existing general string ID contract without prefix assumptions.
Promotion preserves the lead's person identity and commits the lead/client together.
Promoting to an existing client preserves its inputs and advances its revision as
Apps Script does.

Proposal creation writes no business data. Exact request replay returns the stored
proposal; payload mismatch fails. The expiry is ten minutes or the earlier supplied
outer approval expiry. Decision IDs, numeric owner, workspace, authority epoch,
envelope digest, target/source/dependency fingerprints, and current domain graph
are checked under a per-workspace PostgreSQL row lock. Approval atomically updates
records and deterministic summaries, consumes the proposal, and inserts one receipt,
audit event, and outbox event. Both channels resolve the same terminal receipt.
Outbox delivery stays pending; there is no exporter or fallback writer.

Money and minutes are checked JS-safe integers with PostgreSQL bounds. Business
dates additionally use PostgreSQL `DATE` columns and retain canonical ISO inputs;
event timestamps are timezone-aware UTC. Workspace-scoped foreign keys protect
client/work/refund references. The expense uniqueness index uses a bounded digest
of the exact source type/key while retaining and validating the original values,
so valid long Unicode source keys do not exceed PostgreSQL's index size limit.
Summary overflow fails closed. A competing create cannot exceed the 2,000-record
synthetic workspace bound: capacity is rechecked inside the approval transaction.

`assaf_os_summaries.py` derives monthly, currency-total, work, and client projections
from accepted inputs. Agreed amounts do not become income. Refunds reduce actual
payments; cancelled records, unpaid expenses, and estimates follow the existing
rules. Missing time and incomplete coverage retain nulls. Currencies are separate,
hourly rounding uses checked integer arithmetic, and optional/null work currency
falls back to ILS for monthly time buckets. Monthly/coverage parity is checked
against the actual offline Apps Script summary with a Node VM oracle. The source
runtime has no currency-total function; local totals are deterministic rollups.

`LocalLedgerPrimary.propose/decide/receipt/snapshot` are direct local test APIs.
Receipt verification runs after commit in a separate read-only REPEATABLE READ
transaction. It checks receipt version, record revision/fingerprint, typed inputs,
and current summaries. A lost post-commit response is recovered using the exact
request ID. Missing receipts remain unknown. Snapshot pages are at most 200 rows
per entity, with workspace version, total counts, and continuation cursors. Separate
pages can observe newer versions; a future UI must restart or reconcile if the
version changes. No browser transport contract is activated by this prototype.

### Verification checkpoint

- All prototype tests: **92 passed** on actual disposable PostgreSQL **16.15**;
  28 original task tests, 26 domain tests, 12 summary tests, and 26 ledger tests.
- Full repository Ruff and widget behavioral checks: passed.
- Existing JavaScript runtime/Apps Script tests: **42 passed**.
- Independent HEAVY review: **PASS**, no remaining blocking findings. It separately
  reran all 92 prototype tests and reproduced the Unicode source index, null work
  currency, overflow, competing decision, fingerprint, and version rollback cases.
- `fsync=on` and `synchronous_commit=on` were observed; the ledger rejects disabled
  fsync at schema initialization and disabled synchronous commit on write entry.

Test logs and full repository regression outcome are recorded in
`LOCAL_VALIDATION.md` beside this file. The response-loss and rollback tests are
injected failures around actual transactions, not power-loss/network durability
proof. No real owner approval UI, importer, production data, mirror worker,
backup/restore, performance SLO, live session, or provider behavior is claimed.

### Reproduce the extension

Use a new container name and an unused loopback port; this example intentionally
requires an already cached image and offline Python dependencies. The marker and
database prefix are mandatory. Never use an existing application database.

```powershell
docker run --detach --rm --pull=never --name assaf-os-seven-local-replay --memory 512m --cpus 1 `
  --label codex.task=assaf-os-seven-local-replay `
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=assaf_os_local_replay `
  -p 127.0.0.1:56197:5432 postgres:16
docker exec assaf-os-seven-local-replay pg_isready -U postgres -d assaf_os_local_replay
docker exec assaf-os-seven-local-replay psql -U postgres -d assaf_os_local_replay -v ON_ERROR_STOP=1 `
  -c "ALTER DATABASE assaf_os_local_replay SET assaf_os.local_prototype = 'disposable_only'"
$env:MIA_ENV='test'
$env:MIA_OS_LOCAL_PROTOTYPE_URL='postgresql+psycopg://postgres@127.0.0.1:56197/assaf_os_local_replay'
uv run --offline pytest tests/prototypes --basetemp=.cache/seven-local-replay > .cache/seven-local-replay.log 2>&1
uv run --offline ruff check app tests
node tests/unit/widget_behavior.test.js
```

After verifying that exact container's name, ID, image and task label, stop only
that disposable container. Its `--rm` storage contains synthetic data only.

## Original task slice

Base: verified master/production `459b945a2cb947b066fd3fab8f0fcf8425db40b3`.
Branch: `codex/os-primary-local-prototype`. This is not a production implementation.

`app/prototypes/assaf_os_primary.py` has separate SQLAlchemy metadata and requires explicit
`enabled=True`, a loopback PostgreSQL URL with database prefix `assaf_os_local_`, and a
disposable-server marker before schema initialization. It never reads application settings
or `.env`, registers routes/migrations/models/health checks, or creates provider clients.
The six prototype tables exist only in the disposable local database.

It handles standalone task inputs, exact ten-minute owner-bound proposals, stable approve/reject
IDs, per-workspace transaction serialization, revision/input fingerprint checks, and one atomic
task/terminal receipt/audit/outbox commit. A separate read-only REPEATABLE READ session verifies
primary state after COMMIT. Proposal replies are detached copies; stored envelopes are hashed.
Duplicate decisions return the existing receipt. Unknown receipt reads remain unknown.
No automatic retry or mirror publisher exists; committed outbox entries stay pending.

The injected `LocalPrincipal` is test context, not an authentication proof. Real OS session/CSRF,
private Telegram callback, policy/kill-switch and existing approval-envelope integration are
deliberately not wired. Client/work references are rejected, because those primary tables do
not exist in this slice. Task enums match the existing schema; due dates serialize as ISO dates.
Workspace epochs are checked, but no cutover, restore or epoch-change workflow is implemented.

## Local reproduction

Only use an existing cached image; `--pull=never` is intentional. Use a new uniquely named
container and an unused loopback port. Trust authentication is for synthetic loopback data only.
Do not point this at any existing database or use real credentials.

```powershell
docker run --detach --rm --pull=never --name assaf-os-local-proto-test --memory 512m --cpus 1 `
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=assaf_os_local_slice `
  -p 127.0.0.1:56195:5432 postgres:16
docker exec assaf-os-local-proto-test pg_isready -U postgres -d assaf_os_local_slice
docker exec assaf-os-local-proto-test psql -U postgres -d assaf_os_local_slice -v ON_ERROR_STOP=1 `
  -c "ALTER DATABASE assaf_os_local_slice SET assaf_os.local_prototype = 'disposable_only'"
$env:MIA_OS_LOCAL_PROTOTYPE_URL='postgresql+psycopg://postgres@127.0.0.1:56195/assaf_os_local_slice'
python -m pytest tests/prototypes/test_assaf_os_primary.py --basetemp=.cache/prototype-pytest
python -m ruff check app/prototypes tests/prototypes
```

After verifying the exact container identity, stop only your disposable container. Its `--rm`
configuration removes its synthetic storage. Do not stop Docker Desktop or unrelated containers.

## Evidence and limits

Final suite contains 28 tests on actual PostgreSQL16, including real concurrent connections
for two-channel approve and approve-vs-reject races. The rollback test injects failure after
flushing task/receipt/audit but before outbox; all changes and proposal consumption roll back.
The lost-response test raises after an actual commit and recovers the exact receipt without
another task/event/outbox. It is not a real network interruption or power-loss durability test.
The missing/malformed-principal regression refuses all public data operations before DB checkout,
including a receipt read with no owner and dataclass instances containing unhashable field values.

No browser benchmark, four-second production SLO, live owner authentication, actual approval
UI, full financial/refund/import semantics, Sheet mirror, backup/restore, schema migration,
production integration or effect delivery is claimed. Existing test-framework deprecation
warnings are unrelated to this slice; no dependency upgrade or installation is part of it.
