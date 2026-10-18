"""V2-14b schema addendum (contract §4), exercised only on the named disposable PG16."""

import hashlib
import io
import json
import os
import re
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.db import os_ledger_migrate as ledger
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parents[2]
V2_14 = "20261015_assaf_os_ledger.sql"
FILES = (
    "20261016_assaf_os_ledger_core_v2.sql",
    "20261016_assaf_os_statements.sql",
    "20261016_assaf_os_assistant.sql",
)
V2_14_TABLES = frozenset({
    "os_workspaces", "os_authority", "os_leads", "os_clients", "os_works", "os_payments",
    "os_time", "os_expenses", "os_tasks", "os_proposals", "os_receipts", "os_events",
    "os_outbox", "os_mirror_state", "os_summary_monthly", "os_summary_cycle",
    "os_import_batches", "os_import_rows",
})
NEW_TABLES = frozenset({
    "os_statements", "os_statement_lines", "os_statement_batch_lines", "os_merchant_rules",
    "os_statement_review_drafts", "os_assistant_credentials", "os_assistant_audit",
})
STM = "STM_" + "a" * 32
RULE = "MR_" + "a" * 24
CRED = "AC_" + "a" * 32
SCOPES = "merchant_rules:read own_proposals:read statement:propose"
HEX = "0123456789abcdef" * 4
NOW = "2026-10-16T00:00:00Z"
CHECK, UNIQUE, FK, NOT_NULL, TOO_LONG = "23514", "23505", "23503", "23502", "22001"

# Exact §4 columns as built: (name, type, NOT NULL, default). Order is the SQL's.
VC, TS = "character varying", "timestamp with time zone"
STAMPS = (("created_at", TS, True, "now()"), ("updated_at", TS, True, "now()"))
COLUMNS = {
    "os_statements": (
        ("workspace_id", f"{VC}(128)", True, None),
        ("statement_id", f"{VC}(36)", True, None),
        ("account_label", f"{VC}(40)", True, None),
        ("charge_date", "date", True, None),
        ("cycle", "character(7)", True, None),
        ("cycle_from", "date", True, None),
        ("cycle_to", "date", True, None),
        ("status", f"{VC}(16)", True, f"'active'::{VC}"),
        ("line_count", "integer", True, None),
        ("lines_digest", "character(64)", True, None),
        ("current_proposal_id", f"{VC}(64)", False, None),
        ("approved_proposal_id", f"{VC}(64)", False, None),
        ("imported_version", "bigint", False, None),
        *STAMPS,
    ),
    "os_statement_lines": (
        ("workspace_id", f"{VC}(128)", True, None),
        ("statement_id", f"{VC}(36)", True, None),
        ("line_key", "character(64)", True, None),
        ("tag", f"{VC}(16)", True, None),
        ("state", f"{VC}(16)", True, f"'active'::{VC}"),
        ("txn_date", "date", False, None),
        ("merchant_display", f"{VC}(80)", False, None),
        ("merchant_key", f"{VC}(160)", False, None),
        ("amount_minor", "bigint", False, None),
        ("kind", f"{VC}(16)", False, None),
        ("currency", "character(3)", False, None),
        ("original_amount_minor", "bigint", False, None),
        ("original_currency", "character(3)", False, None),
        ("installment_n", "integer", False, None),
        ("installment_of", "integer", False, None),
        ("category", f"{VC}(40)", False, None),
        ("path", f"{VC}(40)", False, None),
        ("platform", f"{VC}(40)", False, None),
        ("is_subscription", "boolean", False, None),
        ("rule_id", f"{VC}(27)", False, None),
        ("assistant_tag", f"{VC}(16)", False, None),
        ("owner_override", "jsonb", False, None),
        ("expense_id", f"{VC}(64)", False, None),
        ("matched_expense_id", f"{VC}(64)", False, None),
        ("expense_revision_at_import", "bigint", False, None),
        ("revision", "bigint", True, None),
        *STAMPS,
    ),
    "os_statement_batch_lines": (
        ("workspace_id", f"{VC}(128)", True, None),
        ("proposal_id", f"{VC}(64)", True, None),
        ("line_key", "character(64)", True, None),
        ("occurrence_index", "integer", True, None),
        ("detail", "jsonb", False, None),
        ("purged_at", TS, False, None),
        *STAMPS,
    ),
    "os_merchant_rules": (
        ("workspace_id", f"{VC}(128)", True, None),
        ("rule_id", f"{VC}(27)", True, None),
        ("match", f"{VC}(8)", True, None),
        ("tag", f"{VC}(16)", True, None),
        ("merchant_key", f"{VC}(160)", False, None),
        ("merchant_key_hmac", "character(64)", True, None),
        ("category", f"{VC}(40)", False, None),
        ("path", f"{VC}(40)", False, None),
        ("platform", f"{VC}(40)", False, None),
        ("is_subscription", "boolean", False, None),
        ("label", f"{VC}(80)", False, None),
        ("aliases", "jsonb", True, "'[]'::jsonb"),
        ("active", "boolean", True, "true"),
        ("revision", "bigint", True, None),
        ("source_proposal_id", f"{VC}(64)", True, None),
        *STAMPS,
    ),
    "os_statement_review_drafts": (
        ("workspace_id", f"{VC}(128)", True, None),
        ("proposal_id", f"{VC}(64)", True, None),
        ("draft_revision", "bigint", True, "0"),
        ("overrides", "jsonb", True, "'{}'::jsonb"),
        ("choices", "jsonb", True, "'{}'::jsonb"),
        *STAMPS,
    ),
    "os_assistant_credentials": (
        ("workspace_id", f"{VC}(128)", True, None),
        ("credential_id", f"{VC}(35)", True, None),
        ("owner_id", f"{VC}(20)", True, None),
        ("epoch", "bigint", True, None),
        ("label", f"{VC}(40)", True, None),
        ("kind", f"{VC}(8)", True, None),
        ("scopes", f"{VC}(64)", True, None),
        ("secret_hash", "character(64)", True, None),
        ("created_at", TS, True, "now()"),
        ("expires_at", TS, True, None),
        ("revoked_at", TS, False, None),
        ("last_used_at", TS, False, None),
        ("use_count", "bigint", True, "0"),
    ),
    "os_assistant_audit": (
        ("workspace_id", f"{VC}(128)", True, None),
        ("audit_id", "uuid", True, None),
        ("credential_id", f"{VC}(35)", False, None),
        ("occurred_at", TS, True, "now()"),
        ("operation", f"{VC}(32)", True, None),
        ("reason", f"{VC}(64)", True, None),
        ("ok", "boolean", True, None),
        ("line_count", "integer", True, "0"),
        ("bytes", "integer", True, "0"),
        ("proposal_id", f"{VC}(64)", False, None),
        ("client_ip_hmac", "character(64)", True, None),
    ),
}
ADDED_COLUMNS = {
    "os_workspaces": (
        ("pending_seq", "bigint", True, "0"),
        ("statement_pepper_fp", "character(64)", False, None),
    ),
    "os_receipts": (("reason_codes", "jsonb", True, "'[]'::jsonb"),),
}

