"""Exact V2-14 PostgreSQL grant map and its fail-closed catalog validator.

Nothing connects on import. validate_ledger_role is the V2_PG_CONTRACTS.md §5.5
preflight (first connection, every 10 minutes and after a reconnect; never per
request). validate_ledger_catalog is the operator transaction hook used by
provisioning. V2-14b extends the map for its seven tables and makes retention a
LOGIN NOINHERIT role with exactly its §18.2 purge rights. No default privileges are
granted for future tables.
"""

from collections.abc import Mapping
from types import MappingProxyType

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.os_ledger_migrate import OWNER_ROLE, RUNTIME_ROLE, TABLES, _validate_main_role_catalog

RETENTION_ROLE = "assaf_os_retention"
DENIED = "ledger_role_denied"
# pg_temp last: a session's temporary relation can never shadow a catalog name
# (left out, pg_temp is searched FIRST for relations). Functions never resolve there.
CATALOG_SEARCH_PATH = "SET LOCAL search_path = pg_catalog, pg_temp"

TABLE_PRIVILEGES: Mapping[str, frozenset[str]] = MappingProxyType({
    name: frozenset({"SELECT"} if name == "os_authority" else {"SELECT", "INSERT"})
    for name in sorted(TABLES)
})
# §18.2: every record table has these UPDATE columns plus its own typed columns.
# Never updatable: workspace_id, record_id, created_at and the generated os_tasks.status.
RECORD_UPDATE_COLUMNS = (
    "revision", "inputs", "business_date", "source", "updated_at", "deleted_at",
    "deleted_by_request_id",
)
_R = RECORD_UPDATE_COLUMNS
COLUMN_UPDATES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "os_workspaces": ("version", "summaries", "updated_at"),
    "os_leads": (*_R, "person_id"),
    "os_clients": _R,
    "os_works": (*_R, "person_id", "agreed_amount_minor", "currency"),
    "os_payments": (*_R, "person_id", "work_id", "amount_minor", "currency", "original_payment_id"),
    "os_time": (*_R, "person_id", "work_id", "minutes"),
    "os_expenses": (*_R, "amount_minor", "currency", "source_type", "source_key", "source_digest"),
    "os_tasks": (*_R, "person_id", "work_id", "due_date"),
    "os_proposals": ("status",),
    "os_outbox": (
        "status", "attempts", "next_attempt_at", "lease_owner", "lease_generation",
        "leased_at", "last_error_code", "updated_at",
    ),
    "os_mirror_state": (
        "status", "projected_version", "verified_version", "last_attempt_at", "last_verified_at",
        "last_error_code", "watermark", "scan_coverage", "updated_at",
    ),
    "os_summary_monthly": ("version", "summary", "calculated_at"),
    "os_summary_cycle": ("version", "starts_on", "ends_on", "summary", "calculated_at"),
    "os_import_batches": ("status", "quarantine", "completed_at"),
    "os_import_rows": ("record_id", "status", "canonical_record", "quarantine"),
})
FUNCTION_PRIVILEGES = frozenset({"expense_source_key_sha256(text)"})
# The complete assaf_os inventory, owned by assaf_os_owner (provisioning re-owns it).
OWNED_TABLES = tuple(sorted(TABLES | {"schema_migrations"}))
OWNED_FUNCTIONS = (
    "expense_source_key_sha256(text)", "require_pending_assistant_proposal_insert()",
)
# The only non-internal trigger on any assaf_os relation:
# (table, trigger, function, tgenabled 'O' = fires normally, tgtype 7 = ROW|BEFORE|INSERT).
TRIGGERS = frozenset({(
    "os_proposals", "os_proposals_assistant_insert_pending",
    "assaf_os.require_pending_assistant_proposal_insert()", "O", 7,
)})
_USER_SCHEMA = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'"
)
# Every catalog object that belongs to assaf_os, as (catalog oid, object oid): the schema,
# its relations with their toast tables and toast indexes, functions, types,
# constraints, triggers, rules, column defaults, policies and extended statistics.
_INSIDE = """
WITH ns AS (SELECT oid FROM pg_namespace WHERE nspname = 'assaf_os'),
rel AS (SELECT c.oid, c.reltoastrelid FROM pg_class c WHERE c.relnamespace IN (SELECT oid FROM ns)),
allrel AS (
    SELECT oid FROM rel
    UNION SELECT reltoastrelid FROM rel WHERE reltoastrelid <> 0
    UNION SELECT i.indexrelid FROM pg_index i
          WHERE i.indrelid IN (SELECT reltoastrelid FROM rel WHERE reltoastrelid <> 0)),
inside(classid, objid) AS (
    SELECT 'pg_namespace'::regclass::oid, oid FROM ns
    UNION ALL SELECT 'pg_class'::regclass::oid, oid FROM allrel
    UNION ALL SELECT 'pg_proc'::regclass::oid, p.oid FROM pg_proc p
              WHERE p.pronamespace IN (SELECT oid FROM ns)
    UNION ALL SELECT 'pg_type'::regclass::oid, t.oid FROM pg_type t
              WHERE t.typnamespace IN (SELECT oid FROM ns)
    UNION ALL SELECT 'pg_constraint'::regclass::oid, k.oid FROM pg_constraint k
              WHERE k.connamespace IN (SELECT oid FROM ns)
    UNION ALL SELECT 'pg_trigger'::regclass::oid, g.oid FROM pg_trigger g
              WHERE g.tgrelid IN (SELECT oid FROM allrel)
    UNION ALL SELECT 'pg_rewrite'::regclass::oid, w.oid FROM pg_rewrite w
              WHERE w.ev_class IN (SELECT oid FROM allrel)
    UNION ALL SELECT 'pg_attrdef'::regclass::oid, a.oid FROM pg_attrdef a
              WHERE a.adrelid IN (SELECT oid FROM allrel)
    UNION ALL SELECT 'pg_policy'::regclass::oid, y.oid FROM pg_policy y
              WHERE y.polrelid IN (SELECT oid FROM allrel)
    UNION ALL SELECT 'pg_statistic_ext'::regclass::oid, s.oid FROM pg_statistic_ext s
              WHERE s.stxnamespace IN (SELECT oid FROM ns))
"""
# initdb-created objects (OID < FirstNormalObjectId, e.g. the plpgsql language) are
# the only things outside assaf_os that an assaf_os object may depend on.
_FIRST_NORMAL_OID = 16384

