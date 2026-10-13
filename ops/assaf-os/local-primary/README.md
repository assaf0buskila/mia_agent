# Local PostgreSQL task transaction prototype

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
