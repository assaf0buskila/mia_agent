from app.db.session import sqlalchemy_database_url


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
