# Local PostgreSQL transaction prototypes

## Creation-request lookup (read-only recovery), local only

Branch `codex/os-primary-creation-recovery-local`, from export-preparation commit
`ef161ae`. A Save whose creation acknowledgement was lost is recovered by reading,
never by replaying the POST or minting a new request ID.

- `LocalLedgerPrimary.creation_request(principal, request_id)` runs in a REPEATABLE
  READ, `SET TRANSACTION READ ONLY` transaction with no row locks. It re-verifies the
  workspace binding and summaries, plus the proposal's owner, epoch and envelope
  digest. It returns the immutable proposal envelope with its stored status,
  `past_expiry` (pending only; `None` once decided) and an exact decision pointer
  `{request_id, status}`. The pointer comes from the receipt in the same snapshot.
  - Allowed decision IDs per terminal status: committed/target_changed → `approval_id`;
    rejected → `rejection_id`; expired → either.
  - A pending proposal with a receipt, a terminal proposal without its exact receipt,
    or an epoch/version disagreement fails closed as `primary_verification_failed`.
  - A missing row is `unknown`. That means "not proven", never "never happened".
- `LocalPrimaryAdapter.creation_request(request_id)` validates the UUID before
  authorization. It takes a fresh `assaf-os:read` context, so it still works while
  mutation policy and browser verification are closed, and still enforces session
  expiry, scope, numeric owner, workspace and epoch. It re-checks the response shape
  and per-status decision IDs, and maps verification or unexpected failures to
  `AdapterError('inconclusive', request_id=<exact UUID>)`.
- Reads never expire, consume or transition a proposal, and never write
  receipts/audit/outbox/epoch. A pending proposal past its expiry is reported as-is;
  only an explicit decision records `expired`. The pointer is not commit proof:
  verified truth still comes from `receipt(decision_id)`.
- Decision IDs are returned to any owner channel that holds read scope (OS, MIA,
  TELEGRAM_APPROVAL). This widens visibility only. `decide` still requires approve
  scope, open mutation policy, OS browser verification, a non-MIA channel and an
  explicit owner decision.
- Still not done: no route, no browser/MIA transport, no server session/CSRF
  integration, no activation.

Evidence on disposable loopback PostgreSQL 16 (`assaf_os_local_creation_lookup`, port
56211). States are `LOCAL_TESTED` only:
- **Focused suite:** `tests/prototypes/test_assaf_os_ledger.py` +
  `test_assaf_os_adapter.py`. 20 new or extended tests were red before implementation;
  the round-1 review then added more. The final focused count is recorded in the
  handoff.
- **Mutation check:** six deliberate breakages, each caught: the decision-ID map,
  receipt version, receipt epoch, removed READ ONLY, the adapter's ID map, and a
  read-side expiry write.
- **Concurrency:** a real-PG test holds a lookup snapshot open while a decision commits
  unblocked, then sees the pre-decision state, never a mix.
- **Independent review:** round 1 returned PASS_WITH_NITS; its fixes are in `ecec72e`.

## Adapter and synthetic import-shadow milestone

The next isolated branch, `codex/os-primary-shadow-adapter-local`, starts from the
reviewed seven-entity commit `5fcce60abb44c1c770f0650dda3a134b2ea38cc0`. Its bounded
scope, ownership and gates are in `STAGE2_MILESTONE_PLAN.md`; final evidence and
benchmark limits are in `STAGE2_VALIDATION.md`. Both prior prototype worktrees remain
intact. This milestone has no runtime registration, provider access, live route,
production migration, real source loader, authority switch or external mirror.

`app/prototypes/assaf_os_adapter.py` is an explicitly disabled-by-default facade
over `LocalLedgerPrimary`. An injected server authorization callback must return a
fresh `TrustedOwnerContext` for every operation: numeric owner, workspace, authority
epoch, channel, exact scopes, aware expiry, mutation policy and browser verification
facts. They cannot come from a client payload. The current context is synthetic
contract input, not authentication proof. Real sessions, Telegram callbacks,
Origin/CSRF, source revalidation and production policy integration remain later work.

