-- Execute as the database superuser in ONE transaction. No password in this file.
-- Optional transaction-local mia.app_role is for isolated tests; VM wrapper pins mia_app.
-- Transaction-local mia.app_role_reapply = 'on' selects the post-restore repair (`reapply`):
--   * it never creates the role (roles are cluster-global and survive DROP DATABASE), and
--   * it does not refuse residual assaf_os access. pg_restore --no-owner --no-privileges
--     resurrects PUBLIC EXECUTE on the ledger functions, and V2-15's reapply, the next
--     post-restore step, clears it. The caller verifies the final state with the real
--     V2-14 validator. Every other guard below applies unchanged.
-- Do not REASSIGN OWNED: that would also transfer the database and private schemas.
DO $demote$
DECLARE
    target text := coalesce(nullif(current_setting('mia.app_role', true), ''), 'mia_app');
    reapply boolean := coalesce(current_setting('mia.app_role_reapply', true), '') = 'on';
    target_oid oid;
    obj record;
BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
        RAISE EXCEPTION 'app_role_admin_required';
    END IF;
    IF target = current_user OR target !~ '^mia_app(_[a-f0-9]+)?$' THEN
        RAISE EXCEPTION 'app_role_target_invalid';
    END IF;
    PERFORM pg_advisory_xact_lock(214014);
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=target) THEN
        IF reapply THEN
            RAISE EXCEPTION 'app_role_reapply_role_missing';
        END IF;
        EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                       'NOREPLICATION NOBYPASSRLS NOINHERIT', target);
    END IF;
    SELECT oid INTO STRICT target_oid FROM pg_roles WHERE rolname=target;
    -- Never silently repair a pre-existing privileged role or its memberships.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE oid=target_oid AND
        (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
         OR rolinherit OR NOT rolcanlogin)) THEN
        RAISE EXCEPTION 'app_role_existing_unsafe';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_database WHERE datdba=target_oid) THEN
        RAISE EXCEPTION 'app_role_database_owner_unsafe';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_auth_members WHERE member=target_oid) THEN
        RAISE EXCEPTION 'app_role_membership_unsafe';
    END IF;
    IF NOT reapply AND (EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='assaf_os' AND
        has_schema_privilege(target, oid, 'USAGE,CREATE')) OR EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='assaf_os' AND CASE
        WHEN c.relkind='S' THEN has_sequence_privilege(target,c.oid,'USAGE,SELECT,UPDATE')
        WHEN c.relkind IN ('r','p','v','m','f') THEN
        has_table_privilege(target,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
        OR has_any_column_privilege(target,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')
        ELSE false END) OR EXISTS (
        SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='assaf_os' AND has_function_privilege(target,p.oid,'EXECUTE'))) THEN
        RAISE EXCEPTION 'app_role_ledger_access_unsafe';
    END IF;
    -- PUBLIC database CREATE is not required by current deployments. Keep CONNECT/TEMP.
    IF EXISTS (SELECT 1 FROM pg_database d,
        LATERAL aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a
        WHERE d.datname=current_database() AND a.grantee=0 AND a.privilege_type='CREATE') THEN
        EXECUTE format('REVOKE CREATE ON DATABASE %I FROM PUBLIC', current_database());
    END IF;
    IF has_database_privilege(target, current_database(), 'CREATE') THEN
        EXECUTE format('REVOKE CREATE ON DATABASE %I FROM %I', current_database(), target);
    END IF;
    IF NOT has_database_privilege(target, current_database(), 'CONNECT') THEN
        EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), target);
    END IF;
    IF NOT has_schema_privilege(target, 'public', 'USAGE')
       OR NOT has_schema_privilege(target, 'public', 'CREATE') THEN
        EXECUTE format('GRANT USAGE, CREATE ON SCHEMA public TO %I', target);
    END IF;
    -- Tables first: PostgreSQL moves their owned serial/identity sequences with them.
    FOR obj IN SELECT c.oid, c.relname, c.relkind FROM pg_class c
        WHERE c.relnamespace='public'::regnamespace AND c.relkind IN ('r','p','v','m','f')
        AND c.relowner<>target_oid ORDER BY c.oid
    LOOP
        IF EXISTS (SELECT 1 FROM pg_depend WHERE classid='pg_class'::regclass
                   AND objid=obj.oid AND deptype='e') THEN
            RAISE EXCEPTION 'app_role_extension_object_requires_review';
        END IF;
        EXECUTE format('ALTER %s public.%I OWNER TO %I',
            CASE obj.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
            WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END, obj.relname, target);
    END LOOP;
    FOR obj IN SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace
        AND relkind='S' AND relowner<>target_oid
    LOOP
        EXECUTE format('ALTER SEQUENCE public.%I OWNER TO %I', obj.relname, target);
    END LOOP;
    FOR obj IN SELECT p.oid, p.prokind, p.proname, pg_get_function_identity_arguments(p.oid) args
        FROM pg_proc p WHERE p.pronamespace='public'::regnamespace AND p.proowner<>target_oid
    LOOP
        IF EXISTS (SELECT 1 FROM pg_depend WHERE classid='pg_proc'::regclass
                   AND objid=obj.oid AND deptype='e') THEN
            RAISE EXCEPTION 'app_role_extension_object_requires_review';
        END IF;
        EXECUTE format('ALTER %s public.%I(%s) OWNER TO %I',
            CASE obj.prokind WHEN 'p' THEN 'PROCEDURE' WHEN 'a' THEN 'AGGREGATE'
            ELSE 'FUNCTION' END, obj.proname, obj.args, target);
    END LOOP;
    -- Future public objects are created/owned by mia_app by the existing migrator.
    -- Owners already have all rights. Remove implicit PUBLIC execution globally:
    -- a schema-local REVOKE cannot cancel the global function default.
    IF EXISTS (SELECT 1 FROM aclexplode(coalesce(
        (SELECT defaclacl FROM pg_default_acl WHERE defaclrole=target_oid
         AND defaclnamespace=0 AND defaclobjtype='f'), acldefault('f', target_oid))) a
         WHERE a.grantee=0) THEN
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I '
                       'REVOKE ALL ON FUNCTIONS FROM PUBLIC', target);
    END IF;
    FOR obj IN SELECT d.defaclnamespace, d.defaclobjtype FROM pg_default_acl d
        WHERE d.defaclrole=target_oid AND d.defaclobjtype IN ('r','S','f')
        AND d.defaclnamespace IN (0, 'public'::regnamespace)
        AND EXISTS (SELECT 1 FROM aclexplode(d.defaclacl) a WHERE a.grantee=0)
    LOOP
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I %s REVOKE ALL ON %s FROM PUBLIC',
            target, CASE WHEN obj.defaclnamespace=0 THEN '' ELSE 'IN SCHEMA public' END,
            CASE obj.defaclobjtype WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES'
            ELSE 'FUNCTIONS' END);
    END LOOP;
END
$demote$;