# --- V2-14b extension (V2_PG_CONTRACTS.md section 18.2) --------------------------------
# Kept apart from the V2-15 map above. Literal tuples only; the policy tests pin each one
# to the migration SQL's columns. TABLE_PRIVILEGES already gives every V2-14b table
# SELECT and INSERT, because os_ledger_migrate.TABLES lists them. Never updatable:
# os_workspaces.epoch and statement_pepper_fp, os_receipts.reason_codes, and every
# os_assistant_audit column.
ADDENDUM_COLUMN_UPDATES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "os_workspaces": ("pending_seq",),
    "os_statements": (
        "status", "line_count", "lines_digest", "current_proposal_id", "approved_proposal_id",
        "imported_version", "updated_at",
    ),
    # Every column except workspace_id, statement_id, line_key and created_at.
    "os_statement_lines": (
        "tag", "state", "txn_date", "merchant_display", "merchant_key", "amount_minor", "kind",
        "currency", "original_amount_minor", "original_currency", "installment_n",
        "installment_of", "category", "path", "platform", "is_subscription", "rule_id",
        "assistant_tag", "owner_override", "expense_id", "matched_expense_id",
        "expense_revision_at_import", "revision", "updated_at",
    ),
    # Every column except workspace_id, rule_id and created_at.
    "os_merchant_rules": (
        "match", "tag", "merchant_key", "merchant_key_hmac", "category", "path", "platform",
        "is_subscription", "label", "aliases", "active", "revision", "source_proposal_id",
        "updated_at",
    ),
    "os_statement_review_drafts": ("draft_revision", "overrides", "choices", "updated_at"),
    "os_statement_batch_lines": ("detail", "purged_at"),
    "os_assistant_credentials": ("revoked_at", "last_used_at", "use_count"),
})
# Retention (LOGIN NOINHERIT): schema USAGE plus exactly these. The os-retention job uses
# DELETE only for purged batch lines, batch lines whose proposal expired more than a day
# ago (the I11 backstop, whatever purged_at says), and audit rows older than 180 days.
RETENTION_TABLE_PRIVILEGES: Mapping[str, frozenset[str]] = MappingProxyType({
    "os_assistant_audit": frozenset({"SELECT", "DELETE"}),
    "os_statement_batch_lines": frozenset({"SELECT", "DELETE"}),
})
RETENTION_COLUMN_SELECTS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "os_proposals": ("workspace_id", "proposal_id", "kind", "status", "expires_at"),
})
# §22 E6: the database cannot enforce retention's row predicates (no RLS), so the
# os-retention job runs exactly these fixed statements and no other SQL as the retention
# role. The only bound parameter is :workspace_id; the windows are literals, so no caller
# can widen them. V2-26's app/workers/os_retention.py must execute these constants.
RETENTION_DELETE_SQL: Mapping[str, str] = MappingProxyType({
    # Batch lines whose detail the runtime path already purged.
    "purge": (
        "DELETE FROM assaf_os.os_statement_batch_lines "
        "WHERE workspace_id = :workspace_id AND purged_at IS NOT NULL"
    ),
    # The I11 backstop: lines of a proposal that expired more than a day ago, whatever
    # purged_at says.
    "backstop": (
        "DELETE FROM assaf_os.os_statement_batch_lines AS b USING assaf_os.os_proposals AS p "
        "WHERE b.workspace_id = :workspace_id AND p.workspace_id = b.workspace_id "
        "AND p.proposal_id = b.proposal_id AND p.expires_at < now() - interval '1 day'"
    ),
    # Assistant audit rows are kept for 180 days (§17.2).
    "audit": (
        "DELETE FROM assaf_os.os_assistant_audit "
        "WHERE workspace_id = :workspace_id AND occurred_at < now() - interval '180 days'"
    ),
})