# Every key, unique and FK as built (pg_get_constraintdef), plus the CHECK names per table.
_WS = "FOREIGN KEY (workspace_id) REFERENCES assaf_os.os_workspaces(workspace_id)"
_PROPOSAL = "REFERENCES assaf_os.os_proposals(workspace_id, proposal_id)"
_EXPENSE = "REFERENCES assaf_os.os_expenses(workspace_id, record_id)"
KEYS = {
    "os_statements": {
        "os_statements_pkey": "PRIMARY KEY (workspace_id, statement_id)",
        "os_statements_workspace_fkey": _WS,
        "os_statements_current_proposal_fkey":
            f"FOREIGN KEY (workspace_id, current_proposal_id) {_PROPOSAL}",
        "os_statements_approved_proposal_fkey":
            f"FOREIGN KEY (workspace_id, approved_proposal_id) {_PROPOSAL}",
        "os_statements_label_charge_date_key":
            "UNIQUE (workspace_id, account_label, charge_date)",
    },
    "os_statement_lines": {
        "os_statement_lines_pkey": "PRIMARY KEY (workspace_id, statement_id, line_key)",
        "os_statement_lines_workspace_fkey": _WS,
        "os_statement_lines_statement_fkey": "FOREIGN KEY (workspace_id, statement_id) "
            "REFERENCES assaf_os.os_statements(workspace_id, statement_id)",
        "os_statement_lines_expense_fkey": f"FOREIGN KEY (workspace_id, expense_id) {_EXPENSE}",
        "os_statement_lines_matched_expense_fkey":
            f"FOREIGN KEY (workspace_id, matched_expense_id) {_EXPENSE}",
    },
    "os_statement_batch_lines": {
        "os_statement_batch_lines_pkey": "PRIMARY KEY (workspace_id, proposal_id, line_key)",
        "os_statement_batch_lines_workspace_fkey": _WS,
        "os_statement_batch_lines_proposal_fkey":
            f"FOREIGN KEY (workspace_id, proposal_id) {_PROPOSAL}",
    },
    "os_merchant_rules": {
        "os_merchant_rules_pkey": "PRIMARY KEY (workspace_id, rule_id)",
        "os_merchant_rules_workspace_fkey": _WS,
        "os_merchant_rules_source_proposal_fkey":
            f"FOREIGN KEY (workspace_id, source_proposal_id) {_PROPOSAL}",
        "os_merchant_rules_match_hmac_key": "UNIQUE (workspace_id, match, merchant_key_hmac)",
    },
    "os_statement_review_drafts": {
        "os_statement_review_drafts_pkey": "PRIMARY KEY (workspace_id, proposal_id)",
        "os_statement_review_drafts_workspace_fkey": _WS,
        "os_statement_review_drafts_proposal_fkey":
            f"FOREIGN KEY (workspace_id, proposal_id) {_PROPOSAL}",
    },
    "os_assistant_credentials": {
        "os_assistant_credentials_pkey": "PRIMARY KEY (workspace_id, credential_id)",
        "os_assistant_credentials_workspace_fkey": _WS,
        "os_assistant_credentials_secret_hash_key": "UNIQUE (secret_hash)",
    },
    "os_assistant_audit": {
        "os_assistant_audit_pkey": "PRIMARY KEY (workspace_id, audit_id)",
        "os_assistant_audit_workspace_fkey": _WS,
    },
}
CHECKS = {
    "os_statements": {
        "statement_id", "account_label", "cycle", "status", "line_count", "lines_digest",
        "imported_version", "cycle_range", "updated",
    },
    "os_statement_lines": {
        "line_key", "tag", "state", "amount_minor", "kind", "currency",
        "original_amount_minor", "original_currency", "rule_id", "assistant_tag",
        "owner_override", "expense_revision_at_import", "revision", "personal", "business",
        "original_pair", "installment", "updated",
    },
    "os_statement_batch_lines": {"line_key", "occurrence_index", "detail", "purge", "updated"},
    "os_merchant_rules": {
        "rule_id", "match", "tag", "merchant_key_hmac", "aliases", "revision", "personal",
        "business", "updated",
    },
    "os_statement_review_drafts": {"draft_revision", "overrides", "choices", "updated"},
    "os_assistant_credentials": {
        "credential_id", "owner_id", "epoch", "kind", "scopes", "secret_hash", "use_count",
        "expiry", "expiry_max", "revoked",
    },
    "os_assistant_audit": {
        "credential_id", "line_count", "bytes", "client_ip_hmac",
    },
}
CORE_CONSTRAINTS = {
    "os_workspaces": {"os_workspaces_pending_seq_check", "os_workspaces_statement_pepper_fp_check"},
    "os_proposals": {
        "os_proposals_status_check", "os_proposals_abandoned_kind_check",
        "os_proposals_credential_fkey",
    },
    "os_receipts": {
        "os_receipts_status_check", "os_receipts_undo_of_kind_check",
        "os_receipts_undo_until_kind_check", "os_receipts_reason_codes_check",
    },
    "os_import_batches": {"os_import_batches_workspace_epoch_digest_key"},
}
INDEXES = {
    "os_proposals_workspace_status_kind_created_idx":
        "CREATE INDEX os_proposals_workspace_status_kind_created_idx ON assaf_os.os_proposals "
        "USING btree (workspace_id, status, kind, created_at)",
    "os_statement_lines_matched_expense_key":
        "CREATE UNIQUE INDEX os_statement_lines_matched_expense_key ON assaf_os.os_statement_lines "
        "USING btree (workspace_id, matched_expense_id) WHERE (((state)::text = 'active'::text) "
        "AND (matched_expense_id IS NOT NULL))",
    "os_assistant_audit_credential_occurred_idx":
        "CREATE INDEX os_assistant_audit_credential_occurred_idx ON assaf_os.os_assistant_audit "
        "USING btree (credential_id, occurred_at)",
}
# The facts every business line carries; the optional ones are checked as pairs.
REQUIRED_FACTS = (
    "txn_date", "merchant_display", "merchant_key", "amount_minor", "kind", "currency",
)
# A personal line keeps only its keyed identity: each group must stay NULL (paired facts together).
PERSONAL_NULL = (
    {"txn_date": "2026-10-01"}, {"merchant_display": "Shop"}, {"merchant_key": "shop"},
    {"amount_minor": 100}, {"kind": "charge"}, {"currency": "ILS"},
    {"original_amount_minor": 100, "original_currency": "USD"},
    {"installment_n": 1, "installment_of": 3},
    {"category": "Ads"}, {"path": "Ads"}, {"platform": "Meta"}, {"is_subscription": False},
    {"rule_id": RULE}, {"assistant_tag": "business"}, {"owner_override": {}},
    {"expense_id": "expense"}, {"matched_expense_id": "expense"},
    {"expense_revision_at_import": 1},
)
PERSONAL_RULE_DETAIL = (
    {"merchant_key": "shop"}, {"match": "prefix"}, {"aliases": ["shop"]}, {"label": "Shop"},
    {"category": "Ads"}, {"path": "Ads"}, {"platform": "Meta"}, {"is_subscription": False},
    {"is_subscription": True},
)


def _sql_without_comments(name):
    data = (ROOT / "migrations_assaf_os" / name).read_text(encoding="utf-8")
    return "\n".join(line.split("--", 1)[0] for line in data.splitlines())


