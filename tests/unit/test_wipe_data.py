from pathlib import Path

from app.db.migrate import apply_migrations
from app.db.session import make_engine
from app.db.wipe import wipe_all_data
from sqlalchemy import text

from tests.unit.test_migrate import _create_stub_tables


def test_wipe_all_data_clears_rows_but_keeps_schema_migrations(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'wipe.db'}")
    try:
        _create_stub_tables(engine)
        apply_migrations(engine)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO leads (id) VALUES (1)"))
        with engine.connect() as conn:
            mig_before = conn.execute(
                text("SELECT COUNT(*) FROM schema_migrations")
            ).scalar_one()
        wiped = wipe_all_data(engine)
        assert "leads" in wiped
        with engine.connect() as conn:
            lead_count = conn.execute(text("SELECT COUNT(*) FROM leads")).scalar_one()
            mig_after = conn.execute(
                text("SELECT COUNT(*) FROM schema_migrations")
            ).scalar_one()
        assert lead_count == 0
        assert mig_after == mig_before
        assert mig_before > 0
    finally:
        engine.dispose()
