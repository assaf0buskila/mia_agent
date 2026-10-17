"""Server-side cache of the last validated Assaf OS snapshot (V2-42, contract section 1).

The cache exists so the app can paint something real while the authoritative snapshot, an 11-14 s
Apps Script call, is still loading. It is never a source of truth and never a write input.

* One row per owner and workspace, overwritten in place. Emptying the table is always safe.
* remember_snapshot has one caller, the snapshot route, and receives only what
  AssafOsPort.snapshot() returned, which exists only after it passed _validate_result. It
  re-checks the binding, re-validates the shape, bounds the size, and upserts only a newer
  snapshot or a changed binding. It never raises.
* read_cached_snapshot serves a row only when every check passes: the binding columns and the
  keyed binding hash, the digest and length, the exact snapshot validator (minus only its 300 s
  freshness rule), observed_at == as_of, and an age of at most 7 days. Any failure, a missing
  table included, is a miss. It never raises and never reaches the Apps Script.
* Logging is one line per call: op, outcome and a fixed reason code. Never a payload, an owner,
  a hash, a size or an age.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.config import Settings
from app.core.owner_timing import owner_phase
from app.db.session import get_session_factory
from app.services.assaf_os import (
    API_VERSION,
    SCHEMA_VERSION,
    AssafOsError,
    AssafOsPort,
    _validate_result,
)

BINDING_DOMAIN = "assaf-os-snapshot-cache.v1"
# The Worker caps a response at 262,144 bytes. 1 KiB is left for the envelope around the snapshot.
MAX_CACHED_BYTES = 261_120
MAX_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_FUTURE_SECONDS = 300
_DB_TIMEOUT = "2s"

_LOG = logging.getLogger("mia.os_snapshot_cache")

# Not an ORM model: schema_ready() and init_db() stay unchanged, and a missing table is a miss.
_TABLE = Table(
    "owner_os_snapshot_cache",
    MetaData(),
    Column("owner_id", String(32), primary_key=True),
    Column("workspace_id", String(128), primary_key=True),
    Column("workbook_id", String(128), nullable=False),
    Column("mode", String(8), nullable=False),
    Column("api_version", String(32), nullable=False),
    Column("schema_version", String(64), nullable=False),
    Column("binding_hash", String(64), nullable=False),
    Column("snapshot_json", Text, nullable=False),
    Column("snapshot_sha256", String(64), nullable=False),
    Column("byte_length", Integer, nullable=False),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("stored_at", DateTime(timezone=True), nullable=False),
    Column("version", BigInteger, nullable=False),
)
_REPLACED_COLUMNS = (
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
)


class _Refusal(Exception):
    """A fixed reason code. The outcome (skipped, miss or failed) follows from the operation."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _now() -> datetime:
    return datetime.now(UTC)


def _log(op: str, outcome: str, reason: str) -> None:
    _LOG.info("os_snapshot_cache op=%s outcome=%s reason=%s", op, outcome, reason)