def test_allowlist_is_exact_ordered_and_lf_only():
    assert ledger.MIGRATIONS == (V2_14, *FILES)
    assert ledger.TABLES == V2_14_TABLES | NEW_TABLES
    assert {p.name for p in (ROOT / "migrations_assaf_os").glob("*.sql")} == set(ledger.MIGRATIONS)
    scripts = ledger.migration_scripts()
    assert [name for name, _, _ in scripts] == list(ledger.MIGRATIONS)
    for name, sql, digest in scripts:
        data = (ROOT / "migrations_assaf_os" / name).read_bytes()
        assert b"\r" not in data and data.endswith(b"\n")
        assert sql == data.decode("utf-8")
        assert digest == hashlib.sha256(data).hexdigest()


_PRIVILEGED = re.compile(
    r"\b(GRANT|REVOKE|OWNER\s+TO|DEFAULT\s+PRIVILEGES|ROLE|SECURITY|FUNCTION|PROCEDURE|"
    r"TRIGGER|POLICY|RULE|EXTENSION|INSERT|UPDATE|DELETE|TRUNCATE|COPY|SCHEMA|SEARCH_PATH|"
    r"DROP\s+(TABLE|INDEX|COLUMN|VIEW|TYPE)|DO|EXECUTE|SEQUENCE|SERIAL|BIGSERIAL|IDENTITY)\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize("name", FILES)
def test_addendum_sql_holds_no_privilege_dml_or_code(name):
    sql = _sql_without_comments(name)
    assert not _PRIVILEGED.findall(sql)
    # Every object is schema-qualified; the runner's search_path is never relied on.
    targets = re.findall(
        r"\b(?:CREATE\s+TABLE|ALTER\s+TABLE|REFERENCES|INDEX\s+\w+\s+ON)\s+([\w.]+)", sql,
        flags=re.IGNORECASE,
    )
    assert targets and all(target.startswith("assaf_os.") for target in targets)


def test_normal_runner_never_lists_the_addendum():
    from app.db.migrate import POSTGRES_ONLY, list_migration_files

    names = {path.name for path in list_migration_files()}
    assert not names & set(FILES)
    assert not POSTGRES_ONLY & set(FILES)
    assert not any("assaf_os" in path.read_text(encoding="utf-8")
                   for path in list_migration_files())


def _constraints(c):
    return {
        name: (table, kind, definition) for name, table, kind, definition in c.execute(text(
            "SELECT conname, conrelid::regclass::text, contype, pg_get_constraintdef(oid) "
            "FROM pg_constraint WHERE connamespace='assaf_os'::regnamespace"
        ))
    }


def _catalog(c):
    return {
        "relations": sorted(c.execute(text(
            "SELECT c.relname, c.relkind, c.relowner::regrole::text, c.relacl::text "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='assaf_os'"
        )).all()),
        "columns": sorted(c.execute(text(
            "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
            "a.attacl::text FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='assaf_os' "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        )).all()),
        "constraints": sorted(_constraints(c).items()),
        "indexes": sorted(c.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='assaf_os'"
        )).all()),
    }


@pytest.fixture(scope="module")
def addendum_db():
    url = os.getenv("MIA_OS_LEDGER_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("disposable MIA_OS_LEDGER_TEST_POSTGRES_URL required")
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "localhost", "::1"}
    assert parsed.database == "mia_ledger_test" and parsed.username == "postgres"
    assert not parsed.query
    engine = create_engine(parsed)
    evidence = SimpleNamespace(engine=engine, worker=[], ledger_after_worker=[])
    with engine.begin() as c:
        assert 160000 <= int(c.scalar(text("SHOW server_version_num"))) < 170000
        # The same disposable reset as test_assaf_os_ledger_migration.py: an empty PG16.
        c.execute(text("DROP SCHEMA IF EXISTS assaf_os CASCADE"))
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    from app.db import session
    from app.workers import migrate

    # The ordinary deploy worker, twice, with the addendum files on disk.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session, "get_engine", lambda: engine)
        patch.setattr(migrate, "get_engine", lambda: engine)
        for _ in range(2):
            out = io.StringIO()
            with redirect_stdout(out):
                migrate.main()
            evidence.worker.append(json.loads(out.getvalue()))
            with engine.connect() as c:
                evidence.ledger_after_worker.append(
                    c.scalar(text("SELECT to_regnamespace('assaf_os')"))
                )
    scripts = ledger.migration_scripts()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ledger, "migration_scripts", lambda: scripts[:1])
        evidence.v214 = ledger.apply_ledger_migrations(engine)
    # The test reads each V2-14 constraint name from pg_constraint before any DROP runs.
    with engine.connect() as c:
        evidence.before = _constraints(c)
    evidence.addendum = ledger.apply_ledger_migrations(engine)
    yield evidence
    engine.dispose()


def insert(c, table, values):
    params = {
        key: json.dumps(value) if isinstance(value, (dict, list)) else value
        for key, value in values.items()
    }
    c.execute(text(
        f"INSERT INTO assaf_os.{table} ({', '.join(params)}) "
        f"VALUES ({', '.join(':' + key for key in params)})"
    ), params)


def proposal(c, proposal_id, **changes):
    values = dict(
        workspace_id="one", proposal_id=proposal_id, request_id=uuid4(), request_digest=HEX,
        envelope_digest=HEX, owner_id="123", epoch=1, approval_id=uuid4(),
        rejection_id=uuid4(), entity="statements", record_id=STM, expected_revision=0,
        mutations="[]", dependency_digest=HEX, source="ASSISTANT_PASTE", kind="statement_batch",
        status="pending", created_at=NOW, expires_at="2026-10-16T00:05:00Z",
    ) | changes
    insert(c, "os_proposals", values)
    return values


def expense(c, record_id, workspace="one"):
    insert(c, "os_expenses", dict(
        workspace_id=workspace, record_id=record_id, revision=1, inputs="{}", source="OS",
        created_at=NOW, updated_at=NOW, amount_minor=100, source_type="actual",
        source_key=record_id, source_digest=hashlib.sha256(record_id.encode()).hexdigest(),
    ))


def valid(table, **changes):
    rows = {
        "os_statements": dict(
            workspace_id="one", statement_id=STM, account_label="Business card 1234",
            charge_date="2026-10-15", cycle="2026-10", cycle_from="2026-09-10",
            cycle_to="2026-10-09", line_count=1, lines_digest=HEX,
        ),
        "os_statement_lines": dict(
            workspace_id="one", statement_id=STM, line_key=HEX, tag="business",
            txn_date="2026-10-01", merchant_display="Shop", merchant_key="shop",
            amount_minor=1200, kind="charge", currency="ILS", revision=1,
        ),
        "os_statement_batch_lines": dict(
            workspace_id="one", proposal_id="batch", line_key=HEX, occurrence_index=0,
            detail={"merchant_display": "Shop", "confidence": 0.9},
        ),
        "os_merchant_rules": dict(
            workspace_id="one", rule_id=RULE, match="exact", tag="business",
            merchant_key="shop", merchant_key_hmac=HEX, revision=1, source_proposal_id="batch",
        ),
        "os_statement_review_drafts": dict(workspace_id="one", proposal_id="batch"),
        "os_assistant_credentials": dict(
            workspace_id="one", credential_id=CRED, owner_id="123", epoch=1, label="Claude",
            kind="claude", scopes=SCOPES, secret_hash=HEX, created_at=NOW,
            expires_at="2026-11-15T00:00:00Z",
        ),
        "os_assistant_audit": dict(
            workspace_id="one", audit_id=uuid4(), operation="submit", reason="accepted",
            ok=True, client_ip_hmac=HEX,
        ),
    }
    return rows[table] | changes


