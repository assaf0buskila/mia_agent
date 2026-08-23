from pathlib import Path

from app.db.base import Base
from app.db.session import make_engine, schema_ready
from sqlalchemy import text


def test_schema_ready_false_on_empty_db(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    try:
        assert schema_ready(engine) is False
    finally:
        engine.dispose()


def test_schema_ready_true_after_create_all(tmp_path: Path) -> None:
    from app.db import models as _models  # noqa: F401

    engine = make_engine(f"sqlite:///{tmp_path / 'full.db'}")
    try:
        Base.metadata.create_all(bind=engine)
        assert schema_ready(engine) is True
    finally:
        engine.dispose()


def test_schema_ready_false_when_mapped_column_missing(tmp_path: Path) -> None:
    from app.db import models as _models  # noqa: F401

    engine = make_engine(f"sqlite:///{tmp_path / 'gap.db'}")
    try:
        Base.metadata.create_all(bind=engine)
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE lead_sales_state DROP COLUMN company_domain"))
        assert schema_ready(engine) is False
    finally:
        engine.dispose()
