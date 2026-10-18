"""Give schema assaf_os its owner and its grants back after a restore, in the DRILL (V2-10).

DRILL ONLY. The restore drill (restore_drill.py, on Assaf's PC) restores a backup into a scratch
server with `pg_restore --no-owner --no-privileges`, which leaves a restored assaf_os owned by the
restoring role with its functions back at the default ACL (EXECUTE for PUBLIC). This module repairs
that on the scratch server and proves it. It is never installed on the VM and has no command line:
`mia restore` repairs the real database through V2-15's provision_ledger.py alone
(V2_PG_CONTRACTS.md 18.1), so there is exactly one reviewed grant path in production.

  1. re-own the schema and every table, view, sequence, function and type in it to assaf_os_owner;
  2. revoke everything from PUBLIC;
  3. re-apply the reviewed grant set, which may only contain GRANT, REVOKE and ALTER DEFAULT
     PRIVILEGES statements that name assaf_os. The input is exactly the lines of V2-15's
     `ledger_grant_statements()` (see the runbook), not the provisioning plan;
  4. run a read-only posture check and fail unless it is clean.

The posture check here is a floor, not V2-15's exact grant-map validator: it catches a wrong owner,
any PUBLIC grant, any grantee other than the owner, the ledger role and the retention role (V2-14b
gives retention its own two-table grants; its exact map is the real validator's job), and DELETE or
TRUNCATE for the ledger role. The VM step runs V2-15's exact validator for real.

A database without the schema passes and nothing is changed. A database with the schema and no grant
file fails: the posture can not be proven. Standard library only. The statements are the only
interface to PostgreSQL, so the caller decides which server they reach. Roles are created only when
the caller asks, and only the drill asks.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

import backup_lib
from backup_lib import BackupError

SCHEMA = "assaf_os"
OWNER_ROLE = "assaf_os_owner"
LEDGER_ROLE = "assaf_os_ledger"
RETENTION_ROLE = "assaf_os_retention"
POSTURE_CHECKS = (
    "schema_owner",
    "object_owner",
    "public_grants",
    "other_grantees",
    "ledger_destructive",
)

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_ALLOWED_STATEMENT = re.compile(
    r"(?:GRANT|REVOKE)\s.*\sON\s|ALTER\s+DEFAULT\s+PRIVILEGES\s", re.IGNORECASE | re.DOTALL
)
_SCHEMA_WORD = re.compile(rf"\b{SCHEMA}\b", re.IGNORECASE)
_FORBIDDEN_CHARACTERS = re.compile(r"['\"$\\]|/\*|\*/")

RunSql = Callable[[str], backup_lib.Result]


def role_name(name: str) -> str:
    if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
        raise BackupError("ledger_role_invalid", "a role name must be a plain lowercase identifier")
    return name


# --- the grant file -------------------------------------------------------------------------


def parse_grants(text: str) -> list[str]:
    """The statements of a reviewed grant file, or `ledger_grants_invalid`.

    Only GRANT, REVOKE and ALTER DEFAULT PRIVILEGES that mention the assaf_os schema are accepted,
    and no quoting, dollar quoting, block comments or backslashes: the file is data, not a script.
    """
    cleaned = "\n".join(line.split("--", 1)[0] for line in text.splitlines())
    if _FORBIDDEN_CHARACTERS.search(cleaned):
        raise BackupError("ledger_grants_invalid", "quoting and escapes are not allowed")
    statements = [" ".join(part.split()) for part in cleaned.split(";") if part.strip()]
    if not statements:
        raise BackupError("ledger_grants_invalid", "the grant file has no statements")
    for number, statement in enumerate(statements, 1):
        if not _ALLOWED_STATEMENT.match(statement):
            raise BackupError("ledger_grants_invalid", f"statement {number} is not a grant")
        if not _SCHEMA_WORD.search(statement):
            raise BackupError("ledger_grants_invalid", f"statement {number} does not name {SCHEMA}")
    return statements


# --- SQL ------------------------------------------------------------------------------------

_REOWN = """\
DO $reown$
DECLARE item record;
BEGIN
  FOR item IN
    SELECT c.oid::regclass::text AS name, c.relkind AS kind
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'assaf_os' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
    ORDER BY c.oid
  LOOP
    EXECUTE format('ALTER %s %s OWNER TO {owner}',
      CASE item.kind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                     WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END, item.name);
  END LOOP;
  -- A sequence owned by a column follows its table. An identity sequence refuses a direct change.
  FOR item IN
    SELECT c.oid::regclass::text AS name
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'assaf_os' AND c.relkind = 'S'
      AND NOT EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                        AND d.deptype = 'i')
    ORDER BY c.oid
  LOOP
    EXECUTE format('ALTER SEQUENCE %s OWNER TO {owner}', item.name);
  END LOOP;
  FOR item IN
    SELECT p.oid::regprocedure::text AS name, p.prokind AS kind
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'assaf_os'
    ORDER BY p.oid
  LOOP
    EXECUTE format('ALTER %s %s OWNER TO {owner}',
      CASE item.kind WHEN 'p' THEN 'PROCEDURE' WHEN 'a' THEN 'AGGREGATE' ELSE 'FUNCTION' END,
      item.name);
  END LOOP;
  FOR item IN
    SELECT t.oid::regtype::text AS name, t.typtype AS kind
    FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = 'assaf_os' AND t.typtype IN ('e', 'd')
    ORDER BY t.oid
  LOOP
    EXECUTE format('ALTER %s %s OWNER TO {owner}',
      CASE item.kind WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END, item.name);
    -- Enum and domain types default to USAGE for PUBLIC.
    EXECUTE format('REVOKE ALL ON %s %s FROM PUBLIC',
      CASE item.kind WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END, item.name);
  END LOOP;