def prepare(c, table):
    if table == "os_statement_lines":
        insert(c, "os_statements", valid("os_statements"))


@contextmanager
def refused(c, sqlstate, constraint=None):
    with pytest.raises(SQLAlchemyError) as caught, c.begin_nested():
        yield
    error = caught.value.orig
    assert (error.sqlstate, error.diag.constraint_name if constraint else None) == (
        sqlstate, constraint,
    )


@pytest.fixture
def db(addendum_db):
    with addendum_db.engine.connect() as c, c.begin():
        # interval '90 days' follows the session TimeZone; pin UTC so the boundary is exact.
        c.execute(text("SET LOCAL TimeZone = 'UTC'"))
        for workspace in ("one", "two"):
            insert(c, "os_workspaces", dict(workspace_id=workspace, owner_id="123", epoch=1))
        expense(c, "expense")
        expense(c, "expense2")
        proposal(c, "batch")
        yield c
        c.rollback()


def test_normal_mia_migrate_never_applies_the_ledger(addendum_db):
    assert len(addendum_db.worker) == 2
    for summary in addendum_db.worker:
        assert summary["failed"] == "" and not summary["skipped"]
        assert not set(FILES) & set(summary["applied"] + summary["already"])
    assert addendum_db.ledger_after_worker == [None, None]


def test_applies_after_v214_in_order_and_rerun_is_a_noop(addendum_db):
    from app.db.session import ledger_schema_ready

    engine = addendum_db.engine
    assert (addendum_db.v214.applied, addendum_db.v214.already) == ([V2_14], [])
    assert (addendum_db.addendum.applied, addendum_db.addendum.already) == (list(FILES), [V2_14])
    assert addendum_db.addendum.failed == ""
    with engine.connect() as c:
        before = _catalog(c)
    for _ in range(2):
        again = ledger.apply_ledger_migrations(engine)
        assert (again.applied, again.already) == ([], list(ledger.MIGRATIONS))
    assert ledger_schema_ready(engine)
    with engine.connect() as c:
        assert _catalog(c) == before
        assert dict(c.execute(text(
            "SELECT filename, sha256 FROM assaf_os.schema_migrations"
        )).all()) == {
            name: hashlib.sha256((ROOT / "migrations_assaf_os" / name).read_bytes()).hexdigest()
            for name in ledger.MIGRATIONS
        }
        assert set(c.scalars(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='assaf_os'"
        ))) == V2_14_TABLES | NEW_TABLES | {"schema_migrations"}


def test_v214_constraint_names_are_read_before_they_are_replaced(addendum_db):
    before = addendum_db.before
    table, kind, definition = before["os_proposals_status_check"]
    assert (table, kind) == ("assaf_os.os_proposals", "c")
    assert "'superseded'" in definition and "'abandoned'" not in definition
    table, kind, definition = before["os_receipts_status_check"]
    assert (table, kind) == ("assaf_os.os_receipts", "c")
    assert "'target_changed'" in definition and "'superseded'" not in definition
    assert before["os_import_batches_workspace_id_source_digest_key"] == (
        "assaf_os.os_import_batches", "u", "UNIQUE (workspace_id, source_digest)",
    )
    with addendum_db.engine.connect() as c:
        after = _constraints(c)
    assert "os_import_batches_workspace_id_source_digest_key" not in after
    assert after["os_import_batches_workspace_epoch_digest_key"] == (
        "assaf_os.os_import_batches", "u", "UNIQUE (workspace_id, authority_epoch, source_digest)",
    )
    for name in ("os_proposals_status_check", "os_receipts_status_check"):
        assert "'superseded'" in after[name][2] and "'abandoned'" in after[name][2]
    # Every other V2-14 constraint is untouched, byte for byte.
    replaced = {
        "os_proposals_status_check", "os_receipts_status_check",
        "os_import_batches_workspace_id_source_digest_key",
    }
    assert {k: v for k, v in before.items() if k not in replaced} == {
        k: v for k, v in after.items()
        if k in before and k not in replaced
    }
    added = set(after) - set(before)
    assert {k for k in added if after[k][0].split(".")[1] not in NEW_TABLES} == set().union(
        *CORE_CONSTRAINTS.values()
    ) - replaced


@pytest.mark.parametrize("table", sorted(NEW_TABLES))
def test_new_table_columns_are_exact(addendum_db, table):
    with addendum_db.engine.connect() as c:
        rows = c.execute(text(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
            "pg_get_expr(d.adbin, d.adrelid) FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
            "WHERE a.attrelid=to_regclass('assaf_os.' || :t) AND a.attnum > 0 "
            "AND NOT a.attisdropped ORDER BY a.attnum"
        ), {"t": table}).all()
    assert [tuple(row) for row in rows] == list(COLUMNS[table])


@pytest.mark.parametrize("table", sorted(ADDED_COLUMNS))
def test_core_columns_added_exactly(addendum_db, table):
    with addendum_db.engine.connect() as c:
        rows = c.execute(text(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
            "pg_get_expr(d.adbin, d.adrelid) FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
            "WHERE a.attrelid=to_regclass('assaf_os.' || :t) AND a.attnum > 0 "
            "AND NOT a.attisdropped ORDER BY a.attnum"
        ), {"t": table}).all()
    added = [tuple(row) for row in rows][-len(ADDED_COLUMNS[table]):]
    assert added == list(ADDED_COLUMNS[table])


@pytest.mark.parametrize("table", sorted(NEW_TABLES))
def test_new_table_keys_and_checks_are_exact(addendum_db, table):
    with addendum_db.engine.connect() as c:
        rows = c.execute(text(
            "SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid=to_regclass('assaf_os.' || :t)"
        ), {"t": table}).all()
    assert {name: d for name, kind, d in rows if kind in {"p", "u", "f"}} == KEYS[table]
    assert {name for name, kind, _ in rows if kind == "c"} == {
        f"{table}_{suffix}_check" for suffix in CHECKS[table]
    }
    assert {kind for _, kind, _ in rows} <= {"p", "u", "f", "c"}


