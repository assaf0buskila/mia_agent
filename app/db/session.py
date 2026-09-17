"""Engine construction, plus the write-time bounded-column width guard."""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import String, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Table

from app.core.config import get_settings
from app.db.base import Base

_LOG = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


class ColumnWidthExceeded(SQLAlchemyError):
    """A write would have overflowed a bounded ``String(N)`` column.

    It subclasses ``SQLAlchemyError`` on purpose. Postgres answers an over-long
    value with ``DataError``, which is also a ``SQLAlchemyError``, so every caller
    that already handles a width overflow on the real engine handles this one too
    and the guard creates no new escape class. A plain ``ValueError`` would escape
    those handlers -- that is how one poison row wedged the delivery queue.
    """


# ---------------------------------------------------------------------------
# Column width policy.
#
# SQLite ignores VARCHAR(N) entirely, so local runs and the `checks` CI job are
# blind to an over-long write by construction; only the `postgres` job sees it.
# That arrangement is what produced the 41-char-key-into-String(40) outage. The
# guard below makes the write loud on every engine, and this is the rule it
# applies.
#
# RAISE    -- identity, key, hash, timestamp, version and controlled-vocabulary
#             columns. A truncated key is not a shorter key, it is a *wrong* key:
#             it collides with another row or breaks idempotency, silently and
#             undetectably. Loud is the only correct answer.
# TRUNCATE -- free prose written by a person or by the model, with a reason code
#             naming the column. Losing the tail of a business description beats
#             losing the lead, and the money path must not start raising because a
#             real visitor typed a long sentence.
#
# A column matching neither rule is UNCLASSIFIED: the guard raises (the safe
# answer, and the one Postgres already gives), and tests/unit/test_column_widths.py
# fails until someone classifies it. That is the mechanism that stops a column
# added next year from silently inheriting a policy nobody chose.
# ---------------------------------------------------------------------------

POLICY_RAISE = "raise"
POLICY_TRUNCATE = "truncate"

# Identifiers, keys and hashes. `_ref` covers `source_ref`, which carries a UNIQUE
# constraint -- truncating it merges two distinct activities into one row.
_IDENTITY_NAMES = frozenset({"id", "key"})
_IDENTITY_SUFFIXES = ("_id", "_key", "_hash", "_ref")

# Timestamps and dates, stored as ISO strings. A truncated timestamp is a wrong
# timestamp, and leases, expiries and dedupe windows are compared as strings.
_TIMESTAMP_NAMES = frozenset({"week_start"})
_TIMESTAMP_SUFFIXES = ("_at", "_date", "_modified")

# Version tags: a truncated version tag names a different version.
_VERSION_SUFFIXES = ("_version",)

# Controlled values: enums, reason codes, provider/model names, locators and
# numeric-valued strings. None of these is prose. An over-long one means a bug
# upstream, and truncating would substitute a value that is silently wrong -- a
# broken URL, a different enum member, a corrupted count.
_CONTROLLED_VALUE_COLUMNS = frozenset(
    {
        "action",
        "actor_role",
        "aggregate_type",
        "approver",
        "attribution_confidence",
        "automation_mode",
        "automation_scope",
        "block_reason",
        "category",
        "channel",
        "closed_value",
        "comments",
        # Bounded by the value's own rule: `sanitize_company_domain` returns None
        # above 253 characters (DNS max hostname), so 253 is provable, not hoped.
        "company_domain",
        "condition",
        "confidence",
        "deal_stage",
        "decision",
        "decision_confidence",
        "delivery_status",
        "destination",
        "embedding_model",
        "envelope_kind",
        "estimated_value",
        "event_type",
        "expected_value",
        "field_name",
        "fit",
        "follow_up_status",
        "freshness",
        "intent",
        "issue_type",
        "kind",
        "language",
        # The worker identity holding an outbox lease. Truncating it would let two
        # workers believe they hold the same lease.
        "lease_owner",
        "likes",
        "media_type",
        # Locators. A truncated URL is not a shorter URL, it is a broken one.
        "meet_link",
        "meeting_status",
        "meeting_type",
        "model",
        # `brain_entities.name` is the same logical value as `entity_key`, at the
        # same width, and `entity_key` is UNIQUE. Truncating one while raising on
        # the other would write a row whose name no longer matches its own key.
        "name",
        "next_action",
        # Normalised contact value, UNIQUE with `kind`. Truncating it merges two
        # different people into one identity.
        "normalized_value",
        "provider",
        "reach",
        "reason",
        "resolution",
        "resource_type",
        "retention_status",
        "risk",
        "saved",
        "scope",
        "site_stale",
        "source",
        "stage",
        "status",
        "stt_model",
        "stt_provider",
        "subject",
        # A pointer to the memory row that replaced this one -- an id by another name.
        "superseded_by",
        "takeover_state",
        "task_type",
        "tool",
        "trigger",
        "url",
        "views",
        "willingness_to_meet",
    }
)

