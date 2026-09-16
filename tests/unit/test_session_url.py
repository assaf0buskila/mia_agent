import logging

import pytest
from app.core.config import Settings, _with_overridden_dsn_password
from app.db.session import make_engine, sqlalchemy_database_url


def test_sqlalchemy_database_url_pins_postgres_to_psycopg() -> None:
    assert (
        sqlalchemy_database_url("postgres://u:p@db:5432/mia")
        == "postgresql+psycopg://u:p@db:5432/mia"
    )
    assert (
        sqlalchemy_database_url("postgresql://u:p@db:5432/mia")
        == "postgresql+psycopg://u:p@db:5432/mia"
    )


def test_sqlalchemy_database_url_leaves_sqlite_and_explicit_dialect() -> None:
    assert sqlalchemy_database_url("sqlite:///./mia.db") == "sqlite:///./mia.db"
    explicit = "postgresql+psycopg://u:p@db:5432/mia"
    assert sqlalchemy_database_url(explicit) == explicit


def test_sqlalchemy_database_url_keeps_ssl_query() -> None:
    raw = (
        "postgres://u:p@db.rds.amazonaws.com:5432/mia"
        "?sslmode=verify-full&sslrootcert=/etc/ssl/certs/rds-global-bundle.pem"
    )
    pinned = sqlalchemy_database_url(raw)
    assert pinned.startswith("postgresql+psycopg://")
    assert "sslmode=verify-full" in pinned
    assert "sslrootcert=/etc/ssl/certs/rds-global-bundle.pem" in pinned


# --- MIA_DATABASE_PASSWORD override (chunk C8: survive RDS credential rotation) ---


def test_database_password_override_absent_leaves_url_unchanged() -> None:
    settings = Settings(database_url="postgresql://u:p@host:5432/mia", database_password="")
    assert settings.effective_database_url() == settings.database_url


def test_database_password_override_replaces_only_the_password() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://svc_user:oldpass@10.0.1.5:5432/mia",
        database_password="newpass123",
    )
    assert (
        settings.effective_database_url()
        == "postgresql+psycopg://svc_user:newpass123@10.0.1.5:5432/mia"
    )


def test_database_password_override_preserves_query_string_byte_for_byte() -> None:
    raw = (
        "postgresql://u:oldpass@db.rds.amazonaws.com:5432/mia"
        "?sslmode=verify-full&sslrootcert=/etc/ssl/certs/rds-global-bundle.pem"
    )
    settings = Settings(database_url=raw, database_password="N3w#Pass/word")
    overridden = settings.effective_database_url()
    assert overridden.startswith("postgresql://u:")
    assert "@db.rds.amazonaws.com:5432/mia" in overridden
    assert overridden.endswith(
        "?sslmode=verify-full&sslrootcert=/etc/ssl/certs/rds-global-bundle.pem"
    )


def test_database_password_override_leaves_sqlite_untouched() -> None:
    memory = Settings(database_url="sqlite:///:memory:", database_password="anything")
    assert memory.effective_database_url() == "sqlite:///:memory:"

    path = Settings(database_url="sqlite:///./mia.db", database_password="anything")
    assert path.effective_database_url() == "sqlite:///./mia.db"


def test_database_password_override_inserts_password_when_authority_has_username_only() -> None:
    settings = Settings(
        database_url="postgresql://appuser@db.example.internal:5432/mia",
        database_password="freshsecret",
    )
    assert (
        settings.effective_database_url()
        == "postgresql://appuser:freshsecret@db.example.internal:5432/mia"
    )


def test_database_password_override_anchors_on_last_at_before_host() -> None:
    # A stale, unencoded password already containing a literal "@" must not
    # shift where the host is found.
    raw = "postgresql://u:p@ssword@db.example.internal:5432/mia"
    settings = Settings(database_url=raw, database_password="rotated")
    overridden = settings.effective_database_url()
    assert overridden == "postgresql://u:rotated@db.example.internal:5432/mia"
    engine = make_engine(overridden)
    assert engine.url.host == "db.example.internal"
    assert engine.url.password == "rotated"


def test_with_overridden_dsn_password_noop_without_scheme_separator() -> None:
    assert _with_overridden_dsn_password("not-a-url", "pw") == "not-a-url"


@pytest.mark.parametrize(
    "password",
    [
        "plain",
        "has#hash",
        "has?question",
        "has/slash",
        "has@at",
        "has%percent",
        "has:colon",
        "has space",
        "mix#?/@%: space",
    ],
)
def test_database_password_override_round_trips_through_sqlalchemy(password: str) -> None:
    """The real proof of correct encoding: SQLAlchemy's own DSN parser must
    recover the exact original password, not just a string that looks right."""
    settings = Settings(
        database_url=(
            "postgresql://appuser:oldpass@db.example.internal:5432/mia?sslmode=verify-full"
        ),
        database_password=password,
    )
    overridden = settings.effective_database_url()
    engine = make_engine(overridden)
    assert engine.url.password == password
    assert engine.url.username == "appuser"
    assert engine.url.host == "db.example.internal"
    assert engine.url.port == 5432
    assert engine.url.database == "mia"
    assert engine.url.query.get("sslmode") == "verify-full"


def test_database_password_override_never_logs(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(
        database_url="postgresql://u:oldpass@host:5432/mia",
        database_password="s3cr3t#pass",
    )
    with caplog.at_level(logging.DEBUG):
        overridden = settings.effective_database_url()
    assert caplog.records == []
    assert "s3cr3t" not in caplog.text
    assert overridden not in caplog.text