def test_indexes_and_no_other_objects(addendum_db):
    with addendum_db.engine.connect() as c:
        indexes = dict(c.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='assaf_os' "
            "AND indexname NOT IN (SELECT conname FROM pg_constraint "
            "WHERE connamespace='assaf_os'::regnamespace)"
        )).all())
        for name, definition in INDEXES.items():
            assert indexes[name] == definition
        assert {name for name, d in indexes.items()
                if any(f"ON assaf_os.{t} " in d for t in NEW_TABLES)} == {
            "os_statement_lines_matched_expense_key", "os_assistant_audit_credential_occurred_idx",
        }
        assert set(c.scalars(text(
            "SELECT DISTINCT c.relkind FROM pg_class c JOIN pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname='assaf_os'"
        ))) == {"r", "i"}
        assert set(c.scalars(text(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='assaf_os'"
        ))) == {"expense_source_key_sha256", "require_pending_assistant_proposal_insert"}
        assert not c.scalar(text(
            "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgrelid IN "
            "(SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='assaf_os' AND c.relname <> 'os_proposals')"
        ))
        for table, names in CORE_CONSTRAINTS.items():
            assert names <= set(c.scalars(text(
                "SELECT conname FROM pg_constraint WHERE conrelid=to_regclass('assaf_os.' || :t)"
            ), {"t": table}))


def test_migration_grants_nothing_and_public_has_no_access(addendum_db):
    with addendum_db.engine.connect() as c:
        relations = c.execute(text(
            "SELECT c.relname, c.relacl IS NULL FROM pg_class c JOIN pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname='assaf_os' AND c.relkind='r'"
        )).all()
        assert {name for name, no_acl in relations if no_acl} >= NEW_TABLES
        assert not c.scalar(text(
            "SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='assaf_os' "
            "AND a.attacl IS NOT NULL"
        ))
        assert not c.scalar(text("""
            SELECT EXISTS(SELECT 1 FROM pg_namespace n CROSS JOIN LATERAL
              aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) a
              WHERE n.nspname='assaf_os' AND a.grantee=0)
            OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) a
              WHERE n.nspname='assaf_os' AND a.grantee=0)
            OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
              CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) a
              WHERE n.nspname='assaf_os' AND a.grantee=0)
            OR has_schema_privilege('public', 'assaf_os', 'USAGE,CREATE')
            OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname='assaf_os' AND c.relkind='r' AND (
                has_table_privilege('public', c.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR has_any_column_privilege('public', c.oid, 'SELECT,INSERT,UPDATE,REFERENCES')))
        """))


def test_valid_rows_are_accepted(db):
    insert(db, "os_statements", valid("os_statements"))
    for table in sorted(NEW_TABLES - {"os_statements"}):
        insert(db, table, valid(table))
    insert(db, "os_statement_lines", valid(
        "os_statement_lines", line_key="b" * 64, original_amount_minor=1500,
        original_currency="USD", installment_n=2, installment_of=3, category="Ads",
        path="Ads", platform="Meta", is_subscription=True, rule_id=RULE,
        assistant_tag="personal", owner_override={"tag": "business"}, expense_id="expense",
        matched_expense_id="expense2", expense_revision_at_import=1,
    ))
    insert(db, "os_statement_lines", valid(
        "os_statement_lines", line_key="c" * 64, tag="personal", **dict.fromkeys(REQUIRED_FACTS),
    ))
    insert(db, "os_merchant_rules", valid(
        "os_merchant_rules", rule_id="MR_" + "b" * 24, tag="personal", merchant_key=None,
        merchant_key_hmac="b" * 64,
    ))
    insert(db, "os_merchant_rules", valid(
        "os_merchant_rules", rule_id="MR_" + "c" * 24, match="prefix", merchant_key_hmac=HEX,
        aliases=["shop"] * 10, label="x" * 80, category="Ads", path="Ads", platform="Meta",
        is_subscription=False, active=False,
    ))
    insert(db, "os_assistant_audit", valid(
        "os_assistant_audit", credential_id=CRED, ok=False, line_count=300, bytes=131072,
        proposal_id="batch",
    ))
    assert db.execute(text(
        "SELECT state, draft.draft_revision, draft.overrides, draft.choices, rule.aliases, "
        "rule.active, cred.use_count, audit.line_count, audit.bytes, stmt.status "
        "FROM assaf_os.os_statement_lines line, assaf_os.os_statement_review_drafts draft, "
        "assaf_os.os_merchant_rules rule, assaf_os.os_assistant_credentials cred, "
        "assaf_os.os_assistant_audit audit, assaf_os.os_statements stmt "
        "WHERE line.line_key=:key AND rule.rule_id=:rule AND audit.credential_id IS NULL"
    ), {"key": HEX, "rule": RULE}).one() == (
        "active", 0, {}, {}, [], True, 0, 0, 0, "active",
    )


