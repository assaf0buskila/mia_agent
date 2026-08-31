import inspect
from pathlib import Path

from app.db import migrate as migrate_module
from app.db.migrate import POSTGRES_ONLY, apply_migrations
from app.db.models import ApprovalRow
from app.db.session import make_engine
from app.workers import migrate as migrate_worker
from sqlalchemy import text

_STUB_TABLES = (
    "ai_runs",
    "approvals",
    "canonical_events",
    "channel_identities",
    "idempotency_records",
    "lead_follow_ups",
    "lead_sales_state",
    "leads",
    "meetings",
    "owner_briefs",
    "owner_tasks",
    "owner_weeklies",
    "tool_runs",
    "voice_transcripts",
    "webhook_events",
)


def _create_stub_tables(engine) -> None:
    with engine.begin() as conn:
        for table in _STUB_TABLES:
            conn.execute(
                text(f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY)")
            )


def _schema_migration_filenames(engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT filename FROM schema_migrations")).fetchall()
    return [row[0] for row in rows]


def test_apply_migrations_first_run_sqlite(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        _create_stub_tables(engine)
        summary = apply_migrations(engine)

        assert "20260821_approval_campaign_resource.sql" in summary.skipped
        assert "20260901_approval_resource_id_varchar80.sql" in summary.skipped
        assert "20260901_linkedin_approval_parameters_text.sql" in summary.skipped
        assert summary.failed == ""
        assert summary.applied or summary.already

        sqlite_safe = "20260822_lead_sales_state_company_domain.sql"
        assert sqlite_safe in summary.applied or sqlite_safe in summary.already
        wa_offered = "20260822_lead_sales_state_whatsapp_handoff_offered.sql"
        assert wa_offered in summary.applied or wa_offered in summary.already
        scan_cols = "20260822_existing_db_scan_columns.sql"
        assert scan_cols in summary.applied or scan_cols in summary.already

        recorded = _schema_migration_filenames(engine)
        assert recorded
        assert "20260821_approval_campaign_resource.sql" not in recorded
        assert "20260901_approval_resource_id_varchar80.sql" not in recorded
        assert "20260901_linkedin_approval_parameters_text.sql" not in recorded
        assert sqlite_safe in recorded
    finally:
        engine.dispose()


def test_apply_migrations_second_run_moves_to_already(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    try:
        _create_stub_tables(engine)
        first = apply_migrations(engine)
        assert first.failed == ""

        second = apply_migrations(engine)

        assert second.failed == ""
        assert second.applied == []
        assert "20260821_approval_campaign_resource.sql" in second.skipped
        assert "20260901_approval_resource_id_varchar80.sql" in second.skipped
        assert "20260901_linkedin_approval_parameters_text.sql" in second.skipped
        assert set(first.applied).issubset(set(second.already))
    finally:
        engine.dispose()


def test_recipient_delivery_status_migration_marks_existing_rows_legacy(
    tmp_path: Path,
) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'delivery-status.db'}")
    try:
        _create_stub_tables(engine)
        summary = apply_migrations(engine)
        assert summary.failed == ""
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO owner_notification_recipient_claims "
                    "(kind, lead_id, notification_key, recipient_id, claimed_at) "
                    "VALUES ('website_owner_handoff', 'lead_1', '', '111', 'now')"
                )
            )
        with engine.connect() as conn:
            status = conn.execute(
                text(
                    "SELECT delivery_status FROM owner_notification_recipient_claims "
                    "WHERE lead_id = 'lead_1'"
                )
            ).scalar_one()
        assert status == "legacy"
    finally:
        engine.dispose()


def test_postgres_only_file_does_not_split_on_comment_semicolons() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "20260821_approval_campaign_resource.sql"
    )
    statements = migrate_module._split_statements(path.read_text(encoding="utf-8"))
    assert statements
    joined = " ".join(statements).lower()
    assert "sqlite fresh" not in joined
    assert all(item.upper().startswith(("ALTER", "CREATE", "DROP")) for item in statements)


def test_migration_sql_comments_do_not_contain_semicolons() -> None:
    root = Path(__file__).resolve().parents[2] / "migrations"
    offenders: list[str] = []
    for path in sorted(root.glob("*.sql")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("--") and ";" in stripped:
                offenders.append(f"{path.name}: {stripped}")
    assert offenders == []


def test_migration_sql_is_postgres_parseable_no_sqlite_autoincrement() -> None:
    root = Path(__file__).resolve().parents[2] / "migrations"
    offenders = [
        path.name
        for path in sorted(root.glob("*.sql"))
        if "AUTOINCREMENT" in path.read_text(encoding="utf-8").upper()
    ]
    assert offenders == []


def test_postgres_only_skip_contract() -> None:
    assert POSTGRES_ONLY == frozenset(
        {
            "20260821_approval_campaign_resource.sql",
            "20260901_approval_resource_id_varchar80.sql",
            "20260901_linkedin_approval_parameters_text.sql",
        }
    )


def test_approval_resource_id_schema_matches_bounded_workflows() -> None:
    assert ApprovalRow.__table__.c.resource_id.type.length == 80


def test_approval_resource_id_postgres_migration_widens_existing_column() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "20260901_approval_resource_id_varchar80.sql"
    )
    statements = migrate_module._split_statements(path.read_text(encoding="utf-8"))
    assert statements == [
        "ALTER TABLE approvals ALTER COLUMN resource_id TYPE VARCHAR(80)"
    ]


def test_migration_modules_do_not_import_integrations() -> None:
    migrate_source = inspect.getsource(migrate_module)
    worker_source = inspect.getsource(migrate_worker)
    for source in (migrate_source, worker_source):
        assert "MessagePort" not in source
        assert "app.integrations" not in source


def test_migrations_dir_finds_image_layout_when_module_is_site_packages(
    tmp_path: Path,
) -> None:
    sql_dir = tmp_path / "image" / "migrations"
    sql_dir.mkdir(parents=True)
    (sql_dir / "20260821_example.sql").write_text("-- example\n", encoding="utf-8")
    site = tmp_path / "site-packages" / "app" / "db"
    site.mkdir(parents=True)
    module_file = site / "migrate.py"
    module_file.write_text("", encoding="utf-8")
    found = migrate_module.migrations_dir_for(
        module_file,
        image_dir=sql_dir,
        cwd=tmp_path / "empty-cwd",
    )
    assert found == sql_dir


def test_migrate_worker_creates_tables_before_sql() -> None:
    source = inspect.getsource(migrate_worker.main)
    assert "init_db()" in source
    assert source.index("init_db()") < source.index("apply_migrations")


def test_apply_file_uses_savepoints_for_postgres_duplicates() -> None:
    source = inspect.getsource(migrate_module._apply_file)
    assert "begin_nested" in source


def test_apply_migrations_does_not_call_get_engine() -> None:
    source = inspect.getsource(migrate_module.apply_migrations)
    assert "get_engine" not in source


def test_migration_summary_json_shape() -> None:
    from app.db.migrate import MigrationSummary

    payload = MigrationSummary().model_dump()
    assert set(payload) == {"applied", "skipped", "already", "failed"}
    assert payload["failed"] == ""