The facade exposes `contract`, `snapshot`, `propose`, `decide`, `receipt` and
`creation_request` (see the section above).
It uses the existing `assaf-os:read`, `assaf-os:propose`, `assaf-os:approve` scopes.
MIA may read/propose but cannot decide. Approval/rejection requires an explicit
owner decision; the adapter and ledger default-off gates both remain active.
Contract version is `assaf-os.primary-local.v1`, with honest PostgreSQL source,
workspace/epoch/version fields and a separate pending mirror status. Proposal
creation is not a verified business commit. Lost acknowledgement produces sanitized
`AdapterError('inconclusive', decision_id=<exact UUID>)`; recover by reading that
same receipt. Do not retry automatically or mint another ID. Receipt recovery can
continue under an authorized read scope after mutation policy closes. Exact proposal
replay also survives expiry of an unchanged outer approval deadline.

`app/prototypes/assaf_os_shadow_import.py` has independent `ShadowBase` tables and
only staging/read/reconciliation APIs. It cannot expose inherited business
transaction methods or write business rows, approval receipts or outbox events.
`initialize_local_schema`, `stage`, `read_shadow` and `reconcile` retain the default-off,
numeric-owner, loopback database prefix and disposable marker boundaries. The
`synthetic:` source namespace labels fixtures; it does not prove that arbitrary
user data is synthetic, and this module provides no file or real-export loader.

Synthetic batch headers bind schema version, source ID/epoch, workspace/authority
epoch, observation time, UUID and seven-entity coverage. Each original row retains
its ID/revision and source/accepted input digests, accepted revision, synthetic native
acceptance kind and source snapshot version. No human approval is fabricated.
The fixed fixture in `assaf_os_synthetic.py` includes all seven entities, legacy-form
IDs, two currencies, a refund, an estimated expense excluded from paid cash, and
missing-time hourly nulls.

Staging is bounded to 2,000 rows and 16 MiB JSON per batch, with depth/node/header
integer limits checked before database checkout. Safely representable invalid rows
and provenance stay inspectable with quarantine reason codes; non-JSON, NUL or
oversized headers/payloads are rejected before staging. Chunk rows, provenance,
issues and checkpoints commit atomically. A crash after a committed chunk resumes
the exact immutable batch; changed payload under the same batch ID is refused.
Empty batches complete with an explicit zero-row checkpoint.

Resume, reads and reconciliation verify the immutable original payload, stored
headers, row/provenance hashes and counts, exact checkpoint ordinals, and authority
epoch. Chunk/reconciliation transactions hold workspace locks; read-only shadow
reads use REPEATABLE READ. Corruption is refused rather than accepted. Reconciliation
exposes counts, IDs/revisions, input/coverage hashes, required references, per-currency
money/refunds, deterministic summaries, issues and manifest digest. Promoted leads
require their client; unpromoted lead identities can legitimately precede a client.
Unknown/partial coverage remains visible and blocks acceptance.

`status='accepted'` means the synthetic shadow batch passed local validation. It is
not an owner business approval, production import, authority activation or backup.
`read_shadow` returns raw staged rows, including safely stored quarantined rows;
consumers must check the reconciliation manifest before using them as accepted input.
Production export signatures, historical approvals/receipts/replay journals and raw
Sheet/formula/header evidence are outside this synthetic milestone.

`scripts/benchmark_assaf_os_local.py --run-synthetic --samples 15` measures bounded
sequential synthetic adapter writes through fresh verification, first-page snapshots,
and twelve-row shadow stage/reconcile. It checks accepted manifests, exact counts
and known financial totals before counting success. Results go to the ignored
`.cache/stage2-benchmark.json`; the final reviewed sample is saved beside the
validation report. It excludes human think time and environment setup, uses a
synthetic authorization callback, and measures no browser, WAN, live authentication,
mirror delivery or production SLO. Reproduce only with the disposable local setup
below and cached dependencies.

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