INVALID = [
    ("os_statements", {"statement_id": "STM_" + "A" * 32}, CHECK, "statement_id_check"),
    ("os_statements", {"statement_id": "STM_" + "a" * 31}, CHECK, "statement_id_check"),
    ("os_statements", {"account_label": "Card 12345678"}, CHECK, "account_label_check"),
    ("os_statements", {"account_label": "x" * 41}, TOO_LONG, None),
    ("os_statements", {"cycle": "2026-13"}, CHECK, "cycle_check"),
    ("os_statements", {"cycle": "202610"}, CHECK, "cycle_check"),
    ("os_statements", {"status": "pending"}, CHECK, "status_check"),
    ("os_statements", {"line_count": -1}, CHECK, "line_count_check"),
    ("os_statements", {"lines_digest": "A" * 64}, CHECK, "lines_digest_check"),
    ("os_statements", {"lines_digest": "a" * 63}, CHECK, "lines_digest_check"),
    ("os_statements", {"imported_version": 0}, CHECK, "imported_version_check"),
    ("os_statements", {"cycle_to": "2026-09-09"}, CHECK, "cycle_range_check"),
    ("os_statements", {"created_at": NOW, "updated_at": "2026-10-15T00:00:00Z"}, CHECK,
     "updated_check"),
    ("os_statements", {"charge_date": None}, NOT_NULL, None),
    ("os_statements", {"current_proposal_id": "missing"}, FK, "current_proposal_fkey"),
    ("os_statements", {"approved_proposal_id": "missing"}, FK, "approved_proposal_fkey"),
    ("os_statements", {"workspace_id": "two", "current_proposal_id": "batch"}, FK,
     "current_proposal_fkey"),
    ("os_statement_lines", {"line_key": "G" * 64}, CHECK, "line_key_check"),
    ("os_statement_lines", {"tag": "shared"}, CHECK, "tag_check"),
    ("os_statement_lines", {"tag": None}, NOT_NULL, None),
    ("os_statement_lines", {"state": "deleted"}, CHECK, "state_check"),
    ("os_statement_lines", {"amount_minor": 0}, CHECK, "amount_minor_check"),
    ("os_statement_lines", {"amount_minor": -5}, CHECK, "amount_minor_check"),
    ("os_statement_lines", {"kind": "refund"}, CHECK, "kind_check"),
    ("os_statement_lines", {"currency": "ils"}, CHECK, "currency_check"),
    ("os_statement_lines", {"original_amount_minor": 0, "original_currency": "USD"}, CHECK,
     "original_amount_minor_check"),
    ("os_statement_lines", {"original_amount_minor": 1, "original_currency": "US1"}, CHECK,
     "original_currency_check"),
    ("os_statement_lines", {"original_amount_minor": 1}, CHECK, "original_pair_check"),
    ("os_statement_lines", {"original_currency": "USD"}, CHECK, "original_pair_check"),
    ("os_statement_lines", {"installment_n": 1}, CHECK, "installment_check"),
    ("os_statement_lines", {"installment_of": 3}, CHECK, "installment_check"),
    ("os_statement_lines", {"installment_n": 0, "installment_of": 3}, CHECK, "installment_check"),
    ("os_statement_lines", {"installment_n": 4, "installment_of": 3}, CHECK, "installment_check"),
    ("os_statement_lines", {"merchant_display": "x" * 81}, TOO_LONG, None),
    ("os_statement_lines", {"merchant_key": "x" * 161}, TOO_LONG, None),
    ("os_statement_lines", {"category": "x" * 41}, TOO_LONG, None),
    ("os_statement_lines", {"rule_id": "MR_" + "a" * 23}, CHECK, "rule_id_check"),
    ("os_statement_lines", {"assistant_tag": "unsure"}, CHECK, "assistant_tag_check"),
    ("os_statement_lines", {"owner_override": []}, CHECK, "owner_override_check"),
    ("os_statement_lines", {"expense_id": "missing"}, FK, "expense_fkey"),
    ("os_statement_lines", {"matched_expense_id": "missing"}, FK, "matched_expense_fkey"),
    ("os_statement_lines", {"expense_revision_at_import": 0}, CHECK,
     "expense_revision_at_import_check"),
    ("os_statement_lines", {"revision": 0}, CHECK, "revision_check"),
    ("os_statement_lines", {"revision": None}, NOT_NULL, None),
    ("os_statement_lines", {"statement_id": "STM_" + "b" * 32}, FK, "statement_fkey"),
    ("os_statement_lines", {"created_at": NOW, "updated_at": "2026-10-15T00:00:00Z"}, CHECK,
     "updated_check"),
    ("os_statement_batch_lines", {"line_key": "a" * 63}, CHECK, "line_key_check"),
    ("os_statement_batch_lines", {"occurrence_index": -1}, CHECK, "occurrence_index_check"),
    ("os_statement_batch_lines", {"detail": []}, CHECK, "detail_check"),
    ("os_statement_batch_lines", {"detail": None}, CHECK, "purge_check"),
    ("os_statement_batch_lines", {"purged_at": NOW}, CHECK, "purge_check"),
    ("os_statement_batch_lines", {"proposal_id": "missing"}, FK, "proposal_fkey"),
    ("os_statement_batch_lines", {"workspace_id": "two"}, FK, "proposal_fkey"),
    ("os_statement_batch_lines", {"created_at": NOW, "updated_at": "2026-10-15T00:00:00Z"},
     CHECK, "updated_check"),
    ("os_merchant_rules", {"rule_id": "MR_" + "A" * 24}, CHECK, "rule_id_check"),
    ("os_merchant_rules", {"match": "regex"}, CHECK, "match_check"),
    ("os_merchant_rules", {"tag": "shared"}, CHECK, "tag_check"),
    ("os_merchant_rules", {"merchant_key_hmac": "z" * 64}, CHECK, "merchant_key_hmac_check"),
    ("os_merchant_rules", {"merchant_key_hmac": None}, NOT_NULL, None),
    ("os_merchant_rules", {"merchant_key": None}, CHECK, "business_check"),
    ("os_merchant_rules", {"aliases": {}}, CHECK, "aliases_check"),
    ("os_merchant_rules", {"aliases": '"shop"'}, CHECK, "aliases_check"),
    ("os_merchant_rules", {"aliases": ["shop"] * 11}, CHECK, "aliases_check"),
    ("os_merchant_rules", {"label": "x" * 81}, TOO_LONG, None),
    ("os_merchant_rules", {"revision": 0}, CHECK, "revision_check"),
    ("os_merchant_rules", {"source_proposal_id": None}, NOT_NULL, None),
    ("os_merchant_rules", {"source_proposal_id": "missing"}, FK, "source_proposal_fkey"),
    ("os_merchant_rules", {"created_at": NOW, "updated_at": "2026-10-15T00:00:00Z"}, CHECK,
     "updated_check"),
    ("os_statement_review_drafts", {"draft_revision": -1}, CHECK, "draft_revision_check"),
    ("os_statement_review_drafts", {"overrides": []}, CHECK, "overrides_check"),
    ("os_statement_review_drafts", {"choices": "null"}, CHECK, "choices_check"),
    ("os_statement_review_drafts", {"choices": []}, CHECK, "choices_check"),
    ("os_statement_review_drafts", {"proposal_id": "missing"}, FK, "proposal_fkey"),
    ("os_statement_review_drafts", {"workspace_id": "two"}, FK, "proposal_fkey"),
    ("os_statement_review_drafts", {"created_at": NOW, "updated_at": "2026-10-15T00:00:00Z"},
     CHECK, "updated_check"),
    ("os_assistant_credentials", {"credential_id": "AC_" + "a" * 31}, CHECK,
     "credential_id_check"),
    ("os_assistant_credentials", {"owner_id": "@assaf"}, CHECK, "owner_id_check"),
    ("os_assistant_credentials", {"epoch": 0}, CHECK, "epoch_check"),
    ("os_assistant_credentials", {"label": "x" * 41}, TOO_LONG, None),
    ("os_assistant_credentials", {"kind": "gemini"}, CHECK, "kind_check"),
    ("os_assistant_credentials", {"scopes": "statement:propose"}, CHECK, "scopes_check"),
    ("os_assistant_credentials", {"scopes": "merchant_rules:read own_proposals:read "
                                            "statement:approve"}, CHECK, "scopes_check"),
    ("os_assistant_credentials", {"scopes": SCOPES + " statement:approve"}, TOO_LONG, None),
    ("os_assistant_credentials", {"secret_hash": "A" * 64}, CHECK, "secret_hash_check"),
    ("os_assistant_credentials", {"use_count": -1}, CHECK, "use_count_check"),
    ("os_assistant_credentials", {"expires_at": "2027-01-14T00:00:01Z"}, CHECK,
     "expiry_max_check"),
    ("os_assistant_credentials", {"expires_at": NOW}, CHECK, "expiry_check"),
    ("os_assistant_credentials", {"revoked_at": "2026-10-15T23:59:59Z"}, CHECK, "revoked_check"),
    ("os_assistant_credentials", {"expires_at": None}, NOT_NULL, None),
    ("os_assistant_audit", {"credential_id": "AC_bad"}, CHECK, "credential_id_check"),
    ("os_assistant_audit", {"operation": "x" * 33}, TOO_LONG, None),
    ("os_assistant_audit", {"reason": "x" * 65}, TOO_LONG, None),
    ("os_assistant_audit", {"line_count": -1}, CHECK, "line_count_check"),
    ("os_assistant_audit", {"bytes": -1}, CHECK, "bytes_check"),
    ("os_assistant_audit", {"client_ip_hmac": "g" * 64}, CHECK, "client_ip_hmac_check"),
    ("os_assistant_audit", {"ok": None}, NOT_NULL, None),
]
# CHECKs exercised by the dedicated tests below rather than by INVALID.
DEDICATED_CHECKS = {
    "os_statement_lines_personal_check", "os_statement_lines_business_check",
    "os_merchant_rules_personal_check",
}


