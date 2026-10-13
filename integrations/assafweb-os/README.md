# Frozen mobile adapter — local integration

This Node module serves the existing six frozen mobile assets and forwards the
opaque Mia owner cookie. No frontend asset is copied, edited or rebuilt.
No provider credential belongs in Node or the browser. Both Mia feature gates
default off; browser action forwarding remains blocked even in action-enabled tests.

Import `createServer` from `bff.mjs` with:
- `origin`: exact HTTPS app origin, no trailing slash.
- `backend`: Mia HTTPS origin, no path/query/credentials.
- `frontendRoot`: absolute path to `prototypes/assafweb-os-mobile`.
- `telegramBotUsername`, `telegramBotId`: public bot identifiers.
- `localTest`: false normally; true allows only loopback HTTP for disposable tests.

Bind the server to a private loopback listener behind the approved TLS proxy.
Keep Host/Origin/Fetch Metadata intact. Never copy an arbitrary forwarded Origin
into the configured origin, terminate cookies on a different host, or expose Node
without HTTPS. Real proxy/Safari acceptance remains pending.

## Reproduce the disposable test environment

Use unique container names and unused loopback ports. The password below is a
public synthetic local fixture, never a real credential. These commands create
no host-mounted data or production connection.

```powershell
docker run --detach --name mia-owner-os-local-test --publish 127.0.0.1:55439:5432 --env POSTGRES_PASSWORD=owner_os_synthetic_local --env POSTGRES_DB=owner_os_test postgres:16-alpine
docker run --detach --name mia-owner-os-regression-test --publish 127.0.0.1:55440:5432 --env POSTGRES_PASSWORD=owner_os_synthetic_local --env POSTGRES_DB=mia_regression pgvector/pgvector:0.8.6-pg16-trixie
docker exec mia-owner-os-local-test pg_isready -U postgres -d owner_os_test
docker exec mia-owner-os-regression-test pg_isready -U postgres -d mia_regression
docker exec mia-owner-os-local-test psql -U postgres -d owner_os_test -c 'CREATE DATABASE quick_lead_test'
```

Wait until both readiness checks succeed, then from this backend worktree:

```powershell
$env:MIA_ENV='test'
$env:UV_CACHE_DIR=(Join-Path (Get-Location) '.cache/uv')
$env:MIA_OWNER_OS_TEST_POSTGRES_URL='postgresql+psycopg://postgres:owner_os_synthetic_local@127.0.0.1:55439/owner_os_test'
$env:MIA_TEST_POSTGRES_URL='postgresql+psycopg://postgres:owner_os_synthetic_local@127.0.0.1:55440/mia_regression'
$env:MIA_QUICK_LEAD_TEST_POSTGRES_URL='postgresql+psycopg://postgres:owner_os_synthetic_local@127.0.0.1:55439/quick_lead_test'
$env:MIA_OWNER_OS_FRONTEND_ROOT='ABSOLUTE_PATH_TO_FROZEN_FRONTEND'
uv sync --frozen --group dev
uv run --no-sync pytest --basetemp=.cache/owner-os-unique --junitxml=.cache/owner-os-tests.xml *> .cache/owner-os-tests.txt
Get-Content .cache/owner-os-tests.txt -Tail 35
uv run --no-sync ruff check app tests
node tests/unit/widget_behavior.test.js
node --test "$env:MIA_OWNER_OS_FRONTEND_ROOT/tests/*.test.mjs"
```

The owner fixture refuses non-loopback hosts and databases other than
`owner_os_test`. It creates isolated random schemas and NOINHERIT auth/read/action
roles; migration twice; synthetic contacts/activities/reminders; valid and
adversarial synthetic Telegram proofs. It cleans up its schemas/roles.
The existing full regressions require pgvector, so they use a separate disposable
database. Owner OS tests intentionally prove a database with no public executable
application routines; the production effective ACL posture must be checked
separately. Adding pgvector here is a regression dependency, not a new OS feature.

The actual frozen `OwnerClient` and DTO validators run through Node BFF -> FastAPI
-> PostgreSQL. The harness emulates TLS termination over loopback; it does not
claim a real browser's Secure-cookie behavior or a live Telegram login.

After verification remove only the two containers you created by their exact names:
```powershell
docker rm -f mia-owner-os-local-test
docker rm -f mia-owner-os-regression-test
```

No migration/live deployment/feature enablement is authorized by this recipe.
See ../../docs/OWNER_OS_LOCAL_INTEGRATION.md and the threat model for rollout gates.
