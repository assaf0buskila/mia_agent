# Local rehearsal bootstrap

Preparation for V2-35a/V2-35 only. The guard and launcher do not implement V2-31,
make Google calls, migrate schemas, grant roles, change authority or enable jobs.
Complete [the owner checklist](../../../docs/runbooks/OS_REHEARSAL_ENV.md) first.

## Files and commands

`local/rehearsal.env` and `local/production-pins.json` must exist, be untracked and
be git-ignored by the adjacent `.gitignore`. Use only this directory for these
files. Never use `.env`. The pins JSON has three nonempty string arrays:
`script_urls`, `deployment_ids`, `workbook_ids`. Fill them locally from current
production identifiers; no secrets belong in the pins file. The example env
contains names only. Empty optional assignments are omitted by the parser.

From the repository root, substitute the same full 40-hex release SHA in each:

```text
uv run --offline python ops/assaf-os/rehearsal/rehearsal.py check --sha <sha>
uv run --offline python ops/assaf-os/rehearsal/rehearsal.py prepare --sha <sha>
```

`check` reads config and performs local git ignore checks only. `prepare` checks
that the commit exists locally, connects to PG and requires major version 16,
then creates `local/worktrees/<sha>` with `git worktree add --detach`, LF checkout.
It refuses an existing dirty or attached tree. It never fetches, resets, cleans,
stashes or commits. A failed create may leave a directory/registration for manual
inspection; it never silently removes it. The builder's sandbox cannot write
`.git`, so actual worktree creation is **owner-run and not exercised here**.

To create PG instead of using the coordinator's container, append `--start-pg` to
`prepare`. This requires a locally cached `postgres:16` image and a local Unix
socket or Windows named-pipe Docker context. It refuses `DOCKER_HOST` overrides
and remote contexts; it never pulls an image. The exact bootstrap DSN is:

```text
postgresql+psycopg://postgres@127.0.0.1:56217/rehearsal_test
```

The created container is `assaf-os-rehearsal-<first-12-sha-characters>`, publishes
only `127.0.0.1:56217`, uses trust authentication for this disposable bootstrap,
has tmpfs storage, and is removed when stopped. It has no persistent bind mount.
If the port/name is occupied it refuses; omit the flag to use a coordinator-owned
PG16 after confirming it is disposable. Never point that port at a production
tunnel. A loopback address alone cannot prove the database's provenance. Failure
after container creation leaves it for recorded owner teardown, not automatic
deletion of an uncertain resource.

`prepare` prints the exact PowerShell/POSIX invocation of `run`, environment
**names** and the actual uvicorn entrypoint. `run` reloads both files, repeats the
guard, PG16, detached SHA and clean-tree checks, then runs in that tree:

```text
uv run --offline --frozen uvicorn app.main:app --host 127.0.0.1 --port 58000
```

Before invoking `run`, prepare that SHA's dependencies in its detached tree with
`uv sync --offline --frozen --cache-dir <absolute-path-to-rehearsal/local/uv-cache>`.
Preseed that cache with the reviewed release dependencies through the coordinator;
an empty cache/new tree may fail offline. Do not install or fetch implicitly.
The launcher itself requires Python 3.12+ and the repo's installed `psycopg`.

The child receives only the allowlisted MIA values from `rehearsal.env`, plus
`MIA_ENV=test`, `MIA_BUILD_SHA=<sha>`, `MIA_ASSAF_OS_ENABLED=true` and
`MIA_AUTOMATION_MODE=off`. Inherited MIA credentials, PostgreSQL overrides and
Python module path/venv overrides are removed. `MIA_ENV=test` disables dotenv
loading in `app/core/config.py:31`. No production bot, model, Composio or cloud
credential is enabled by this helper. The main app's own stdout remains its
normal reason-code logging; the helper never prints config values, provider
errors, DSNs or the HMAC.

This bootstrap's `postgres` login is **not** an acceptable runtime role for a full
rehearsal. The integrated release must apply §18's role separation and validators
on the disposable database before T1-T7. The optional ledger/session/read/action
DSNs are individually guarded for loopback and the same database target.
The launcher intentionally does not accept arbitrary feature flags or retention
credentials. Use V2-31's reviewed isolated harness for authority phases, owner
authentication, mirror, freeze flags and privileged jobs; retention credentials
belong only in a job-specific local file. A local uvicorn process alone is not the
GCP `mia restore`/`mia settings` harness. See the rehearsal plan's integration gate.

## Guard outcomes

Every refusal prints `reason=<code>` to stderr and returns exit 2 before Docker,
PG or worktree mutation for a bad config. Success returns 0. URL parsing accepts
only the canonical Google `/macros/s/<id>/exec` form, without query/fragment.
URL-derived deployment IDs are checked against both production ID pins and IDs
derived from production URL pins, preventing an unrelated metadata ID bypass.
All pins must be present; their currency and correctness require owner evidence.

| Input | Expected reason / result |
|---|---|
| Production Script URL | `production_script_url` |
| Production deployment ID, supplied or URL-derived | `production_deployment` |
| Production workbook ID | `production_workbook` |
| Non-loopback host, including on an optional DSN | `dsn_not_loopback` |
| DSN query override or malformed DSN | `dsn_invalid` |
| Optional DSN for a different local database/port | `dsn_target_mismatch` |
| MODE=LIVE or FENCED | `mode_not_test` |
| Missing production pin set | `pins_incomplete` |
| Unknown config key | `config_unknown_key` |
| Valid TEST config, different pins, loopback DSNs | `rehearsal_guard_passed`, exit 0 |

No network is used by `tests/ops/test_rehearsal.py`. There is no PG integration
test in this preparation: mocked orchestration is not PG evidence. Any later PG
test must use the coordinator's exact disposable DSN above and skip explicitly
with `rehearsal_pg_unavailable` if it is down, never fall back to another DB.

For teardown, confirm the recorded container name and
`assaf-os.disposable=rehearsal` label, then `docker stop <recorded-name>`.
For a coordinator-owned container, return teardown ownership to the coordinator.
Follow the owner guide for deployment archival, copy deletion and private data.