def test_every_new_check_has_a_rejecting_case():
    covered = {f"{table}_{suffix}" for table, _, sqlstate, suffix in INVALID if sqlstate == CHECK}
    expected = {f"{t}_{s}_check" for t, suffixes in CHECKS.items() for s in suffixes}
    assert covered | DEDICATED_CHECKS == expected


@pytest.mark.parametrize("table,changes,sqlstate,suffix", INVALID)
def test_constraint_rejects(db, table, changes, sqlstate, suffix):
    prepare(db, table)
    with refused(db, sqlstate, suffix and f"{table}_{suffix}"):
        insert(db, table, valid(table) | changes)


@pytest.mark.parametrize("table", sorted(NEW_TABLES))
def test_every_new_table_is_keyed_and_bound_to_its_workspace(db, table):
    prepare(db, table)
    values = valid(table)
    insert(db, table, values)
    # Change only what another unique key covers, so the primary key is what collides.
    other = {
        "os_statements": {"charge_date": "2026-11-15"},
        "os_merchant_rules": {"merchant_key_hmac": "b" * 64},
        "os_assistant_credentials": {"secret_hash": "b" * 64},
    }.get(table, {})
    with refused(db, UNIQUE, f"{table}_pkey"):
        insert(db, table, values | other)
    with refused(db, FK):
        insert(db, table, values | other | {"workspace_id": "missing"})


@pytest.mark.parametrize("column", REQUIRED_FACTS)
def test_business_line_requires_its_facts(db, column):
    prepare(db, "os_statement_lines")
    with refused(db, CHECK, "os_statement_lines_business_check"):
        insert(db, "os_statement_lines", valid("os_statement_lines", **{column: None}))


@pytest.mark.parametrize("detail", PERSONAL_NULL, ids=lambda d: "+".join(d))
def test_personal_line_keeps_no_fact_classification_or_expense(db, detail):
    prepare(db, "os_statement_lines")
    personal = valid("os_statement_lines", tag="personal", **dict.fromkeys(REQUIRED_FACTS))
    insert(db, "os_statement_lines", personal)
    with refused(db, CHECK, "os_statement_lines_personal_check"):
        insert(db, "os_statement_lines", personal | {"line_key": "b" * 64} | detail)


@pytest.mark.parametrize("detail", PERSONAL_RULE_DETAIL, ids=lambda d: f"{d}")
def test_personal_rule_keeps_only_its_keyed_hmac(db, detail):
    personal = valid("os_merchant_rules", tag="personal", merchant_key=None)
    insert(db, "os_merchant_rules", personal)
    with refused(db, CHECK, "os_merchant_rules_personal_check"):
        insert(db, "os_merchant_rules", personal | {
            "rule_id": "MR_" + "b" * 24, "merchant_key_hmac": "b" * 64,
        } | detail)


def test_workspace_pending_seq_and_pepper_fingerprint(db):
    assert db.execute(text(
        "SELECT pending_seq, statement_pepper_fp FROM assaf_os.os_workspaces "
        "WHERE workspace_id='one'"
    )).one() == (0, None)
    db.execute(text(
        "UPDATE assaf_os.os_workspaces SET pending_seq=9007199254740993, statement_pepper_fp=:fp "
        "WHERE workspace_id='one'"
    ), {"fp": HEX})
    for assignment, sqlstate, name in (
        ("pending_seq=-1", CHECK, "os_workspaces_pending_seq_check"),
        ("pending_seq=NULL", NOT_NULL, None),
        (f"statement_pepper_fp='{'A' * 64}'", CHECK, "os_workspaces_statement_pepper_fp_check"),
        (f"statement_pepper_fp='{'a' * 63}'", CHECK, "os_workspaces_statement_pepper_fp_check"),
        (f"statement_pepper_fp='{'g' * 64}'", CHECK, "os_workspaces_statement_pepper_fp_check"),
        (f"statement_pepper_fp='{'a' * 65}'", TOO_LONG, None),
    ):
        with refused(db, sqlstate, name):
            db.execute(text(f"UPDATE assaf_os.os_workspaces SET {assignment}"))
    db.execute(text("UPDATE assaf_os.os_workspaces SET statement_pepper_fp=NULL"))


def test_proposal_statuses_and_abandoned_only_for_owner_direct(db):
    statuses = ("pending", "committed", "rejected", "expired", "target_changed",
                "superseded", "abandoned")
    for status in statuses:
        proposal(db, "direct-" + status, source="OS_DIRECT", kind="owner_direct", status=status,
                 entity="tasks", record_id="task")
    for kind, source in (("proposal", "MIA"), ("statement_batch", "OS"), ("import", "OS"),
                         ("owner_undo", "OS_DIRECT")):
        with refused(db, CHECK, "os_proposals_abandoned_kind_check"):
            proposal(db, "bad-" + kind, source=source, kind=kind, status="abandoned")
    with refused(db, CHECK, "os_proposals_status_check"):
        proposal(db, "bad-status", source="OS_DIRECT", kind="owner_direct", status="cancelled")
    proposal(db, "mia", source="MIA", kind="proposal")
    with refused(db, CHECK, "os_proposals_abandoned_kind_check"):
        db.execute(text("UPDATE assaf_os.os_proposals SET status='abandoned' "
                        "WHERE proposal_id='mia'"))
    db.execute(text("UPDATE assaf_os.os_proposals SET status='superseded' "
                    "WHERE proposal_id='mia'"))


def receipt(c, name, **changes):
    proposal(c, name, source="OS_DIRECT", kind="owner_direct", status="committed",
             entity="tasks", record_id="task")
    values = dict(
        workspace_id="one", request_id=uuid4(), proposal_id=name, epoch=1, status="committed",
        kind="owner_direct", commit_version=1, completed_at=NOW,
    ) | changes
    insert(c, "os_receipts", values)
    return values


def test_receipt_statuses_undo_shape_and_reason_codes(db):
    first = receipt(db, "first", undo_until="2026-10-16T00:00:30Z")
    assert db.scalar(text(
        "SELECT reason_codes FROM assaf_os.os_receipts WHERE proposal_id='first'"
    )) == []
    for status in ("rejected", "expired", "target_changed", "superseded", "abandoned"):
        receipt(db, "status-" + status, status=status, reason_codes=["code"] * 20)
    receipt(db, "undo", kind="owner_undo", undo_of=first["request_id"])
    receipt(db, "proposal", kind="proposal", reason_codes=["credential_revoked"])
    for name, changes, sqlstate, constraint in (
        ("s", {"status": "cancelled"}, CHECK, "os_receipts_status_check"),
        ("u1", {"kind": "owner_undo"}, CHECK, "os_receipts_undo_of_kind_check"),
        ("u2", {"kind": "proposal", "undo_of": first["request_id"]}, CHECK,
         "os_receipts_undo_of_kind_check"),
        ("t1", {"kind": "proposal", "undo_until": "2026-10-16T00:00:30Z"}, CHECK,
         "os_receipts_undo_until_kind_check"),
        ("t2", {"kind": "statement_batch", "undo_until": "2026-10-16T00:00:30Z"}, CHECK,
         "os_receipts_undo_until_kind_check"),
        ("t3", {"status": "rejected", "undo_until": "2026-10-16T00:00:30Z"}, CHECK,
         "os_receipts_undo_until_kind_check"),
        ("t4", {"status": "abandoned", "undo_until": "2026-10-16T00:00:30Z"}, CHECK,
         "os_receipts_undo_until_kind_check"),
        ("r1", {"reason_codes": {}}, CHECK, "os_receipts_reason_codes_check"),
        ("r2", {"reason_codes": '"credential_revoked"'}, CHECK, "os_receipts_reason_codes_check"),
        ("r3", {"reason_codes": ["code"] * 21}, CHECK, "os_receipts_reason_codes_check"),
        ("r4", {"reason_codes": None}, NOT_NULL, None),
    ):
        with refused(db, sqlstate, constraint):
            receipt(db, name, **changes)


