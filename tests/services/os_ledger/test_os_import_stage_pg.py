"""The V2-30 importer's staged output against the real V2-14 and V2-14b import tables.

Runs only on the named disposable PostgreSQL 16 (``MIA_OS_LEDGER_TEST_POSTGRES_URL``, server of
database ``mia_ledger_test``), in a database of its own that it creates and drops. It writes with
SQL shaped exactly like ``insert_import_batch``, ``insert_import_rows`` and ``set_import_status``
(erratum E1-rev), so every CHECK, width and unique key of the real schema judges the rows
``prepare`` produces. The ledger-level behaviour (the transaction kind guard, the status machine,
stage, promote) is the conformance suite's and ``test_os_import_stage.py``'s and
``test_os_import_promote.py``'s, on the fake and on the V2-16 ``pg`` ledger case.
"""

import json
import os
from copy import deepcopy
from uuid import uuid4

import pytest
from app.db import os_ledger_migrate as ledger
from app.prototypes import assaf_os_export_provenance as oracle
from app.services.os_ledger.import_workbook import (
    StagedWorkbook,
    import_rows,
    prepare,
    quarantine_reasons,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from tests.fixtures.workbooks.staging import workbook
from tests.fixtures.workbooks.synthetic import synthetic_tables

WORKSPACE = "ws-import"
CLIENT_FIELDS = [field["key"] for field in oracle.NATIVE_FIELDS["clients"]]
CHECK, UNIQUE, TRUNCATION = "23514", "23505", "22001"


@pytest.fixture(scope="module")
def import_db():
    """A throwaway database on the disposable server, so no other ledger fixture is disturbed."""
    url = os.getenv("MIA_OS_LEDGER_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("disposable MIA_OS_LEDGER_TEST_POSTGRES_URL required")
    parsed = make_url(url)
    assert parsed.host in {"127.0.0.1", "localhost", "::1"}
    assert parsed.database == "mia_ledger_test" and parsed.username == "postgres"
    name = "mia_import_stage_" + uuid4().hex[:8]
    admin = create_engine(parsed, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        assert 160000 <= int(c.scalar(text("SHOW server_version_num"))) < 170000
        c.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(parsed.set(database=name))
    try:
        ledger.apply_ledger_migrations(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def db(import_db):
    with import_db.connect() as c, c.begin():
        c.execute(text(
            "INSERT INTO assaf_os.os_workspaces (workspace_id, owner_id, epoch) "
            "VALUES (:w, '123', 1)"), {"w": WORKSPACE})
        yield c
        c.rollback()


def batch_of(tmp_path, tables=None, *, epoch=1, name="synthetic.xlsx") -> StagedWorkbook:
    return prepare(workbook(tmp_path, tables or synthetic_tables(), name),
                   workspace_id=WORKSPACE, authority_epoch=epoch)


def insert_batch(c, batch: StagedWorkbook, *, epoch=1, batch_id=None):
    """What ``insert_import_batch`` writes."""
    c.execute(text(
        "INSERT INTO assaf_os.os_import_batches (workspace_id, batch_id, source_binding, "
        "source_digest, authority_epoch, provenance) "
        "VALUES (:w, :b, :binding, :digest, :epoch, CAST(:provenance AS jsonb))"
    ), {"w": WORKSPACE, "b": batch_id or batch.batch_id, "binding": batch.source_id,
        "digest": batch.source_digest, "epoch": epoch,
        "provenance": json.dumps({"issues": list(batch.issues)})})


def insert_rows(c, batch: StagedWorkbook, batch_id=None, rows=None):
    """What ``insert_import_rows`` writes (insert-only)."""
    for row in rows if rows is not None else import_rows(batch):
        c.execute(text(
            "INSERT INTO assaf_os.os_import_rows (workspace_id, batch_id, row_number, entity, "
            "record_id, source_digest, status, raw_record, canonical_record, provenance, "
            "quarantine) VALUES (:w, :b, :n, :entity, :record, :digest, :status, "
            "CAST(:raw AS jsonb), CAST(:canonical AS jsonb), CAST(:provenance AS jsonb), "
            "CAST(:quarantine AS jsonb))"
        ), {"w": WORKSPACE, "b": batch_id or batch.batch_id, "n": row.row_number,
            "entity": row.entity, "record": row.record_id, "digest": row.source_digest,
            "status": row.status, "raw": json.dumps(row.raw_record, ensure_ascii=False),
            "canonical": None if row.canonical_record is None else json.dumps(
                row.canonical_record, ensure_ascii=False),
            "provenance": json.dumps(row.provenance, ensure_ascii=False),
            "quarantine": None if row.quarantine is None else json.dumps(row.quarantine)})


def set_status(c, batch_id, status, quarantine=None):
    """What ``set_import_status`` writes: ``completed_at`` exactly on the terminal states."""
    terminal = status in {"imported", "quarantined", "failed"}
    c.execute(text(
        "UPDATE assaf_os.os_import_batches SET status = :status, "
        "completed_at = CASE WHEN :terminal THEN now() END, "
        "quarantine = CAST(:quarantine AS jsonb) WHERE workspace_id = :w AND batch_id = :b"
    ), {"status": status, "terminal": terminal, "w": WORKSPACE, "b": batch_id,
        "quarantine": None if quarantine is None else json.dumps(quarantine)})


def refused(c, sqlstate, call):
    with pytest.raises(DBAPIError) as caught:
        with c.begin_nested():  # a failed statement rolls back to the savepoint only
            call()
    assert caught.value.orig.sqlstate == sqlstate


def stored_rows(c, batch_id):
    return c.execute(text(
        "SELECT row_number, entity, record_id, source_digest, status, raw_record, "
        "canonical_record, provenance, quarantine FROM assaf_os.os_import_rows "
        "WHERE workspace_id = :w AND batch_id = :b ORDER BY row_number"
    ), {"w": WORKSPACE, "b": batch_id}).all()


def test_a_clean_batch_fits_the_import_tables_and_round_trips(db, tmp_path):
    batch = batch_of(tmp_path)
    insert_batch(db, batch)
    insert_rows(db, batch)
    rows = stored_rows(db, batch.batch_id)
    expected = import_rows(batch)
    assert [r.row_number for r in rows] == [1, 2, 3, 4, 5, 6]
    for stored, row in zip(rows, expected, strict=True):
        assert (stored.entity, stored.record_id, stored.source_digest, stored.status) == (
            row.entity, row.record_id, row.source_digest, row.status)
        # JSONB hands back exactly what the importer produced, Hebrew and all.
        assert stored.raw_record == json.loads(json.dumps(row.raw_record))
        assert stored.canonical_record == json.loads(json.dumps(row.canonical_record))
        assert stored.provenance == json.loads(json.dumps(row.provenance))
        assert stored.quarantine is None
    # staged -> validated keeps completed_at null; validated -> imported sets it.
    set_status(db, batch.batch_id, "validated")
    set_status(db, batch.batch_id, "imported")
    assert db.execute(text(
        "SELECT status, completed_at IS NOT NULL FROM assaf_os.os_import_batches"
    )).one() == ("imported", True)


def test_a_status_without_its_completed_at_is_refused_by_the_schema(db, tmp_path):
    batch = batch_of(tmp_path)
    insert_batch(db, batch)
    refused(db, CHECK, lambda: db.execute(text(
        "UPDATE assaf_os.os_import_batches SET status = 'imported'")))
    refused(db, CHECK, lambda: db.execute(text(
        "UPDATE assaf_os.os_import_batches SET status = 'quarantined', completed_at = now()")))


def test_a_quarantined_batch_keeps_every_row_and_its_quarantine_objects(db, tmp_path):
    tables = synthetic_tables()
    tables["clients"][1][CLIENT_FIELDS.index("name")] = "Unaccepted edit"
    extra = deepcopy(tables["clients"][1])
    extra[CLIENT_FIELDS.index("person_id")] = ""
    tables["clients"].append(extra)
    batch = batch_of(tmp_path, tables)
    insert_batch(db, batch)
    insert_rows(db, batch)
    rows = stored_rows(db, batch.batch_id)
    flagged = [r for r in rows if r.status == "quarantined"]
    assert len(rows) == 7 and len(flagged) == 2
    assert all(r.quarantine["reasons"] for r in flagged)
    assert [r.record_id for r in rows if r.record_id is None] == [None]
    set_status(db, batch.batch_id, "quarantined", {
        "rows": len(flagged), "reasons": quarantine_reasons(batch), "issues": list(batch.issues)})
    stored = db.execute(text(
        "SELECT status, quarantine, completed_at IS NOT NULL FROM assaf_os.os_import_batches"
    )).one()
    assert stored[0] == "quarantined" and stored[2] is True
    assert stored[1]["rows"] == 2 and stored[1]["reasons"]["unaccepted_record"] >= 1


def test_a_structurally_broken_workbook_settles_as_failed(db, tmp_path):
    tables = synthetic_tables()
    del tables["tasks"]
    batch = batch_of(tmp_path, tables)
    assert batch.issues == ("entity_sheet_missing",)
    insert_batch(db, batch)
    insert_rows(db, batch)
    set_status(db, batch.batch_id, "failed")
    assert db.execute(text(
        "SELECT status, provenance -> 'issues', completed_at IS NOT NULL "
        "FROM assaf_os.os_import_batches")).one() == ("failed", ["entity_sheet_missing"], True)


def test_an_id_that_is_not_well_formed_never_reaches_the_record_id_column(db, tmp_path):
    tables = synthetic_tables()
    extra = deepcopy(tables["clients"][1])
    extra[CLIENT_FIELDS.index("person_id")] = "PER_" + "X" * 80
    tables["clients"].append(extra)
    batch = batch_of(tmp_path, tables)
    insert_batch(db, batch)
    insert_rows(db, batch)  # accepted: the over-long id is kept only in raw_record
    stored = [r for r in stored_rows(db, batch.batch_id) if r.record_id is None]
    assert len(stored) == 1 and stored[0].status == "quarantined"
    assert stored[0].raw_record["fields"]["person_id"] == "PER_" + "X" * 80
    # The column itself is VARCHAR(64): carrying the raw id would have failed the whole stage.
    bad = deepcopy(import_rows(batch)[1])
    refused(db, TRUNCATION, lambda: insert_rows(db, batch, rows=[
        type(bad)(**{**bad.__dict__, "row_number": 99, "record_id": "PER_" + "X" * 80,
                     "source_digest": "f" * 64})]))


def test_a_source_digest_is_unique_per_epoch_and_a_new_epoch_is_a_new_batch(db, tmp_path):
    first = batch_of(tmp_path, epoch=1)
    insert_batch(db, first, epoch=1)
    refused(db, UNIQUE, lambda: insert_batch(db, first, epoch=1, batch_id=uuid4()))
    second = batch_of(tmp_path, epoch=2)
    assert second.batch_id != first.batch_id and second.source_digest == first.source_digest
    insert_batch(db, second, epoch=2)
    assert db.scalar(text("SELECT count(*) FROM assaf_os.os_import_batches")) == 2


def test_rows_are_unique_by_number_and_by_source_digest_within_a_batch(db, tmp_path):
    batch = batch_of(tmp_path)
    insert_batch(db, batch)
    insert_rows(db, batch)
    rows = import_rows(batch)
    refused(db, UNIQUE, lambda: insert_rows(db, batch, rows=[rows[0]]))
    clone = type(rows[0])(**{**rows[0].__dict__, "row_number": 50})
    refused(db, UNIQUE, lambda: insert_rows(db, batch, rows=[clone]))
    assert len({r.source_digest for r in rows}) == len(rows) == 6
