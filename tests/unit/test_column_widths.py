"""The write-time bounded-column width guard (`app/db/session.py`).

A 41-character key written into a `String(40)` column stopped the entire lead
delivery queue. SQLite ignores `VARCHAR(N)` entirely, so every local run and the
`checks` CI job is blind to that class of defect by construction; only Postgres
raises. These tests pin the guard that closes the class rather than the instance:
every bounded column carries an explicit policy, over-long identity values are
rejected loudly with a reason code, over-long prose is truncated with a reason
code and the row is still written, and the exception the guard raises is the one
the delivery worker already catches.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from app.db import models as _models  # noqa: F401  (registers tables on Base.metadata)
from app.db import site_v2 as _site_v2  # noqa: F401
from app.db.base import Base
from app.db.models import (
    CrmOutboxRow,
    CustomerRow,
    KnowledgeGapRow,
    OwnerNotificationRow,
    SalesStateRow,
)
from app.db.session import (
    POLICY_RAISE,
    POLICY_TRUNCATE,
    ColumnWidthExceeded,
    _guard_row,
    _parameter_rows,
    bounded_string_columns,
    column_width_policy,
    install_column_width_guard,
    make_engine,
)
from app.integrations.sheets import FakeSheetsPort
from app.workers.crm_delivery import CrmDeliveryWorker
from sqlalchemy import (
    Boolean,
    Float,
    Integer,
    String,
    and_,
    create_engine,
    insert,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

_REPO_ROOT = Path(__file__).resolve().parents[2]

# `text(` followed by a string literal opening with INSERT or UPDATE, matched over
# the whole file source rather than line by line. An f/r/b prefix, a triple-quoted
# body and a newline between `text(` and the verb all match here; none of them did
# under the previous line-by-line `line.split('text(')` form, which stripped only
# quotes and spaces and so read straight past an f-string prefix.
_RAW_WRITE = re.compile(
    "text\\(\\s*[frbFRB]{0,2}(\"\"\"|'''|\"|')\\s*"
    "(INSERT\\s+INTO|UPDATE)\\s+\"?(\\w+)",
    re.IGNORECASE,
)

# The one raw write the guard provably cannot reach, with the argument for why it
# is safe. `schema_migrations` is created by raw DDL in `migrate.py` and is not a
# `Base.metadata` table, so `bounded_string_columns` has nothing to guard it with.
# Its two values are repo-controlled, never visitor- or model-supplied: `filename`
# is a name from `migrations/` (VARCHAR(255), longest in-tree name is far under it)
# and `applied_at` is an ISO timestamp (VARCHAR(64)). Both are also keys, so RAISE
# is the policy they would get, which is what Postgres already does unaided.
# Anything NEW -- another file, another table -- still fails this test.
_RAW_WRITE_ALLOWED = {("app/db/migrate.py", "schema_migrations")}


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = make_engine(f"sqlite:///{tmp_path / 'widths.db'}")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _bounded_columns() -> list[tuple[object, object, int]]:
    """(table, column, limit) for every bounded String column in the schema."""
    found: list[tuple[object, object, int]] = []
    for table in Base.metadata.sorted_tables:
        for bind_key, (name, limit) in bounded_string_columns(table).items():
            found.append((table, table.columns[bind_key], limit))
            assert table.columns[bind_key].name == name
    return found


def _filler(column: object, counter: int) -> object:
    """A minimal, per-iteration-unique value of the right type for one column."""
    column_type = column.type  # type: ignore[attr-defined]
    if isinstance(column_type, String):
        value = f"v{counter}"
        return value[: column_type.length] if column_type.length else value
    if isinstance(column_type, Boolean):
        return False
    if isinstance(column_type, Integer):
        return counter
    if isinstance(column_type, Float):
        return 0.0
    raise AssertionError(
        f"unhandled column type {column_type!r} on "
        f"{column.table.name}.{column.name} - classify it before using it"  # type: ignore[attr-defined]
    )


def _row(table: object, counter: int, *, override_key: str, override: str) -> dict[str, object]:
    values = {
        column.key: _filler(column, counter)
        for column in table.columns  # type: ignore[attr-defined]
    }
    values[override_key] = override
    return values


def test_every_bounded_column_has_an_explicit_policy() -> None:
    """A new `String(N)` column cannot be added without someone choosing a policy.

    This is the mechanism that keeps the class closed. `column_width_policy`
    returns None for a name that matches neither the identity/key/hash/timestamp/
    version rule nor the explicit prose set, and this test fails until the name is
    classified. (The guard itself raises on an unclassified column, so the runtime
    is safe in the gap - but the decision is still forced here, in review.)
    """
    columns = _bounded_columns()
    assert len(columns) > 300, "the schema lost most of its bounded columns; check the walk"
    unclassified = sorted(
        f"{table.name}.{column.name}"  # type: ignore[attr-defined]
        for table, column, _ in columns
        if column_width_policy(column.name) is None  # type: ignore[attr-defined]
    )
    assert unclassified == []


def test_guard_covers_every_bounded_column(engine: Engine) -> None:
    """Every bounded column: at the limit writes, over the limit raises or truncates."""
    columns = _bounded_columns()
    counter = 0
    with engine.begin() as conn:
        for table, column, limit in columns:
            policy = column_width_policy(column.name)  # type: ignore[attr-defined]
            counter += 1
            at_limit = _row(table, counter, override_key=column.key, override="x" * limit)  # type: ignore[attr-defined]
            conn.execute(insert(table).values(**at_limit))  # type: ignore[arg-type]

            counter += 1
            over = "x" * (limit + 1)
            over_limit = _row(table, counter, override_key=column.key, override=over)  # type: ignore[attr-defined]
            label = f"{table.name}.{column.name}"  # type: ignore[attr-defined]
            if policy == POLICY_TRUNCATE:
                assert column not in set(table.primary_key.columns), (  # type: ignore[attr-defined]
                    f"{label} is a primary key; truncating it would move the row"
                )
                conn.execute(insert(table).values(**over_limit))  # type: ignore[arg-type]
                where = and_(
                    *[
                        pk == over_limit[pk.key]
                        for pk in table.primary_key.columns  # type: ignore[attr-defined]
                    ]
                )
                stored = conn.execute(select(column).where(where)).scalar_one()  # type: ignore[arg-type]
                assert stored == "x" * limit, f"{label} was not truncated to its width"
            else:
                assert policy == POLICY_RAISE, f"{label} is unclassified"
                with pytest.raises(ColumnWidthExceeded) as excinfo:
                    conn.execute(insert(table).values(**over_limit))  # type: ignore[arg-type]
                assert isinstance(excinfo.value, SQLAlchemyError)


def test_select_with_an_overlong_comparison_is_not_affected(engine: Engine) -> None:
    """Reads stay untouched: Postgres does not error on an over-long literal either.

    A guard stricter than the real engine would turn a harmless lookup into an
    error, which is a new outage rather than a fix.
    """
    with engine.begin() as conn:
        rows = conn.execute(
            select(CustomerRow.id).where(CustomerRow.id == "x" * 400)
        ).all()
    assert rows == []


def test_overlong_identity_value_is_rejected_with_a_reason_code(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """The original outage shape: a site-scoped key into the owner inbox's String(32)."""
    limit = OwnerNotificationRow.__table__.c.lead_id.type.length
    lead_id = f"site:{uuid4().hex}"
    assert len(lead_id) > limit

    with caplog.at_level(logging.ERROR, logger="app.db.session"):
        with engine.begin() as conn, pytest.raises(ColumnWidthExceeded):
            conn.execute(
                insert(OwnerNotificationRow).values(
                    kind="meeting_booked",
                    lead_id=lead_id,
                    scheduled_at="2026-09-01T09:00:00+00:00",
                    seen_at="",
                )
            )

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "reason=column_width_exceeded" in message
        and "table=owner_notifications" in message
        and "column=lead_id" in message
        for message in messages
    ), messages
    assert not any(lead_id in message for message in messages), "the value must never be logged"