def addendum_grant_statements() -> tuple[str, ...]:
    """V2-14b's GRANTs, appended to ledger_grant_statements() after V2-15's."""
    statements = [
        f"GRANT UPDATE ({', '.join(columns)}) ON TABLE assaf_os.{name} TO assaf_os_ledger;"
        for name, columns in ADDENDUM_COLUMN_UPDATES.items()
    ]
    statements.append("GRANT USAGE ON SCHEMA assaf_os TO assaf_os_retention;")
    for name, privileges in RETENTION_TABLE_PRIVILEGES.items():
        statements.append(
            f"GRANT {', '.join(sorted(privileges))} ON TABLE assaf_os.{name} "
            "TO assaf_os_retention;"
        )
    for name, columns in RETENTION_COLUMN_SELECTS.items():
        statements.append(
            f"GRANT SELECT ({', '.join(columns)}) ON TABLE assaf_os.{name} TO assaf_os_retention;"
        )
    return tuple(statements)


def _addendum_acl() -> tuple[set[tuple], set[tuple], set[tuple]]:
    """Expected V2-14b schema, table and column ACL entries (no grant options)."""
    schema = {(RETENTION_ROLE, "USAGE", False)}
    tables = {
        (name, RETENTION_ROLE, privilege, False)
        for name, privileges in RETENTION_TABLE_PRIVILEGES.items() for privilege in privileges
    }
    columns = {
        (name, column, RUNTIME_ROLE, "UPDATE", False)
        for name, names in ADDENDUM_COLUMN_UPDATES.items() for column in names
    } | {
        (name, column, RETENTION_ROLE, "SELECT", False)
        for name, names in RETENTION_COLUMN_SELECTS.items() for column in names
    }
    return schema, tables, columns