# Free prose, written by a person or by the model. Truncating loses the tail of a
# sentence; raising loses the whole write, and some of these sit on the money path.
_FREE_TEXT_COLUMNS = frozenset(
    {
        "active_objection",
        "change",
        "display_name",
        "draft",
        # Diagnostic text. `last_error` is written by the delivery worker's own
        # failure path -- a guard that raised there would wedge the very queue it
        # exists to protect.
        "error",
        "evidence",
        "headline",
        "last_error",
        "metric",
        # A human-facing list of the fields a lead review is still missing. It is
        # read, never matched on.
        "missing_fields",
        "next_step",
        "outcome",
        "problem",
        "result",
        "summary",
        "title",
        "topic",
        # Free text naming the other party in an activity. Never logged: the reason
        # code names the column, never the value.
        "who",
        "why",
    }
)


def column_width_policy(column_name: str) -> str | None:
    """Return ``POLICY_RAISE``, ``POLICY_TRUNCATE``, or ``None`` when unclassified."""
    if column_name in _FREE_TEXT_COLUMNS:
        return POLICY_TRUNCATE
    if column_name in _IDENTITY_NAMES or column_name.endswith(_IDENTITY_SUFFIXES):
        return POLICY_RAISE
    if column_name in _TIMESTAMP_NAMES or column_name.endswith(_TIMESTAMP_SUFFIXES):
        return POLICY_RAISE
    if column_name.endswith(_VERSION_SUFFIXES):
        return POLICY_RAISE
    if column_name in _CONTROLLED_VALUE_COLUMNS:
        return POLICY_RAISE
    return None


_BOUNDED_COLUMNS_CACHE: dict[Table, dict[str, tuple[str, int]]] = {}


def bounded_string_columns(table: Table) -> dict[str, tuple[str, int]]:
    """Map bind key -> (column name, max length) for this table's bounded strings."""
    cached = _BOUNDED_COLUMNS_CACHE.get(table)
    if cached is None:
        cached = {
            column.key: (column.name, column.type.length)
            for column in table.columns
            if isinstance(column.type, String) and column.type.length
        }
        _BOUNDED_COLUMNS_CACHE[table] = cached
    return cached


def _rewrite_row(
    row: Any, changes: Mapping[str, str], positiontup: Sequence[str] | None
) -> Any | None:
    """Apply truncations to one DBAPI parameter row, or return None if it cannot be."""
    if positiontup is None:
        if not isinstance(row, Mapping) or any(key not in row for key in changes):
            return None
        updated = dict(row)
        updated.update(changes)
        return updated
    if not isinstance(row, (tuple, list)) or len(row) != len(positiontup):
        return None
    positions = list(positiontup)
    updated_row = list(row)
    for key, value in changes.items():
        if key not in positions:
            return None
        # The first occurrence is the VALUES/SET bind. A later duplicate is a WHERE
        # comparison, which must keep the value the caller actually asked about.
        updated_row[positions.index(key)] = value
    return tuple(updated_row)


