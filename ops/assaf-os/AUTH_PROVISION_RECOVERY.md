# Private session provisioning on existing MIA

The preflight is read-only. `python ops/assaf-os/provision_auth.py --apply` creates
only database `assaf_os_auth` and role `assaf_os_session` inside the existing
`mia-postgres-1` container. It does not create a server, expose PostgreSQL, read
an .env file, change CRM permissions or enable any feature. Five opaque technical
session tables are separate from the business workbook. All provider settings
are preserved. The settings baseline is checked immediately before publication
and read back after publication; activation requires the checked version and no
concurrent settings change. Secret Manager offers no compare-and-swap for adding
a version, so concurrent secret editing must stop during this short operation.

The exact production `_validate_runtime_role` SELECT queries run under
`SET LOCAL ROLE assaf_os_session`; this proves the actual effective grants rather
than inferring them from table existence. After backend release, also check the
real auth challenge/session flow using the dedicated login DSN.

If apply is interrupted, do not rerun or rotate credentials blindly. Feature
gates remain false, and the live MIA process has not reloaded settings. Keep the
new Secret Manager version: it contains the generated role password and HMAC.
Inspect only the two pinned catalog targets and five session tables. Recover
with the existing saved credentials, without touching database `mia`:

1. If neither catalog target exists, recover the role password from the existing
   dedicated session DSN internally, then perform the pinned CREATE ROLE/CREATE
   DATABASE statements in `provision_auth.py`; do not generate a replacement.
2. If only the role exists, verify its NOINHERIT/non-superuser flags, then create
   the pinned technical database owned by `mia` and apply the database grants.
3. If both exist, stop if any unrelated object or different ownership exists.
   Apply the idempotent session migration and exact grants in one transaction.
4. Run `verify_role_posture()` and the real auth flow before enabling the gates.

No automated deletion/reset is a recovery step. If review requires rollback,
leave the gates disabled and restore only the previously recorded OS keys while
preserving any subsequent unrelated settings changes. Unused technical objects
may remain until separately reviewed; they add no VM or managed database bill.