def test_import_source_digest_is_unique_per_epoch(db):
    batch = dict(workspace_id="one", batch_id=uuid4(), source_binding="owner_export:book",
                 source_digest=HEX, authority_epoch=1)
    insert(db, "os_import_batches", batch)
    insert(db, "os_import_batches", batch | {"batch_id": uuid4(), "authority_epoch": 2})
    insert(db, "os_import_batches", batch | {"batch_id": uuid4(), "workspace_id": "two"})
    with refused(db, UNIQUE, "os_import_batches_workspace_epoch_digest_key"):
        insert(db, "os_import_batches", batch | {"batch_id": uuid4()})
    with refused(db, UNIQUE, "os_import_batches_workspace_epoch_digest_key"):
        insert(db, "os_import_batches", batch | {"batch_id": uuid4(), "authority_epoch": 2})


def test_statement_identity_is_unique_per_label_and_charge_date(db):
    insert(db, "os_statements", valid("os_statements"))
    insert(db, "os_statements", valid("os_statements", statement_id="STM_" + "b" * 32,
                                      charge_date="2026-11-15"))
    insert(db, "os_statements", valid("os_statements", workspace_id="two"))
    with refused(db, UNIQUE, "os_statements_label_charge_date_key"):
        insert(db, "os_statements", valid("os_statements", statement_id="STM_" + "c" * 32))


def test_a_manual_expense_matches_one_active_line_at_most(db):
    prepare(db, "os_statement_lines")
    line = valid("os_statement_lines", matched_expense_id="expense")
    insert(db, "os_statement_lines", line)
    with refused(db, UNIQUE, "os_statement_lines_matched_expense_key"):
        insert(db, "os_statement_lines", line | {"line_key": "b" * 64})
    db.execute(text("UPDATE assaf_os.os_statement_lines SET state='replaced'"))
    insert(db, "os_statement_lines", line | {"line_key": "b" * 64})
    insert(db, "os_statement_lines", line | {"line_key": "c" * 64, "state": "voided"})
    for key in ("d", "e"):
        insert(db, "os_statement_lines", line | {"line_key": key * 64, "matched_expense_id": None})
    with refused(db, UNIQUE, "os_statement_lines_matched_expense_key"):
        db.execute(text("UPDATE assaf_os.os_statement_lines SET state='active' "
                        "WHERE line_key=:key"), {"key": HEX})


def test_batch_line_purge_clears_detail_atomically(db):
    insert(db, "os_statement_batch_lines", valid("os_statement_batch_lines"))
    for assignment in ("detail=NULL", "purged_at=now()"):
        with refused(db, CHECK, "os_statement_batch_lines_purge_check"):
            db.execute(text(f"UPDATE assaf_os.os_statement_batch_lines SET {assignment}"))
    db.execute(text("UPDATE assaf_os.os_statement_batch_lines SET detail=NULL, purged_at=now()"))
    with refused(db, CHECK, "os_statement_batch_lines_purge_check"):
        db.execute(text("UPDATE assaf_os.os_statement_batch_lines SET detail='{}'"))


def test_rule_key_is_unique_per_match_kind(db):
    insert(db, "os_merchant_rules", valid("os_merchant_rules"))
    insert(db, "os_merchant_rules", valid("os_merchant_rules", rule_id="MR_" + "b" * 24,
                                          match="prefix"))
    with refused(db, UNIQUE, "os_merchant_rules_match_hmac_key"):
        insert(db, "os_merchant_rules", valid("os_merchant_rules", rule_id="MR_" + "c" * 24))


def test_credential_window_scopes_and_secret_hash(db):
    insert(db, "os_assistant_credentials", valid(
        "os_assistant_credentials", expires_at="2027-01-14T00:00:00Z",
    ))
    with refused(db, UNIQUE, "os_assistant_credentials_secret_hash_key"):
        insert(db, "os_assistant_credentials", valid(
            "os_assistant_credentials", workspace_id="two", credential_id="AC_" + "b" * 32,
        ))
    db.execute(text(
        "UPDATE assaf_os.os_assistant_credentials SET revoked_at=:at, last_used_at=:at, "
        "use_count=use_count+1"
    ), {"at": NOW})


def test_proposals_bind_an_existing_credential_of_the_same_workspace(db):
    insert(db, "os_assistant_credentials", valid("os_assistant_credentials"))
    proposal(db, "claude", source="ASSISTANT_CLAUDE", credential_id=CRED)
    proposal(db, "paste", credential_id=None)
    with refused(db, FK, "os_proposals_credential_fkey"):
        proposal(db, "unknown", source="ASSISTANT_CLAUDE", credential_id="AC_" + "b" * 32)
    with refused(db, FK, "os_proposals_credential_fkey"):
        proposal(db, "other-workspace", workspace_id="two", source="ASSISTANT_CLAUDE",
                 credential_id=CRED)


def test_addendum_is_atomic_and_requires_the_v214_constraint_names(addendum_db):
    """Runs last: rebuilds the schema, so it leaves the same migrated state behind."""
    engine = addendum_db.engine
    scripts = ledger.migration_scripts()
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA assaf_os CASCADE"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ledger, "migration_scripts", lambda: scripts[:1])
        ledger.apply_ledger_migrations(engine)
    for table, old in (
        ("os_proposals", "os_proposals_status_check"),
        ("os_receipts", "os_receipts_status_check"),
        ("os_import_batches", "os_import_batches_workspace_id_source_digest_key"),
    ):
        with engine.begin() as c:
            c.execute(text(f"ALTER TABLE assaf_os.{table} RENAME CONSTRAINT {old} TO v214b_moved"))
        with pytest.raises(SQLAlchemyError) as caught:
            ledger.apply_ledger_migrations(engine)
        assert caught.value.orig.sqlstate == "42704"
        with engine.begin() as c:
            assert list(c.scalars(text("SELECT filename FROM assaf_os.schema_migrations"))) == [
                V2_14,
            ]
            assert c.scalar(text("SELECT to_regclass('assaf_os.os_statements')")) is None
            assert c.scalar(text(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema='assaf_os' "
                "AND column_name IN ('pending_seq', 'statement_pepper_fp', 'reason_codes')"
            )) == 0
            c.execute(text(f"ALTER TABLE assaf_os.{table} RENAME CONSTRAINT v214b_moved TO {old}"))
    result = ledger.apply_ledger_migrations(engine)
    assert (result.applied, result.already) == (list(FILES), [V2_14])