def _retention_effective(conn: Connection) -> None:
    # Effective, not only ACL-level: PUBLIC or any other path into assaf_os also fails.
    tables = conn.execute(text("""
        SELECT c.relname, p.privilege FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE',
                                'REFERENCES','TRIGGER']) AS p(privilege)
        WHERE n.nspname='assaf_os' AND c.relkind IN ('r','p','v','m','f')
        AND has_table_privilege(:role, c.oid, p.privilege)
    """), {"role": RETENTION_ROLE}).all()
    _require({tuple(row) for row in tables} == {
        (name, privilege)
        for name, privileges in RETENTION_TABLE_PRIVILEGES.items() for privilege in privileges
    })
    columns = conn.execute(text("""
        SELECT c.relname, a.attname, p.privilege FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum > 0 AND NOT a.attisdropped
        CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','REFERENCES']) AS p(privilege)
        WHERE n.nspname='assaf_os' AND c.relkind IN ('r','p','v','m','f')
        AND has_column_privilege(:role, c.oid, a.attnum, p.privilege)
    """), {"role": RETENTION_ROLE}).all()
    _require({tuple(row) for row in columns if row[0] not in RETENTION_TABLE_PRIVILEGES} == {
        (name, column, "SELECT")
        for name, names in RETENTION_COLUMN_SELECTS.items() for column in names
    })
    _require({row[2] for row in columns if row[0] in RETENTION_TABLE_PRIVILEGES} <= {"SELECT"})
    _require(not conn.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='assaf_os' AND CASE WHEN c.relkind='S'
          THEN has_sequence_privilege(:role, c.oid, 'USAGE,SELECT,UPDATE') ELSE false END)
        OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname='assaf_os' AND has_function_privilege(:role, p.oid, 'EXECUTE'))
        OR NOT has_schema_privilege(:role, 'assaf_os', 'USAGE')
    """), {"role": RETENTION_ROLE}))


def ledger_grant_statements() -> tuple[str, ...]:
    """Return the complete, deterministic runtime GRANT set (no dynamic SQL names)."""
    statements = ["GRANT USAGE ON SCHEMA assaf_os TO assaf_os_ledger;"]
    for name, privileges in TABLE_PRIVILEGES.items():
        statements.append(
            f"GRANT {', '.join(sorted(privileges))} ON TABLE assaf_os.{name} TO assaf_os_ledger;"
        )
    for name, columns in COLUMN_UPDATES.items():
        statements.append(
            f"GRANT UPDATE ({', '.join(columns)}) ON TABLE assaf_os.{name} TO assaf_os_ledger;"
        )
    statements.append(
        "GRANT EXECUTE ON FUNCTION assaf_os.expense_source_key_sha256(text) TO assaf_os_ledger;"
    )
    statements.extend(addendum_grant_statements())  # V2-14b
    return tuple(statements)


def _require(ok: bool) -> None:
    if not ok:
        raise ValueError(DENIED)


def _migration_role(conn: Connection, explicit: str | None) -> str:
    # §5.5: the migration identity is the single member holding an ADMIN-only edge
    # (ADMIN TRUE, SET FALSE, INHERIT FALSE) on assaf_os_owner. Zero or several such
    # members, or an explicit pin that differs from it, are refused.
    members = set(conn.execute(text(
        "SELECT DISTINCT pg_get_userbyid(member) FROM pg_auth_members "
        "WHERE roleid = to_regrole(:owner) AND admin_option AND NOT set_option "
        "AND NOT inherit_option"
    ), {"owner": OWNER_ROLE}).scalars())
    _require(len(members) == 1 and (explicit is None or members == {explicit}))
    return members.pop()


def _roles(conn: Connection, main_role: str, migration_role: str | None) -> None:
    migration_role = _migration_role(conn, migration_role)
    _require(bool(main_role) and bool(migration_role) and len({
        RUNTIME_ROLE, OWNER_ROLE, RETENTION_ROLE, main_role, migration_role,
    }) == 5)
    # V2-14b: retention is LOGIN NOINHERIT (section 18.2).
    for role, login in ((RUNTIME_ROLE, True), (OWNER_ROLE, False), (RETENTION_ROLE, True)):
        row = conn.execute(text(
            "SELECT rolcanlogin, rolinherit, rolsuper, rolcreaterole, rolcreatedb, "
            "rolreplication, rolbypassrls FROM pg_roles WHERE rolname=:role"
        ), {"role": role}).first()
        _require(row is not None and row[0] == login and not any(row[1:]))
    # Match V2-14 review r1: the owner may ONLY have the ADMIN-only edge to the
    # migration identity. NOINHERIT alone never prevents SET ROLE.
    edges = conn.execute(text(
        "SELECT pg_get_userbyid(roleid), pg_get_userbyid(member), admin_option, "
        "set_option, inherit_option FROM pg_auth_members WHERE "
        "roleid = ANY(SELECT oid FROM pg_roles WHERE rolname = ANY(:roles)) OR "
        "member = ANY(SELECT oid FROM pg_roles WHERE rolname = ANY(:roles))"
    ), {"roles": [RUNTIME_ROLE, OWNER_ROLE, RETENTION_ROLE, main_role]}).all()
    _require(all(tuple(edge) == (OWNER_ROLE, migration_role, True, False, False) for edge in edges))
    _validate_main_role_catalog(conn, main_role)
    # The main role has no TEMP: no temporary relation of its own on any connection.
    _require(not conn.scalar(text(
        "SELECT has_database_privilege(:role, current_database(), 'TEMP')"
    ), {"role": main_role}))
    for role in (RUNTIME_ROLE, RETENTION_ROLE):
        _require(not conn.scalar(text(
            "SELECT has_database_privilege(:role,current_database(),'CREATE,TEMP') OR EXISTS ("
            "SELECT 1 FROM pg_class WHERE relowner=to_regrole(:role) UNION ALL "
            "SELECT 1 FROM pg_namespace WHERE nspowner=to_regrole(:role) UNION ALL "
            "SELECT 1 FROM pg_proc WHERE proowner=to_regrole(:role) UNION ALL "
            "SELECT 1 FROM pg_database WHERE datdba=to_regrole(:role)) OR EXISTS ("
            "SELECT 1 FROM pg_namespace WHERE has_schema_privilege(:role,oid,'CREATE'))"
        ), {"role": role}))
        # Exactly one database ACL entry is allowed: the runtime role's own CONNECT
        # on this database (needed when PUBLIC CONNECT is revoked).
        _require(not conn.scalar(text("""
            SELECT EXISTS(SELECT 1 FROM pg_namespace n
            CROSS JOIN LATERAL aclexplode(n.nspacl) a
            WHERE a.grantee=to_regrole(:role) AND n.nspname<>'assaf_os')
            OR EXISTS(SELECT 1 FROM pg_database d
            CROSS JOIN LATERAL aclexplode(d.datacl) a
            WHERE d.datname=current_database() AND a.grantee=to_regrole(:role)
            AND NOT (CAST(:connect AS boolean) AND a.privilege_type = 'CONNECT'
                     AND NOT a.is_grantable))
        """), {"role": role, "connect": role == RUNTIME_ROLE}))
        _require(not conn.scalar(text(f"""
            SELECT EXISTS(SELECT 1 FROM pg_namespace n WHERE {_USER_SCHEMA}
            AND n.nspname NOT IN ('assaf_os','public')
            AND has_schema_privilege(:role,n.oid,'USAGE'))
        """), {"role": role}))


def _acl(conn: Connection, sql: str, expected: set[tuple]) -> None:
    # A non-owner ACL entry must match exactly, including its grant-option bit.
    # PUBLIC (OID zero) and arbitrary third-party grants are therefore rejected.
    rows = conn.execute(text(sql)).all()
    _require({tuple(row) for row in rows} == expected)


def _closure(conn: Connection) -> None:
    # r1 must-fix 1: a rule or trigger turns one exact-grant INSERT into anything its
    # definer may do. No rule; exactly the reviewed trigger, enabled; no RLS policies.
    _require(not conn.scalar(text(
        "SELECT EXISTS(SELECT 1 FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class "
        "WHERE c.relnamespace=to_regnamespace('assaf_os'))"
    )))
    triggers = conn.execute(text(
        "SELECT c.relname, t.tgname, t.tgfoid::regprocedure::text, t.tgenabled::text, "
        "t.tgtype::int FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "WHERE c.relnamespace=to_regnamespace('assaf_os') AND NOT t.tgisinternal"
    )).all()
    _require(len(triggers) == len(TRIGGERS) and {tuple(row) for row in triggers} == TRIGGERS)
    _require(not conn.scalar(text(
        "SELECT EXISTS(SELECT 1 FROM pg_class c WHERE c.relnamespace=to_regnamespace('assaf_os') "
        "AND (c.relrowsecurity OR c.relforcerowsecurity)) OR EXISTS(SELECT 1 FROM pg_policy p "
        "JOIN pg_class c ON c.oid=p.polrelid WHERE c.relnamespace=to_regnamespace('assaf_os'))"
    )))
    # r1 must-fix 2: assaf_os is a closed dependency island. Nothing outside depends on
    # it (views, matviews, sql-body functions, foreign keys, row types, statistics,
    # publications), and it depends on nothing outside except initdb objects
    # (triggers, defaults, checks or indexes calling another schema's functions).
    _require(not conn.scalar(text(_INSIDE + f"""
        SELECT EXISTS(SELECT 1 FROM pg_depend d
        WHERE ((d.classid, d.objid) IN (SELECT classid, objid FROM inside))
              <> ((d.refclassid, d.refobjid) IN (SELECT classid, objid FROM inside))
          AND NOT ((d.classid, d.objid) IN (SELECT classid, objid FROM inside)
                   AND d.refobjid < {_FIRST_NORMAL_OID}::oid))
    """)))


def _footprints(conn: Connection, main_role: str) -> None:
    params = {
        "owner": OWNER_ROLE, "runtime": RUNTIME_ROLE, "retention": RETENTION_ROLE,
        "main": main_role,
    }
    # r1 must-fix 2: the owner owns nothing in this database outside assaf_os (other
    # than its own default ACL, checked in _defaults) and no shared object.
    _require(not conn.scalar(text(_INSIDE + """
        SELECT EXISTS(SELECT 1 FROM pg_shdepend s
        WHERE s.refclassid = 'pg_authid'::regclass AND s.refobjid = to_regrole(:owner)
          AND s.deptype = 'o'
          AND (s.dbid = 0 OR (
               s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
               AND (s.classid, s.objid) NOT IN (SELECT classid, objid FROM inside)
               AND NOT (s.classid = 'pg_default_acl'::regclass AND s.objid IN (
                   SELECT oid FROM pg_default_acl WHERE defaclrole = to_regrole(:owner))))))
    """), params))
    # The runtime role appears in no ACL and owns nothing in ANY database or shared
    # object (tablespaces, other databases, parameters, large objects, types, ...),
    # except assaf_os objects (pinned exactly by _catalog) and CONNECT on this database.
    # V2-14b: retention appears only as the grantee of its exact §18.2 ACL entries in
    # this database (the assaf_os schema, its two tables, the five os_proposals
    # columns); never as an owner, on a shared object or in another database.
    retention = {
        "retention_tables": sorted(RETENTION_TABLE_PRIVILEGES),
        "column_tables": [t for t, cs in RETENTION_COLUMN_SELECTS.items() for _ in cs],
        "column_names": [c for cs in RETENTION_COLUMN_SELECTS.values() for c in cs],
    }
    _require(not conn.scalar(text(_INSIDE + """,
        retention_acl(classid, objid, objsubid) AS (
            SELECT 'pg_namespace'::regclass::oid, oid, 0 FROM ns
            UNION ALL SELECT 'pg_class'::regclass::oid, c.oid, 0 FROM pg_class c
                WHERE c.relnamespace IN (SELECT oid FROM ns)
                  AND c.relname = ANY(CAST(:retention_tables AS text[]))
            UNION ALL SELECT 'pg_class'::regclass::oid, c.oid, a.attnum::int
                FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid
                JOIN unnest(CAST(:column_tables AS text[]), CAST(:column_names AS text[]))
                     AS g(relname, attname)
                  ON g.relname = c.relname AND g.attname = a.attname
                WHERE c.relnamespace IN (SELECT oid FROM ns))
        SELECT EXISTS(SELECT 1 FROM pg_shdepend s
        WHERE s.refclassid = 'pg_authid'::regclass
          AND ((s.refobjid = to_regrole(:retention) AND NOT (s.deptype = 'a'
                 AND s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                 AND (s.classid, s.objid, s.objsubid)
                     IN (SELECT classid, objid, objsubid FROM retention_acl)))
           OR (s.refobjid = to_regrole(:runtime) AND NOT (s.deptype = 'a' AND (
                 (s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                  AND s.classid IN ('pg_namespace'::regclass, 'pg_class'::regclass,
                                    'pg_proc'::regclass)
                  AND (s.classid, s.objid) IN (SELECT classid, objid FROM inside))
                 OR (s.dbid = 0 AND s.classid = 'pg_database'::regclass
                     AND s.objid = (SELECT oid FROM pg_database
                                    WHERE datname = current_database())))))))
    """), params | retention))
    # r1 must-fix 3: SET / ALTER SYSTEM on a parameter (session_replication_role turns
    # off foreign keys and the assistant trigger) is granted to none of these or PUBLIC.
    _require(not conn.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_parameter_acl p CROSS JOIN LATERAL aclexplode(p.paracl) a
        WHERE a.grantee = 0 OR a.grantee IN (to_regrole(:runtime), to_regrole(:retention),
                                             to_regrole(:owner), to_regrole(:main)))
    """), params))
    # No per-role settings for the ledger roles, and no database- or cluster-wide
    # session_replication_role that would apply to the runtime role's sessions.
    _require(not conn.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_db_role_setting s
        WHERE s.setrole IN (to_regrole(:runtime), to_regrole(:retention), to_regrole(:owner))
           OR (s.setrole = 0
               AND s.setdatabase IN (0, (SELECT oid FROM pg_database
                                         WHERE datname = current_database()))
               AND EXISTS(SELECT 1 FROM unnest(s.setconfig) c
                          WHERE lower(split_part(c, '=', 1)) = 'session_replication_role')))
    """), params))
    # A user-created SECURITY DEFINER routine owned by a superuser or the owner is a
    # write path into assaf_os whatever its body. EXECUTE is not the boundary: a
    # trigger on a main-owned table or an event trigger invokes it without that check.
    _require(not conn.scalar(text(f"""
        SELECT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner
        WHERE p.prosecdef AND p.oid >= {_FIRST_NORMAL_OID}::oid
          AND (r.rolsuper OR r.rolname = :owner))
    """), params))


def _catalog(conn: Connection, main_role: str, migration_role: str | None) -> None:
    _require(conn.dialect.name == "postgresql")
    conn.execute(text("SET LOCAL statement_timeout = '5s'"))
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))
    conn.execute(text(CATALOG_SEARCH_PATH))
    _roles(conn, main_role, migration_role)
    _require(conn.scalar(text(
        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='assaf_os'"
    )) == OWNER_ROLE)
    relations = conn.execute(text(
        "SELECT c.relname,c.relkind,pg_get_userbyid(c.relowner) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='assaf_os' "
        "AND c.relkind NOT IN ('i','I','t')"
    )).all()
    _require({tuple(row) for row in relations} == {
        (name, "r", OWNER_ROLE) for name in OWNED_TABLES
    })
    functions = conn.execute(text(
        "SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')', "
        "pg_get_userbyid(p.proowner), p.prosecdef FROM pg_proc p JOIN pg_namespace n "
        "ON n.oid=p.pronamespace WHERE n.nspname='assaf_os'"
    )).all()
    # identity_arguments includes the declared argument name in this SQL helper.
    _require({(name.replace('value text', 'text'), owner, security) for name, owner, security
              in functions} == {(name, OWNER_ROLE, False) for name in OWNED_FUNCTIONS})
    addendum_schema, addendum_tables, addendum_columns = _addendum_acl()  # V2-14b
    _acl(conn, """
        SELECT pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable
        FROM pg_namespace n CROSS JOIN LATERAL
        aclexplode(COALESCE(n.nspacl,acldefault('n',n.nspowner))) a
        WHERE n.nspname='assaf_os' AND a.grantee<>n.nspowner
    """, {(RUNTIME_ROLE, "USAGE", False)} | addendum_schema)
    _acl(conn, """
        SELECT c.relname,pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault(
            CASE WHEN c.relkind='S' THEN 'S'::"char" ELSE 'r'::"char" END,c.relowner))) a
        WHERE n.nspname='assaf_os' AND a.grantee<>c.relowner
    """, {(name, RUNTIME_ROLE, privilege, False)
          for name, privileges in TABLE_PRIVILEGES.items() for privilege in privileges}
        | addendum_tables)
    _acl(conn, """
        SELECT c.relname,col.attname,pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute col ON col.attrelid=c.oid
        CROSS JOIN LATERAL aclexplode(col.attacl) a
        WHERE n.nspname='assaf_os' AND a.grantee<>c.relowner
    """, {(name, col, RUNTIME_ROLE, "UPDATE", False)
          for name, columns in COLUMN_UPDATES.items() for col in columns}
        | addendum_columns)
    _acl(conn, """
        SELECT p.proname,pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
        WHERE n.nspname='assaf_os' AND a.grantee<>p.proowner
    """, {("expense_source_key_sha256", RUNTIME_ROLE, "EXECUTE", False)})
    # Reject ambient PUBLIC privileges and explicit grants in ANY user schema,
    # even without schema USAGE. V2-14b: inside assaf_os, retention's effective
    # privileges are pinned exactly by _retention_effective below.
    for role in (RUNTIME_ROLE, RETENTION_ROLE):
        _require(not conn.scalar(text(f"""
            SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE {_USER_SCHEMA} AND n.nspname<>'assaf_os' AND CASE
              WHEN c.relkind='S' THEN has_sequence_privilege(:role,c.oid,'USAGE,SELECT,UPDATE')
              WHEN c.relkind IN ('r','p','v','m','f') THEN
                has_table_privilege(:role,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR has_any_column_privilege(:role,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')
              ELSE false END)
            OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE {_USER_SCHEMA} AND n.nspname<>'assaf_os'
            AND has_function_privilege(:role,p.oid,'EXECUTE'))
        """), {"role": role}))
    _retention_effective(conn)  # V2-14b
    _closure(conn)
    _footprints(conn, main_role)
    _defaults(conn, main_role)


def _defaults(conn: Connection, main_role: str) -> None:
    # Absence of a global owner function ACL restores PostgreSQL's implicit
    # PUBLIC EXECUTE. A schema-local REVOKE cannot cancel that default.
    _require(conn.scalar(text("""
        SELECT NOT EXISTS(SELECT 1 FROM aclexplode(COALESCE(
          (SELECT defaclacl FROM pg_default_acl WHERE defaclrole='assaf_os_owner'::regrole
           AND defaclnamespace=0 AND defaclobjtype='f'),
          acldefault('f','assaf_os_owner'::regrole))) a
          WHERE a.grantee<>'assaf_os_owner'::regrole)
    """)))
    _require(not conn.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_default_acl d
        CROSS JOIN LATERAL aclexplode(d.defaclacl) a WHERE
          (d.defaclnamespace=(SELECT oid FROM pg_namespace WHERE nspname='assaf_os')
           AND a.grantee<>d.defaclrole)
          OR (d.defaclrole='assaf_os_owner'::regrole AND a.grantee<>d.defaclrole)
          OR a.grantee IN (0,'assaf_os_ledger'::regrole,'assaf_os_retention'::regrole)
          OR (d.defaclnamespace=0 AND a.grantee=to_regrole(:main)
              AND d.defaclrole<>a.grantee))
    """), {"main": main_role}))