def binding_hash(settings: Settings, owner_id: str) -> str:
    """Keyed digest of everything a cached row is only valid for.

    A secret rotation, a re-pointed Script or a version bump therefore invalidates the row.
    """
    message = json.dumps(
        [
            BINDING_DOMAIN,
            API_VERSION,
            SCHEMA_VERSION,
            owner_id,
            settings.assaf_os_workspace_id,
            settings.assaf_os_workbook_id,
            settings.assaf_os_mode,
            settings.assaf_os_script_url,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    key = settings.assaf_os_hmac_secret.encode("utf-8")
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def _prove_configuration(settings: Settings, owner_id: str) -> None:
    # The port constructor re-checks the enable flag, the numeric owner allow-list and the
    # configuration shapes. It is constructed only: no Script call, no in-process cache.
    try:
        AssafOsPort(settings, owner_id, actor_source="OS")
    except AssafOsError:
        raise _Refusal("binding_mismatch") from None


def _canonical(snapshot: dict[str, Any]) -> str:
    return json.dumps(
        snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
    )


def _observed_at(snapshot: dict[str, Any]) -> datetime:
    observed = snapshot["observed_at"]
    if not isinstance(observed, str):
        raise ValueError("observed_at")
    value = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("observed_at")
    return value.astimezone(UTC)


def _as_utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError("as_of")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validate_probe(snapshot: dict[str, Any], settings: Settings) -> None:
    # The exact snapshot validator minus only its freshness rule: the probe is a shallow copy
    # that claims to have been observed now. The stored snapshot itself is never altered.
    probe = dict(snapshot)
    probe["observed_at"] = _now().isoformat()
    _validate_result("snapshot", probe, settings, {})


def _bound_transaction(session: Any) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text(f"SET LOCAL statement_timeout = '{_DB_TIMEOUT}'"))
        session.execute(text(f"SET LOCAL lock_timeout = '{_DB_TIMEOUT}'"))


# ------------------------------------------------------------------------------------ write


def _row_for_write(settings: Settings, owner_id: str, result: Any) -> dict[str, Any]:
    try:
        if not isinstance(result, dict):
            raise _Refusal("invalid_shape")
        _prove_configuration(settings, owner_id)
        binding = {
            "api_version": API_VERSION,
            "source": "google_sheets",
            "mode": settings.assaf_os_mode,
            "workbook_id": settings.assaf_os_workbook_id,
            "workspace_id": settings.assaf_os_workspace_id,
        }
        if any(result.get(key) != value for key, value in binding.items()):
            raise _Refusal("binding_mismatch")
        _validate_probe(result, settings)
        as_of = _observed_at(result)
        encoded = _canonical(result).encode("utf-8")
        if len(encoded) > MAX_CACHED_BYTES:
            raise _Refusal("too_large")
        now = _now()
        if (as_of - now).total_seconds() > MAX_FUTURE_SECONDS:
            raise _Refusal("future_as_of")
        return {
            "owner_id": owner_id,
            "workspace_id": settings.assaf_os_workspace_id,
            "workbook_id": settings.assaf_os_workbook_id,
            "mode": settings.assaf_os_mode,
            "api_version": API_VERSION,
            "schema_version": SCHEMA_VERSION,
            "binding_hash": binding_hash(settings, owner_id),
            "snapshot_json": encoded.decode("utf-8"),
            "snapshot_sha256": hashlib.sha256(encoded).hexdigest(),
            "byte_length": len(encoded),
            "as_of": as_of,
            "stored_at": now,
        }
    except _Refusal:
        raise
    except Exception:
        raise _Refusal("invalid_shape") from None


def _upsert(dialect: str, row: dict[str, Any]):
    if dialect == "postgresql":
        insert = postgresql_insert
    elif dialect == "sqlite":
        insert = sqlite_insert
    else:
        raise RuntimeError("unsupported_dialect")
    statement = insert(_TABLE).values(**row, version=1)
    excluded = statement.excluded
    # A slower, older snapshot never overwrites a newer one. A changed binding always replaces.
    return statement.on_conflict_do_update(
        index_elements=[_TABLE.c.owner_id, _TABLE.c.workspace_id],
        set_={
            **{name: excluded[name] for name in _REPLACED_COLUMNS},
            "version": _TABLE.c.version + 1,
        },
        where=or_(
            _TABLE.c.binding_hash != excluded.binding_hash,
            excluded.as_of > _TABLE.c.as_of,
        ),
    ).returning(_TABLE.c.version)


def _store(row: dict[str, Any]) -> bool:
    session = get_session_factory()()
    try:
        _bound_transaction(session)
        result = session.execute(_upsert(session.get_bind().dialect.name, row))
        # RETURNING is the signal: the driver's rowcount for an upsert is -1 on psycopg. A row
        # comes back only when the snapshot was inserted or replaced, never when it was not newer.
        stored = result.first() is not None
        session.commit()
        return stored
    finally:
        session.close()


def remember_snapshot(settings: Settings, owner_id: str, result: Any) -> None:
    """Store a validated snapshot after the response is sent. Never raises, never delays."""
    try:
        stored = _store(_row_for_write(settings, owner_id, result))
    except _Refusal as refusal:
        _log("write", "skipped", refusal.reason)
    except Exception:
        _log("write", "failed", "db_unavailable")
    else:
        if stored:
            _log("write", "stored", "stored")
        else:
            _log("write", "skipped", "not_newer")


# ------------------------------------------------------------------------------------- read


def _reject_constant(_name: str) -> None:
    raise ValueError("json_constant")


def _fetch(settings: Settings, owner_id: str):
    try:
        with owner_phase("db", "os_read"):
            session = get_session_factory()()
            try:
                _bound_transaction(session)
                return (
                    session.execute(
                        select(_TABLE).where(
                            _TABLE.c.owner_id == owner_id,
                            _TABLE.c.workspace_id == settings.assaf_os_workspace_id,
                        )
                    )
                    .mappings()
                    .first()
                )
            finally:
                session.close()
    except Exception:
        raise _Refusal("db_unavailable") from None


def _hit(settings: Settings, owner_id: str) -> dict[str, Any]:
    _prove_configuration(settings, owner_id)
    row = _fetch(settings, owner_id)
    if row is None:
        raise _Refusal("no_row")

    expected = binding_hash(settings, owner_id).encode("ascii")
    if (
        row["owner_id"] != owner_id
        or row["workspace_id"] != settings.assaf_os_workspace_id
        or row["workbook_id"] != settings.assaf_os_workbook_id
        or row["mode"] != settings.assaf_os_mode
        or row["api_version"] != API_VERSION
        or row["schema_version"] != SCHEMA_VERSION
        or not hmac.compare_digest(str(row["binding_hash"]).encode("utf-8"), expected)
    ):
        raise _Refusal("binding_mismatch")

    raw = row["snapshot_json"]
    encoded = raw.encode("utf-8") if isinstance(raw, str) else b""
    if (
        not encoded
        or len(encoded) > MAX_CACHED_BYTES
        or row["byte_length"] != len(encoded)
        or not hmac.compare_digest(
            str(row["snapshot_sha256"]).encode("utf-8"),
            hashlib.sha256(encoded).hexdigest().encode("ascii"),
        )
    ):
        raise _Refusal("digest_mismatch")

    try:
        snapshot = json.loads(raw, parse_constant=_reject_constant)
        if not isinstance(snapshot, dict):
            raise ValueError("snapshot")
        _validate_probe(snapshot, settings)
        as_of = _as_utc(row["as_of"])
        version = row["version"]
        if _observed_at(snapshot) != as_of or type(version) is not int or version < 1:
            raise ValueError("row")
    except (KeyError, TypeError, ValueError, OverflowError):
        raise _Refusal("invalid_shape") from None

    elapsed = (_now() - as_of).total_seconds()
    if elapsed < -MAX_FUTURE_SECONDS:
        raise _Refusal("future_as_of")
    if elapsed > MAX_AGE_SECONDS:
        raise _Refusal("too_old")
    return {
        "api_version": API_VERSION,
        "source": "google_sheets",
        "mode": settings.assaf_os_mode,
        "workbook_id": settings.assaf_os_workbook_id,
        "workspace_id": settings.assaf_os_workspace_id,
        "cached": True,
        "as_of": snapshot["observed_at"],
        "age_seconds": min(MAX_AGE_SECONDS, max(0, math.floor(elapsed))),
        "cache_version": version,
        "snapshot": snapshot,
    }


def read_cached_snapshot(settings: Settings, owner_id: str) -> dict[str, Any]:
    """The stored snapshot inside its envelope, or {"cached": False}. Never raises."""
    try:
        hit = _hit(settings, owner_id)
    except _Refusal as refusal:
        _log("read", "failed" if refusal.reason == "db_unavailable" else "miss", refusal.reason)
        return {"cached": False}
    except Exception:
        _log("read", "miss", "invalid_shape")
        return {"cached": False}
    _log("read", "hit", "hit")
    return hit
