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
        # `meeting_debriefs.next_step` and `.outcome` are closed vocabularies, not
        # prose: `upsert_meeting_debrief` (app/db/store.py) drops the write outright
        # unless the value is in `ALLOWLISTED_NEXT_STEPS` / `ALLOWLISTED_OUTCOMES`
        # (app/domain/debriefs.py). Truncating a controlled value substitutes a
        # silently wrong one, which is exactly the case that must be loud. Each is
        # bounded on that one table only, so this classification reaches nothing else.
        "next_step",
        "outcome",
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


def _parameter_rows(
    parameters: Any, positiontup: Sequence[str] | None
) -> tuple[list[Any], bool] | None:
    """Split the DBAPI parameters into the row(s) actually about to be executed.

    Returns ``(rows, flat)`` where ``flat`` says ``parameters`` *was* one row and
    so one row must be handed back, or ``None`` when the shape is not one this
    guard understands.

    The ``executemany`` flag is deliberately NOT used to decide this. SQLAlchemy's
    ``insertmanyvalues`` path -- any multi-row INSERT into a table with a
    server-generated PK, which is most of them -- calls ``before_cursor_execute``
    once PER ROW with ``executemany=True`` while ``parameters`` holds that single
    flat row. Trusting the flag is what made this guard fail open: it read
    ``parameters`` as a list of rows, got a list of scalars, could not rewrite
    them, and passed the over-long value straight through *after* logging that it
    had truncated it.
    """
    if positiontup is None:
        if isinstance(parameters, Mapping):
            return [parameters], True
        if (
            isinstance(parameters, (list, tuple))
            and parameters
            and all(isinstance(row, Mapping) for row in parameters)
        ):
            return list(parameters), False
        return None
    if not isinstance(parameters, (list, tuple)):
        return None
    width = len(positiontup)
    if parameters and all(
        isinstance(row, (list, tuple)) and len(row) == width for row in parameters
    ):
        # A true executemany: a sequence of complete rows.
        return list(parameters), False
    if len(parameters) == width:
        # One flat row: a single INSERT/UPDATE, or one insertmanyvalues callback.
        return [parameters], True
    return None


def _guarded_binds(
    row: Any, positiontup: Sequence[str] | None, limits: Mapping[str, tuple[str, int]]
) -> list[tuple[Any, str, int, Any]] | None:
    """``(slot, column name, limit, value)`` for each bounded bind this row carries.

    ``slot`` is the tuple index (positional dialect) or the bind key (named
    dialect) that has to be replaced in order to truncate the value. Only the
    FIRST occurrence of a bind key is guarded: a later duplicate is a WHERE
    comparison, which must keep the value the caller actually asked about.
    """
    if positiontup is None:
        if not isinstance(row, Mapping):
            return None
        return [
            (bind_key, column_name, limit, row[bind_key])
            for bind_key, (column_name, limit) in limits.items()
            if bind_key in row
        ]
    if not isinstance(row, (list, tuple)) or len(row) != len(positiontup):
        return None
    positions = list(positiontup)
    found: list[tuple[Any, str, int, Any]] = []
    for bind_key, (column_name, limit) in limits.items():
        if bind_key not in positions:
            continue
        index = positions.index(bind_key)
        found.append((index, column_name, limit, row[index]))
    return found


def _guard_row(
    row: Any,
    positiontup: Sequence[str] | None,
    limits: Mapping[str, tuple[str, int]],
    table_name: str,
) -> Any:
    """Validate, and where the policy says so truncate, one real parameter row.

    Raises ``ColumnWidthExceeded`` for a RAISE or unclassified column. Returns the
    row to execute, which is the very same object when nothing was over-long.
    """
    binds = _guarded_binds(row, positiontup, limits)
    if binds is None:
        # Not a shape this guard can map bind keys onto, so it cannot have detected
        # an over-long value either. Say exactly that and change nothing: a guard
        # that cannot see must never claim an outcome.
        _LOG.error(
            "db write not guarded reason=column_width_guard_unsupported_shape table=%s",
            table_name,
        )
        return row

    truncations: list[tuple[str, int, int]] = []
    changes: dict[Any, str] = {}
    for slot, column_name, limit, value in binds:
        if not isinstance(value, str) or len(value) <= limit:
            continue
        policy = column_width_policy(column_name)
        if policy == POLICY_TRUNCATE:
            changes[slot] = value[:limit]
            truncations.append((column_name, limit, len(value)))
            continue
        reason = (
            "column_width_exceeded"
            if policy == POLICY_RAISE
            else "column_width_unclassified"
        )
        _LOG.error(
            "db write rejected overlong value reason=%s "
            "table=%s column=%s limit=%d length=%d",
            reason,
            table_name,
            column_name,
            limit,
            len(value),
        )
        raise ColumnWidthExceeded(
            f"value for {table_name}.{column_name} exceeds its declared width "
            f"(limit={limit} length={len(value)})"
        )

    if not changes:
        return row

    if positiontup is None:
        updated: Any = dict(row)
        updated.update(changes)
    else:
        as_list = list(row)
        for index, replacement in changes.items():
            as_list[index] = replacement
        updated = tuple(as_list)

    # Logged only here, once the rewritten row exists. The reason-code trail is this
    # repo's ground truth for what was written, so it must never report a truncation
    # that did not actually reach the cursor.
    for column_name, limit, length in truncations:
        _LOG.warning(
            "db write truncated overlong value reason=column_width_truncated "
            "table=%s column=%s limit=%d length=%d",
            table_name,
            column_name,
            limit,
            length,
        )
    return updated


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

    positiontup = list(compiled.positiontup or []) if context.dialect.positional else None
    split = _parameter_rows(parameters, positiontup)
    if split is None:
        _LOG.error(
            "db write not guarded reason=column_width_guard_unsupported_shape table=%s",
            table.name,
        )
        return statement, parameters

    rows, flat = split
    guarded = [_guard_row(row, positiontup, limits, table.name) for row in rows]
    if all(new is old for new, old in zip(guarded, rows)):
        return statement, parameters
    return statement, guarded[0] if flat else guarded


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
