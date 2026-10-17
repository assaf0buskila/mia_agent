"""Shared helpers for the V2-42 snapshot cache tests. Not collected: no test_ prefix.

Everything here restates the frozen contract on purpose, independently of
app.services.assaf_os_snapshot_cache, so a test can seed a row the way the contract describes it
and then see whether the module under test agrees.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.db.migrate import _split_statements
from sqlalchemy import DateTime, bindparam, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

MIGRATION_NAME = "20261016_owner_os_snapshot_cache.sql"
MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / MIGRATION_NAME
TABLE = "owner_os_snapshot_cache"
BINDING_PREFIX = "assaf-os-snapshot-cache.v1"
MAX_CACHED_BYTES = 261_120
SEVEN_DAYS = 7 * 24 * 60 * 60
COLUMNS = (
    "owner_id",
    "workspace_id",
    "workbook_id",
    "mode",
    "api_version",
    "schema_version",
    "binding_hash",
    "snapshot_json",
    "snapshot_sha256",
    "byte_length",
    "as_of",
    "stored_at",
    "version",
)


def apply_migration(engine: Engine, times: int = 1) -> None:
    statements = _split_statements(MIGRATION.read_text(encoding="utf-8"))
    for _ in range(times):
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))


def sqlite_cache(path: Path) -> tuple[Engine, sessionmaker]:
    """A file SQLite database holding only the migrated cache table, with real pooling."""
    engine = create_engine(f"sqlite:///{path}")
    apply_migration(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def canonical(snapshot: Any) -> str:
    return json.dumps(
        snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    )


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def as_utc(value: Any) -> datetime:
    """A column value as an aware UTC datetime (SQLite hands back naive text or datetimes)."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def binding_hash(
    secret: str,
    *,
    owner_id: str,
    workspace_id: str,
    workbook_id: str,
    mode: str,
    script_url: str,
    api_version: str = "assaf-os.v1",
    schema_version: str = "assaf-os-unified.v1",
) -> str:
    message = json.dumps(
        [
            BINDING_PREFIX,
            api_version,
            schema_version,
            owner_id,
            workspace_id,
            workbook_id,
            mode,
            script_url,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def config_binding(config, owner_id: str, **changes: str) -> str:
    values = {
        "owner_id": owner_id,
        "workspace_id": config.assaf_os_workspace_id,
        "workbook_id": config.assaf_os_workbook_id,
        "mode": config.assaf_os_mode,
        "script_url": config.assaf_os_script_url,
    }
    secret = changes.pop("secret", config.assaf_os_hmac_secret)
    values.update(changes)
    return binding_hash(secret, **values)


def seed(
    factory: sessionmaker,
    config,
    owner_id: str,
    snapshot: dict[str, Any],
    *,
    as_of: datetime | None = None,
    stored_at: datetime | None = None,
    version: int = 1,
    **overrides: Any,
) -> dict[str, Any]:
    """Write one row exactly as the contract describes it. Any column can be overridden."""
    raw = canonical(snapshot)
    encoded = raw.encode("utf-8")
    row: dict[str, Any] = {
        "owner_id": owner_id,
        "workspace_id": config.assaf_os_workspace_id,
        "workbook_id": config.assaf_os_workbook_id,
        "mode": config.assaf_os_mode,
        "api_version": "assaf-os.v1",
        "schema_version": "assaf-os-unified.v1",
        "binding_hash": config_binding(config, owner_id),
        "snapshot_json": raw,
        "snapshot_sha256": hashlib.sha256(encoded).hexdigest(),
        "byte_length": len(encoded),
        "as_of": as_of if as_of is not None else parse_time(snapshot["observed_at"]),
        "stored_at": stored_at if stored_at is not None else datetime.now(UTC),
        "version": version,
    }
    row.update(overrides)
    names = ", ".join(COLUMNS)
    values = ", ".join(f":{name}" for name in COLUMNS)
    insert = text(f"INSERT INTO {TABLE} ({names}) VALUES ({values})").bindparams(
        bindparam("as_of", type_=DateTime(timezone=True)),
        bindparam("stored_at", type_=DateTime(timezone=True)),
    )
    with factory() as db:
        db.execute(
            text(f"DELETE FROM {TABLE} WHERE owner_id = :o AND workspace_id = :w"),
            {"o": row["owner_id"], "w": row["workspace_id"]},
        )
        db.execute(insert, row)
        db.commit()
    return row


def read_row(factory: sessionmaker, owner_id: str, workspace_id: str) -> dict[str, Any] | None:
    with factory() as db:
        found = (
            db.execute(
                text(
                    f"SELECT {', '.join(COLUMNS)} FROM {TABLE} "
                    "WHERE owner_id = :o AND workspace_id = :w"
                ),
                {"o": owner_id, "w": workspace_id},
            )
            .mappings()
            .first()
        )
    return None if found is None else dict(found)


def row_count(factory: sessionmaker) -> int:
    with factory() as db:
        return int(db.execute(text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one())


def snapshot_at(
    base: dict[str, Any],
    when: datetime,
    *,
    marker: str = "",
    zulu: bool = False,
) -> dict[str, Any]:
    """A copy of a valid snapshot observed at `when`, optionally tagged through `issues`."""
    value = json.loads(json.dumps(base))
    stamp = when.astimezone(UTC)
    value["observed_at"] = (
        stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"
        if zulu
        else stamp.isoformat()
    )
    value["issues"] = [marker] if marker else []
    return value


def padded_to(base: dict[str, Any], target_bytes: int, *, char: str = "x") -> dict[str, Any]:
    """A valid snapshot whose canonical UTF-8 length is exactly target_bytes."""
    value = json.loads(json.dumps(base))
    value["issues"] = [""]
    current = len(canonical(value).encode("utf-8"))
    width = len(char.encode("utf-8"))
    missing = target_bytes - current
    assert missing >= 0, missing
    whole, rest = divmod(missing, width)
    value["issues"] = [char * whole + "x" * rest]  # one ASCII filler byte fixes the parity
    assert len(canonical(value).encode("utf-8")) == target_bytes
    return value