def validate_ledger_catalog(
    conn: Connection, *, main_role: str, migration_role: str | None = None,
) -> None:
    """Operator-only transactional audit; raises ValueError('ledger_role_denied').

    main_role must come from validate_main_role on the actual main DSN, never a
    caller-supplied replacement. migration_role=None derives it (§5.5); an explicit
    value must equal the derived one. Runtime consumers use validate_ledger_role.
    This function does not commit and cannot prove a supplied role is a real DSN.
    It sets transaction-local timeouts and a catalog-first search_path.
    """
    try:
        _catalog(conn, main_role, migration_role)
    except (ValueError, SQLAlchemyError, TypeError, AttributeError, KeyError):
        raise ValueError(DENIED) from None


def validate_ledger_role(
    db: Session, main_engine: Engine, *, migration_role: str | None = None,
) -> None:
    """§5.5 runtime/authority preflight; audit both actual DSNs, without caching.

    A preflight, not a per-request check (0.1-0.3 s of catalog queries): run it on
    the first connection, every 10 minutes and after a reconnect, and cache the result.
    Requires separate, unimpersonated identities on the same PostgreSQL server and
    database. migration_role=None derives the migration identity as the single
    ADMIN-only member of assaf_os_owner. Every refusal, including an unavailable
    catalog, is the single sanitized ValueError('ledger_role_denied'). It does not
    activate the ledger.
    """
    try:
        _require(db.get_bind().dialect.name == main_engine.dialect.name == "postgresql")
        db.execute(text(CATALOG_SEARCH_PATH))
        identity = text(
            "SELECT current_database(),inet_server_addr()::text,inet_server_port(),"
            "session_user,current_user"
        )
        ledger = db.execute(identity).one()
        _require(ledger[3] == ledger[4] == RUNTIME_ROLE and None not in ledger[:3])
        with main_engine.connect() as main:
            main.execute(text(CATALOG_SEARCH_PATH))
            app = main.execute(identity).one()
            _require(app[:3] == ledger[:3] and app[3] == app[4] and app[3] != ledger[3])
            main.execute(text("SET LOCAL statement_timeout = '5s'"))
            _validate_main_role_catalog(main, app[3])
            validate_ledger_catalog(
                db.connection(), main_role=app[3], migration_role=migration_role,
            )
    except (ValueError, SQLAlchemyError, TypeError, AttributeError, KeyError):
        raise ValueError(DENIED) from None
