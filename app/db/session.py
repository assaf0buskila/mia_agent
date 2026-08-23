from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.db.base import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def sqlalchemy_database_url(url: str) -> str:
    """Pin Postgres DSNs to psycopg3. sqlite and explicit dialects are unchanged."""
    raw = url.strip()
    scheme = raw.split("://", 1)[0].lower()
    if scheme in {"postgres", "postgresql"}:
        return "postgresql+psycopg://" + raw.split("://", 1)[1]
    return raw


def make_engine(url: str) -> Engine:
    dsn = sqlalchemy_database_url(url)
    kwargs: dict = {}
    if dsn.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in dsn:
            kwargs["poolclass"] = StaticPool
    return create_engine(dsn, **kwargs)


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = make_engine(get_settings().database_url)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def init_db() -> None:
    from app.db import models as _models  # noqa: F401

    Base.metadata.create_all(bind=get_engine())


def schema_ready(engine: Engine) -> bool:
    from app.db import models as _models  # noqa: F401

    try:
        inspector = inspect(engine)
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                return False
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in existing:
                    return False
        return True
    except SQLAlchemyError:
        return False


def database_ready() -> bool:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return schema_ready(engine)
    except SQLAlchemyError:
        return False


def reset_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