def test_overlong_prose_is_truncated_logged_and_the_row_is_still_written(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Losing the tail of a headline is better than losing the lead."""
    limit = SalesStateRow.__table__.c.headline.type.length
    headline = "ניהול תוכן לעסקים קטנים " * 20
    assert len(headline) > limit

    with caplog.at_level(logging.WARNING, logger="app.db.session"):
        with engine.begin() as conn:
            conn.execute(
                insert(SalesStateRow).values(lead_id="lead_abc123", headline=headline)
            )
            stored = conn.execute(
                select(SalesStateRow.headline).where(SalesStateRow.lead_id == "lead_abc123")
            ).scalar_one()

    assert stored == headline[:limit]
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "reason=column_width_truncated" in message
        and "table=lead_sales_state" in message
        and "column=headline" in message
        for message in messages
    ), messages
    assert not any(headline[:40] in message for message in messages), (
        "the value must never be logged"
    )


def test_truncation_rewrites_named_and_positional_parameters_identically() -> None:
    """The two parameter shapes must produce the same stored value.

    SQLite is positional (`qmark`) and psycopg is named (`pyformat`), so the branch
    production actually runs is the one no local test exercises end to end. Prove by
    execution that both branches write the same truncated value into the same bind,
    rather than assuming they agree.
    """
    limits = {"headline": ("headline", 5)}
    positional = _guard_row(("original", "lead_1"), ["headline", "lead_id"], limits, "t")
    named = _guard_row({"headline": "original", "lead_id": "lead_1"}, None, limits, "t")
    assert positional == ("origi", "lead_1")
    assert named == {"headline": "origi", "lead_id": "lead_1"}

    # A bind the row does not carry is simply not guarded: it is not being written.
    untouched = {"lead_id": "lead_1"}
    assert _guard_row(untouched, None, limits, "t") is untouched

    # A duplicate bind key is a WHERE comparison. Only the first occurrence -- the
    # VALUES/SET bind -- is rewritten; the comparison keeps what the caller asked.
    assert _guard_row(
        ("original", "original"), ["headline", "headline"], limits, "t"
    ) == ("origi", "original")


def test_parameter_rows_reads_the_row_being_executed_not_the_executemany_flag() -> None:
    """`executemany=True` does not mean `parameters` is a list of rows.

    SQLAlchemy's insertmanyvalues path calls the hook once per row with
    `executemany=True` and `parameters` holding that one flat row. The guard has to
    key off the actual shape; keying off the flag is what made it fail open.
    """
    positiontup = ["lead_id", "headline"]

    # One flat positional row -- the insertmanyvalues and single-INSERT shape.
    flat = ("lead_1", "text")
    assert _parameter_rows(flat, positiontup) == ([flat], True)

    # A true executemany -- a sequence of complete rows.
    many = [("lead_1", "a"), ("lead_2", "b")]
    assert _parameter_rows(many, positiontup) == (many, False)

    # Named dialect, both shapes.
    one = {"lead_id": "lead_1", "headline": "text"}
    assert _parameter_rows(one, None) == ([one], True)
    assert _parameter_rows([one, one], None) == ([one, one], False)

    # A shape the guard does not understand is reported, never guessed at.
    assert _parameter_rows(("only_one",), positiontup) is None
    assert _parameter_rows("not a row", None) is None


def test_truncation_applies_to_each_row_of_an_executemany(engine: Engine) -> None:
    limit = SalesStateRow.__table__.c.headline.type.length
    with engine.begin() as conn:
        conn.execute(
            insert(SalesStateRow),
            [
                {"lead_id": "lead_many1", "headline": "a" * (limit + 30)},
                {"lead_id": "lead_many2", "headline": "b" * limit},
            ],
        )
        stored = dict(
            conn.execute(
                select(SalesStateRow.lead_id, SalesStateRow.headline).where(
                    SalesStateRow.lead_id.in_(["lead_many1", "lead_many2"])
                )
            ).all()
        )
    assert stored == {"lead_many1": "a" * limit, "lead_many2": "b" * limit}


def test_truncation_applies_to_every_row_of_a_server_pk_multirow_insert(
    engine: Engine,
) -> None:
    """The shape the guard used to fail open on: insertmanyvalues.

    `test_truncation_applies_to_each_row_of_an_executemany` uses `SalesStateRow`,
    whose PK is client-supplied, so it takes the real executemany path. A table with
    a server-generated PK -- which is most of them -- takes SQLAlchemy's
    `insertmanyvalues` path instead: the hook fires once PER ROW with
    `executemany=True` while `parameters` is that single flat row. The guard read
    `parameters` as a list of rows, got a list of scalars, and passed the over-long
    value through untruncated *after* logging `column_width_truncated` for it. On
    Postgres that is still a `DataError`, i.e. the whole write lost, while the
    reason-code trail said it had been handled.

    Two rows in one flush is the minimum that reproduces it; one row takes the
    single-INSERT path and was always correct.
    """
    limit = KnowledgeGapRow.__table__.c.topic.type.length
    factory = sessionmaker(engine)
    with factory() as db:
        db.add(KnowledgeGapRow(gap_id="gap_imv1", topic="x" * (limit + 40), question="q"))
        db.add(KnowledgeGapRow(gap_id="gap_imv2", topic="y" * (limit + 40), question="q"))
        db.commit()

    with factory() as db:
        stored = {row.gap_id: row.topic for row in db.query(KnowledgeGapRow).all()}
    assert stored == {"gap_imv1": "x" * limit, "gap_imv2": "y" * limit}


def test_a_raise_on_a_later_row_of_a_multirow_insert_leaves_nothing_behind(
    engine: Engine,
) -> None:
    """A semantic the rewrite moved, pinned so it cannot drift back.

    The old guard re-scanned every row in `context.compiled_parameters` on every
    callback, so a violation anywhere raised on callback 1, before any row reached
    the cursor. The new guard checks the row it is actually handed, so a violation
    on row 2 raises on callback 2 -- after row 1 has already been executed. That is
    only safe because the flush is inside the transaction, so the whole thing rolls
    back. Proved here rather than assumed.
    """
    gap_limit = KnowledgeGapRow.__table__.c.gap_id.type.length
    factory = sessionmaker(engine)
    with factory() as db:
        db.add(KnowledgeGapRow(gap_id="good1", topic="t", question="q"))
        db.add(KnowledgeGapRow(gap_id="z" * (gap_limit + 1), topic="t", question="q"))
        with pytest.raises(ColumnWidthExceeded):
            db.commit()

    with factory() as db:
        assert db.query(KnowledgeGapRow).all() == [], (
            "the row that preceded the rejected one must not survive the raise"
        )


def test_a_truncation_reason_code_is_only_logged_when_the_value_was_truncated(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """The reason-code trail is ground truth, so it must not claim what did not happen.

    The old guard emitted `column_width_truncated` while detecting, then discovered
    it could not rewrite the row and left the value at full length. Every emitted
    truncation reason code must correspond to a value that really was shortened.
    """
    limit = KnowledgeGapRow.__table__.c.topic.type.length
    factory = sessionmaker(engine)
    with caplog.at_level(logging.WARNING, logger="app.db.session"):
        with factory() as db:
            db.add(KnowledgeGapRow(gap_id="gap_log1", topic="x" * (limit + 5), question="q"))
            db.add(KnowledgeGapRow(gap_id="gap_log2", topic="y" * (limit + 5), question="q"))
            db.commit()

    truncated = [r for r in caplog.messages if "reason=column_width_truncated" in r]
    assert len(truncated) == 2, (
        "one reason code per value actually truncated, no more and no fewer"
    )
    assert not [r for r in caplog.messages if "column_width_truncate_deferred" in r]
    assert not [
        r for r in caplog.messages if "column_width_guard_unsupported_shape" in r
    ]

    with factory() as db:
        lengths = sorted(len(row.topic) for row in db.query(KnowledgeGapRow).all())
    assert lengths == [limit, limit]


def test_guard_raise_is_caught_by_the_delivery_worker_and_does_not_wedge_the_queue(
    tmp_path: Path,
) -> None:
    """The second half of the original P0: one poison job must not stall the queue.

    tests/unit/test_crm_runtime_v2.py proves the contract with a synthetic
    `DataError`. This proves it with the *real* guard doing the raising, which is
    the load-bearing claim: `ColumnWidthExceeded` is a `SQLAlchemyError`, so
    `CrmDeliveryWorker._deliver` marks the one job failed and keeps going.
    """
    engine = make_engine(f"sqlite:///{tmp_path / 'guard-wedge.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    now = datetime.now(UTC).isoformat()

    def poison_handler(payload, reconcile_only):
        # A destination handler persisting a value its column cannot hold - the
        # exact shape of the outage, now surfaced by the guard on SQLite too.
        with factory() as db:
            db.add(
                OwnerNotificationRow(
                    kind="website_handoff_delivery",
                    lead_id=f"site:{uuid4().hex}",
                    scheduled_at=now,
                )
            )
            db.commit()
        return "confirmed"

    delivered: list[object] = []

    def healthy_handler(payload, reconcile_only):
        delivered.append(payload)
        return "confirmed"

    handlers = {"poison": poison_handler, "healthy": healthy_handler}

    def dispatch(payload, reconcile_only):
        return handlers[payload["which"]](payload, reconcile_only)

    with factory() as db:
        for which in ("poison", "healthy"):
            db.add(
                CrmOutboxRow(
                    id=uuid4().hex,
                    dedupe_key=f"telegram:{which}",
                    aggregate_type="crm_contact",
                    aggregate_id=uuid4().hex,
                    destination="telegram",
                    payload_json=json.dumps({"which": which, "recipient_id": "999"}),
                    status="pending",
                    created_at=now,
                )
            )
        db.commit()

    worker = CrmDeliveryWorker(
        session_factory=factory,
        sheets=FakeSheetsPort(),
        telegram_handler=dispatch,
        allowed_telegram_recipient_ids=frozenset({"999"}),
    )
    try:
        run = worker.run_once()  # must not raise
        assert run.claimed == 2
        assert run.failed == 1
        assert run.confirmed == 1
        assert delivered, "the healthy job behind the poison one must still be delivered"
        with factory() as db:
            statuses = {row.dedupe_key: row.status for row in db.query(CrmOutboxRow).all()}
        assert statuses == {"telegram:poison": "failed", "telegram:healthy": "confirmed"}
    finally:
        engine.dispose()


def test_app_has_no_raw_text_insert_or_update() -> None:
    """The guard reads compiled statements, so raw `text()` SQL would bypass it.

    True today. This keeps it true, so the coverage the tests above prove stays
    complete rather than becoming a claim about the past.
    """
    offenders: list[str] = []
    allowed_seen: set[tuple[str, str]] = set()
    for path in sorted((_REPO_ROOT / "app").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(_REPO_ROOT).as_posix()
        for match in _RAW_WRITE.finditer(source):
            target = (relative, match.group(3).lower())
            if target in _RAW_WRITE_ALLOWED:
                allowed_seen.add(target)
                continue
            number = source.count("\n", 0, match.start()) + 1
            offenders.append(f"{relative}:{number} -> {match.group(3)}")
    assert offenders == []
    assert allowed_seen == _RAW_WRITE_ALLOWED, (
        "an allowlisted raw write is gone; delete its entry rather than leaving "
        "the allowlist claiming an exception that no longer exists"
    )


@pytest.mark.skipif(
    not os.getenv("MIA_TEST_POSTGRES_URL"), reason="test PostgreSQL DSN not set"
)
def test_guard_truncates_and_raises_against_real_postgres() -> None:
    """The whole point of the guard, proved on the only engine that enforces widths.

    Every other test here runs on SQLite, which ignores `VARCHAR(N)` outright -- the
    exact blindness this chunk exists to remove. Without this test the guard is
    proved only where the bug it prevents cannot occur, so a regression on the
    multi-row INSERT path (which Postgres answers with `DataError` and SQLite
    silently accepts) would pass CI green.

    psycopg is also a *named* (pyformat) dialect while SQLite is positional, so this
    is the only end-to-end exercise of the `positiontup is None` branch.
    """
    url = os.environ["MIA_TEST_POSTGRES_URL"]
    schema = "mia_widths_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = install_column_width_guard(
        create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    )
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine)
        limit = KnowledgeGapRow.__table__.c.topic.type.length

        # Two rows in one flush: the insertmanyvalues shape. Postgres rejects an
        # over-long value outright, so reaching the assert at all proves the guard
        # truncated every row rather than only claiming to.
        with factory() as db:
            db.add(
                KnowledgeGapRow(gap_id="pg1", topic="x" * (limit + 40), question="q")
            )
            db.add(
                KnowledgeGapRow(gap_id="pg2", topic="y" * (limit + 40), question="q")
            )
            db.commit()
        with factory() as db:
            stored = {row.gap_id: row.topic for row in db.query(KnowledgeGapRow).all()}
        assert stored == {"pg1": "x" * limit, "pg2": "y" * limit}

        # And a RAISE column is still stopped before it reaches the server.
        gap_limit = KnowledgeGapRow.__table__.c.gap_id.type.length
        with factory() as db:
            db.add(
                KnowledgeGapRow(gap_id="z" * (gap_limit + 1), topic="t", question="q")
            )
            with pytest.raises(ColumnWidthExceeded):
                db.commit()
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
