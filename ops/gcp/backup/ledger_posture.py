"""Give schema assaf_os its owner and its grants back after a restore (V2-10).

`mia restore` and the restore drill run pg_restore with --no-owner --no-privileges. A restored
assaf_os therefore belongs to the restoring role and its functions fall back to the default ACL
(EXECUTE for PUBLIC). The ledger job then refuses to run. This module repairs that and proves it:

  1. re-own the schema and every table, view, sequence, function and type in it to assaf_os_owner;
  2. revoke everything from PUBLIC;
  3. re-apply the reviewed grant set (the file V2-15's provision_ledger.py prints), which may only
     contain GRANT, REVOKE and ALTER DEFAULT PRIVILEGES statements that name assaf_os;
  4. run a read-only posture check and fail unless it is clean.

The posture check here is a floor, not V2-15's exact grant-map validator: it catches a wrong owner,
any PUBLIC grant, any grantee other than the two ledger roles, and DELETE or TRUNCATE for the ledger
role. When V2-15 lands, its validator should be run in addition (see the runbook).

A database without the schema passes and nothing is changed. A database with the schema and no grant
file fails: the posture can not be proven. Standard library only. The statements are the only
interface to PostgreSQL, so the same code runs against the VM (compose exec) and the drill's scratch
server. Roles are never created here unless the caller asks; the drill asks, production never does.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import backup_lib
from backup_lib import BackupError

SCHEMA = "assaf_os"
OWNER_ROLE = "assaf_os_owner"
LEDGER_ROLE = "assaf_os_ledger"
POSTURE_CHECKS = (
    "schema_owner",
    "object_owner",
    "public_grants",
    "other_grantees",
    "ledger_destructive",
)

COMPOSE_FILE = "/opt/mia/compose.yaml"
TAG_ENV = "/opt/mia/tag.env"

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
END
$roles$"""

_POSTURE = """\
WITH owner_role AS (SELECT oid FROM pg_roles WHERE rolname = '{owner}'),
ledger_role AS (SELECT oid FROM pg_roles WHERE rolname = '{ledger}'),
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
      AND grantee NOT IN (SELECT oid FROM owner_role UNION SELECT oid FROM ledger_role))
    AS other_grantees,
  (SELECT count(*) FROM acl
    WHERE kind = 'relation' AND privilege_type IN ('DELETE', 'TRUNCATE')
      AND grantee IN (SELECT oid FROM ledger_role)) AS ledger_destructive"""


def schema_present_sql() -> str:
    return f"SELECT count(*) FROM pg_namespace WHERE nspname = '{SCHEMA}'"


def ensure_roles_sql(owner: str = OWNER_ROLE, ledger: str = LEDGER_ROLE) -> str:
    return _ENSURE_ROLES.replace("{owner}", role_name(owner)).replace("{ledger}", role_name(ledger))


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


def posture_sql(owner: str = OWNER_ROLE, ledger: str = LEDGER_ROLE) -> str:
    return _POSTURE.replace("{owner}", role_name(owner)).replace("{ledger}", role_name(ledger))


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
            created = run_sql(ensure_roles_sql(owner, ledger))
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

    answer = run_sql(posture_sql(owner, ledger))
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


# --- the production restore path ---------------------------------------------------------------


def vm_run_sql(
    runner, docker: str, compose_file: str, tag_env: str, user: str, database: str
) -> RunSql:
    """psql inside the compose postgres container, the same door `mia backup` uses."""
    base = [
        docker,
        "compose",
        "-p",
        "mia",
        "-f",
        compose_file,
        "--env-file",
        tag_env,
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        user,
        "-d",
        database,
        "-v",
        "ON_ERROR_STOP=1",
        "-X",
        "-At",
    ]
    return lambda sql: runner.run([*base, "-c", sql], timeout=300)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Re-own and re-grant schema assaf_os on the VM's database after a restore."
    )
    parser.add_argument(
        "command",
        choices=("plan", "apply", "verify"),
        help="plan prints the SQL; apply changes the database; verify only reads",
    )
    parser.add_argument(
        "--grants-file",
        type=Path,
        help="the reviewed V2-15 GRANT set (required for plan with grants and apply)",
    )
    parser.add_argument("--compose-file", default=COMPOSE_FILE)
    parser.add_argument("--tag-env", default=TAG_ENV)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--db-user", default="mia")
    parser.add_argument("--database", default="mia")
    return parser


def read_grants(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise BackupError("ledger_grants_invalid", f"cannot read {path.name}") from error


def main(argv=None, *, runner=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "apply" and args.grants_file is None:
        parser.error("apply needs --grants-file")
    runner = runner if runner is not None else backup_lib.Runner()

    grants_text = None
    try:
        if args.grants_file is not None:
            grants_text = read_grants(args.grants_file)
            grants = parse_grants(grants_text)
        else:
            grants = []
    except BackupError as error:
        print(
            f"mia-ledger-posture: refused code={error.code} detail={error.detail}", file=sys.stderr
        )
        return 2

    if args.command == "plan":
        print(
            f"-- apply\n{apply_sql(grants)}\n-- then verify (must return only zeros)\n"
            f"{posture_sql()};"
        )
        return 0

    run_sql = vm_run_sql(
        runner, args.docker, args.compose_file, args.tag_env, args.db_user, args.database
    )
    outcome = evaluate(run_sql, grants_text=grants_text, apply=args.command == "apply")
    report = {"passed": outcome.passed, "checks": outcome.checks, **outcome.as_report()}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if outcome.passed else 1


if __name__ == "__main__":
    sys.exit(main())