def _apply_column_width_guard(
    statement: str, parameters: Any, context: Any, executemany: bool
) -> tuple[str, Any]:
    if context is None or not (context.isinsert or context.isupdate):
        # SELECT is deliberately untouched: Postgres does not error on an over-long
        # comparison literal either, and a guard stricter than the real engine would
        # turn harmless lookups into errors.
        return statement, parameters
    compiled = getattr(context, "compiled", None)
    table = getattr(getattr(compiled, "statement", None), "table", None)
    if table is None:
        return statement, parameters
    limits = bounded_string_columns(table)
    if not limits:
        return statement, parameters

    edits: dict[int, dict[str, str]] = {}
    for row_index, compiled_row in enumerate(context.compiled_parameters):
        for bind_key, (column_name, limit) in limits.items():
            value = compiled_row.get(bind_key)
            if not isinstance(value, str) or len(value) <= limit:
                continue
            policy = column_width_policy(column_name)
            if policy == POLICY_TRUNCATE:
                _LOG.warning(
                    "db write truncated overlong value reason=column_width_truncated "
                    "table=%s column=%s limit=%d length=%d",
                    table.name,
                    column_name,
                    limit,
                    len(value),
                )
                edits.setdefault(row_index, {})[bind_key] = value[:limit]
                continue
            if policy == POLICY_RAISE:
                _LOG.error(
                    "db write rejected overlong value reason=column_width_exceeded "
                    "table=%s column=%s limit=%d length=%d",
                    table.name,
                    column_name,
                    limit,
                    len(value),
                )
            else:
                _LOG.error(
                    "db write rejected overlong value reason=column_width_unclassified "
                    "table=%s column=%s limit=%d length=%d",
                    table.name,
                    column_name,
                    limit,
                    len(value),
                )
            raise ColumnWidthExceeded(
                f"value for {table.name}.{column_name} exceeds its declared width "
                f"(limit={limit} length={len(value)})"
            )

    if not edits:
        return statement, parameters

    positiontup = list(compiled.positiontup or []) if context.dialect.positional else None
    if executemany:
        rows = list(parameters)
        for row_index, changes in edits.items():
            rewritten = (
                _rewrite_row(rows[row_index], changes, positiontup)
                if row_index < len(rows)
                else None
            )
            if rewritten is None:
                _LOG.error(
                    "db write left overlong value reason=column_width_truncate_deferred "
                    "table=%s",
                    table.name,
                )
                return statement, parameters
            rows[row_index] = rewritten
        return statement, rows

    rewritten_single = _rewrite_row(parameters, edits.get(0, {}), positiontup)
    if rewritten_single is None:
        # The parameter row is not shaped the way this dialect's compiled bind list
        # says it should be (SQLAlchemy batched several rows into one statement).
        # Leave the value alone so the database behaves exactly as it does today
        # rather than the guard inventing a third outcome -- but say so.
        _LOG.error(
            "db write left overlong value reason=column_width_truncate_deferred table=%s",
            table.name,
        )
        return statement, parameters
    return statement, rewritten_single


def install_column_width_guard(engine: Engine) -> Engine:
    """Reject or truncate over-long bounded-string writes on every engine."""

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _before_cursor_execute(  # type: ignore[no-untyped-def]
        conn, cursor, statement, parameters, context, executemany
    ):
        return _apply_column_width_guard(statement, parameters, context, executemany)

    return engine


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
    # Unconditional, never MIA_ENV-gated: gating it to tests would re-create in a new
    # place the two-behaviours problem this guard exists to remove.
    return install_column_width_guard(create_engine(dsn, **kwargs))


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
    from app.db import site_v2 as _site_v2  # noqa: F401

    Base.metadata.create_all(bind=get_engine())


def schema_ready(engine: Engine) -> bool:
    from app.db import models as _models  # noqa: F401
    from app.db import site_v2 as _site_v2  # noqa: F401

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