END
$reown$"""

_REVOKES = (
    "REVOKE ALL ON SCHEMA assaf_os FROM PUBLIC",
    "REVOKE ALL ON ALL TABLES IN SCHEMA assaf_os FROM PUBLIC",
    "REVOKE ALL ON ALL SEQUENCES IN SCHEMA assaf_os FROM PUBLIC",
    "REVOKE ALL ON ALL ROUTINES IN SCHEMA assaf_os FROM PUBLIC",
)

_ENSURE_ROLES = """\
DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{owner}') THEN
    CREATE ROLE {owner} NOLOGIN NOSUPERUSER;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ledger}') THEN
    CREATE ROLE {ledger} NOLOGIN NOINHERIT NOSUPERUSER;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{retention}') THEN
    CREATE ROLE {retention} NOLOGIN NOINHERIT NOSUPERUSER;
  END IF;
END
$roles$"""

_POSTURE = """\
WITH owner_role AS (SELECT oid FROM pg_roles WHERE rolname = '{owner}'),
ledger_role AS (SELECT oid FROM pg_roles WHERE rolname = '{ledger}'),
retention_role AS (SELECT oid FROM pg_roles WHERE rolname = '{retention}'),
acl AS (
  SELECT 'schema'::text AS kind, a.grantee, a.privilege_type::text AS privilege_type
  FROM pg_namespace n,
       LATERAL aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) a
  WHERE n.nspname = 'assaf_os'
  UNION ALL
  SELECT 'relation', a.grantee, a.privilege_type
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace,
       LATERAL aclexplode(COALESCE(c.relacl,
         CASE WHEN c.relkind = 'S' THEN acldefault('s', c.relowner)
              ELSE acldefault('r', c.relowner) END)) a
  WHERE n.nspname = 'assaf_os' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
  UNION ALL
  SELECT 'column', a.grantee, a.privilege_type
  FROM pg_attribute t JOIN pg_class c ON c.oid = t.attrelid
       JOIN pg_namespace n ON n.oid = c.relnamespace,
       LATERAL aclexplode(t.attacl) a
  WHERE n.nspname = 'assaf_os' AND t.attacl IS NOT NULL AND NOT t.attisdropped
  UNION ALL
  SELECT 'routine', a.grantee, a.privilege_type
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace,
       LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) a
  WHERE n.nspname = 'assaf_os'
  UNION ALL
  SELECT 'type', a.grantee, a.privilege_type
  FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace,
       LATERAL aclexplode(COALESCE(t.typacl, acldefault('T', t.typowner))) a
  WHERE n.nspname = 'assaf_os' AND t.typtype IN ('e', 'd')
)
SELECT
  (SELECT count(*) FROM pg_namespace n
    WHERE n.nspname = 'assaf_os'
      AND n.nspowner IS DISTINCT FROM (SELECT oid FROM owner_role)) AS schema_owner,
  ((SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'assaf_os' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
       AND c.relowner IS DISTINCT FROM (SELECT oid FROM owner_role))
   + (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
       WHERE n.nspname = 'assaf_os'
         AND p.proowner IS DISTINCT FROM (SELECT oid FROM owner_role))
   + (SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
       WHERE n.nspname = 'assaf_os' AND t.typtype IN ('e', 'd')
         AND t.typowner IS DISTINCT FROM (SELECT oid FROM owner_role))) AS object_owner,
  (SELECT count(*) FROM acl WHERE grantee = 0) AS public_grants,
  (SELECT count(*) FROM acl
    WHERE grantee <> 0
      AND grantee NOT IN (SELECT oid FROM owner_role UNION SELECT oid FROM ledger_role
                          UNION SELECT oid FROM retention_role))
    AS other_grantees,
  (SELECT count(*) FROM acl
    WHERE kind = 'relation' AND privilege_type IN ('DELETE', 'TRUNCATE')
      AND grantee IN (SELECT oid FROM ledger_role)) AS ledger_destructive"""


def schema_present_sql() -> str:
    return f"SELECT count(*) FROM pg_namespace WHERE nspname = '{SCHEMA}'"


def ensure_roles_sql(
    owner: str = OWNER_ROLE, ledger: str = LEDGER_ROLE, retention: str = RETENTION_ROLE
) -> str:
    return (
        _ENSURE_ROLES.replace("{owner}", role_name(owner))
        .replace("{ledger}", role_name(ledger))
        .replace("{retention}", role_name(retention))
    )


def apply_sql(grants: list[str], owner: str = OWNER_ROLE) -> str:
    """One string, so psql -c runs it as one transaction: it all happens or none of it does."""
    owner = role_name(owner)
    statements = [
        f"ALTER SCHEMA {SCHEMA} OWNER TO {owner}",
        _REOWN.replace("{owner}", owner),
        *_REVOKES,
        *grants,
    ]
    return ";\n".join(statements) + ";\n"


def posture_sql(
    owner: str = OWNER_ROLE, ledger: str = LEDGER_ROLE, retention: str = RETENTION_ROLE
) -> str:
    return (
        _POSTURE.replace("{owner}", role_name(owner))
        .replace("{ledger}", role_name(ledger))
        .replace("{retention}", role_name(retention))
    )


def parse_posture(output: str) -> dict[str, int]:
    parts = output.strip().split("|")
    if len(parts) != len(POSTURE_CHECKS) or not all(part.isdigit() for part in parts):
        raise BackupError("ledger_posture_unreadable", "the posture query did not return counts")
    return dict(zip(POSTURE_CHECKS, (int(part) for part in parts), strict=True))


# --- the check ------------------------------------------------------------------------------


@dataclass
class LedgerOutcome:
    present: bool = False
    checks: list[dict] = field(default_factory=list)
    violations: dict[str, int] = field(default_factory=dict)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "ok": ok, "detail": detail})
        return ok

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check["ok"] for check in self.checks)

    def as_report(self) -> dict:
        return {"schema_present": self.present, "violations": self.violations}


def evaluate(
    run_sql: RunSql,
    *,
    grants_text: str | None,
    apply: bool = True,
    create_roles: bool = False,
    owner: str = OWNER_ROLE,
    ledger: str = LEDGER_ROLE,
    retention: str = RETENTION_ROLE,
) -> LedgerOutcome:
    """Re-own and re-grant assaf_os when it is there, then prove the result. Fails closed."""
    outcome = LedgerOutcome()
    probe = run_sql(schema_present_sql())
    answer = probe.stdout.decode("utf-8", "replace").strip()
    if probe.returncode != 0 or answer not in ("0", "1"):
        outcome.add("ledger_schema", False, "the schema probe failed")
        return outcome
    outcome.present = answer == "1"
    outcome.add("ledger_schema", True, "present" if outcome.present else "absent")
    if not outcome.present:
        return outcome

    if apply:
        if grants_text is None:
            outcome.add("ledger_grants", False, "grants_file_required")
            return outcome
        try:
            grants = parse_grants(grants_text)
        except BackupError as error:
            outcome.add("ledger_grants", False, error.detail or error.code)
            return outcome
        outcome.add("ledger_grants", True, f"{len(grants)} statements")
        if create_roles:
            created = run_sql(ensure_roles_sql(owner, ledger, retention))
            if not outcome.add(
                "ledger_roles",
                created.returncode == 0,
                "" if created.returncode == 0 else backup_lib.tail(created.stderr),
            ):
                return outcome
        done = run_sql(apply_sql(grants, owner))
        if not outcome.add(
            "ledger_reown_regrant",
            done.returncode == 0,
            "" if done.returncode == 0 else backup_lib.tail(done.stderr),
        ):
            return outcome

    answer = run_sql(posture_sql(owner, ledger, retention))
    try:
        if answer.returncode != 0:
            raise BackupError("ledger_posture_unreadable", "the posture query failed")
        outcome.violations = parse_posture(answer.stdout.decode("utf-8", "replace"))
    except BackupError as error:
        outcome.add("ledger_posture", False, error.code)
        return outcome
    failing = sorted(name for name, count in outcome.violations.items() if count)
    outcome.add(
        "ledger_posture",
        not failing,
        "clean" if not failing else ", ".join(f"{n}={outcome.violations[n]}" for n in failing),
    )
    return outcome
