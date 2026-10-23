"""Staging a workbook through the frozen ledger API (erratum E1-rev), on the fake and, when
``MIA_OS_LEDGER_TEST_POSTGRES_URL`` is set, on the V2-16 PostgreSQL ledger (the ``pg`` case of
the ``ledger`` fixture). Nothing here reads a real workbook."""

import hashlib
from copy import deepcopy

import pytest
from app.prototypes import assaf_os_export_provenance as oracle
from app.services.os_ledger import api, import_workbook
from app.services.os_ledger.import_workbook import manifest_digest, prepare, stage

from tests.fakes.contexts import make_ctx
from tests.fakes.os_ledger_builders import fresh_workspace, invariant_violation
from tests.fixtures.workbooks.staging import (
    IMPORT_WS,
    next_epoch,
    snapshot,
    workbook,
    worker,
)
from tests.fixtures.workbooks.synthetic import synthetic_tables

CLIENT_FIELDS = [field["key"] for field in oracle.NATIVE_FIELDS["clients"]]


@pytest.fixture
def shadow(ledger):
    fresh_workspace(ledger, IMPORT_WS)
    return ledger


def batches(env):
    return env.rows("os_import_batches")


def test_stage_persists_the_batch_and_every_row_and_validates_a_clean_one(shadow, tmp_path):
    book = workbook(tmp_path, synthetic_tables())
    result = stage(shadow, worker(), book)
    assert result.status == "validated" and (result.rows, result.quarantined) == (6, 0)
    (stored,) = batches(shadow)
    expected = hashlib.sha256((tmp_path / "synthetic.xlsx").read_bytes()).hexdigest()
    assert stored["batch_id"] == result.batch.batch_id and stored["status"] == "validated"
    assert (stored["authority_epoch"], stored["source_digest"]) == (1, expected)
    assert stored["source_binding"] == "owner_export:" + expected
    assert stored["completed_at"] is None and stored["quarantine"] is None
    assert stored["provenance"]["native_acceptance_is_human_approval"] is False
    assert stored["provenance"]["rows"] == 6 and stored["provenance"]["issues"] == []
    rows = sorted(shadow.rows("os_import_rows"), key=lambda r: r["row_number"])
    assert [r["row_number"] for r in rows] == [1, 2, 3, 4, 5, 6]
    assert {r["status"] for r in rows} == {"staged"}
    assert all(r["canonical_record"] and r["raw_record"]["fields"] for r in rows)
    assert result.manifest_digest == manifest_digest(result.batch)
    # Staging writes import tables only: no record, proposal, receipt, event or outbox row.
    for table in ("os_clients", "os_works", "os_payments", "os_proposals", "os_receipts",
                  "os_events", "os_outbox"):
        assert shadow.rows(table) == []


def test_stage_quarantines_an_unaccepted_edit_and_keeps_every_row(shadow, tmp_path):
    tables = synthetic_tables()
    tables["clients"][1][CLIENT_FIELDS.index("name")] = "Unaccepted edit"
    result = stage(shadow, worker(), workbook(tmp_path, tables))
    assert result.status == "quarantined" and (result.rows, result.quarantined) == (6, 1)
    assert result.reasons["accepted_input_changed"] == 1
    (stored,) = batches(shadow)
    assert stored["status"] == "quarantined" and stored["completed_at"] is not None
    assert stored["quarantine"]["rows"] == 1
    assert stored["quarantine"]["reasons"]["accepted_input_changed"] == 1
    assert stored["quarantine"]["issues"] == ["state_validation_failed"]
    rows = shadow.rows("os_import_rows")
    assert len(rows) == 6
    flagged = [r for r in rows if r["status"] == "quarantined"]
    assert len(flagged) == 1 and flagged[0]["quarantine"]["reasons"]
    assert flagged[0]["record_id"] == "PER_ONE"


def test_stage_keeps_an_id_less_row_with_no_record_id(shadow, tmp_path):
    tables = synthetic_tables()
    extra = deepcopy(tables["clients"][1])
    extra[CLIENT_FIELDS.index("person_id")] = ""
    tables["clients"].append(extra)
    result = stage(shadow, worker(), workbook(tmp_path, tables))
    assert result.status == "quarantined" and result.rows == 7
    idless = [r for r in shadow.rows("os_import_rows") if r["record_id"] is None]
    assert len(idless) == 1 and idless[0]["status"] == "quarantined"
    assert idless[0]["canonical_record"] is None and idless[0]["raw_record"]["fields"]


def test_stage_marks_a_structurally_broken_workbook_failed(shadow, tmp_path):
    tables = synthetic_tables()
    del tables["tasks"]
    result = stage(shadow, worker(), workbook(tmp_path, tables))
    assert result.status == "failed" and result.quarantined == 0
    assert result.reasons == {"entity_sheet_missing": 1}
    (stored,) = batches(shadow)
    assert stored["status"] == "failed" and stored["completed_at"] is not None
    assert stored["provenance"]["issues"] == ["entity_sheet_missing"]
    assert {r["status"] for r in shadow.rows("os_import_rows")} == {"staged"}


def test_stage_writes_rows_in_chunks_without_losing_one(shadow, tmp_path, monkeypatch):
    monkeypatch.setattr(import_workbook, "_ROW_CHUNK", 2)
    stage(shadow, worker(), workbook(tmp_path, synthetic_tables()))
    assert sorted(r["row_number"] for r in shadow.rows("os_import_rows")) == [1, 2, 3, 4, 5, 6]


def test_the_same_workbook_cannot_be_staged_twice_in_one_epoch(shadow, tmp_path):
    book = workbook(tmp_path, synthetic_tables())
    stage(shadow, worker(), book)
    before = snapshot(shadow)
    with invariant_violation(shadow, "import_batch_pk"):
        stage(shadow, worker(), book)
    assert snapshot(shadow) == before


def test_the_same_workbook_at_the_next_epoch_is_a_new_batch(shadow, tmp_path):
    book = workbook(tmp_path, synthetic_tables())
    first = stage(shadow, worker(), book)
    next_epoch(shadow, epoch=1)
    second = stage(shadow, worker(2), book)
    assert first.batch.batch_id != second.batch.batch_id
    assert first.manifest_digest != second.manifest_digest
    assert sorted(b["authority_epoch"] for b in batches(shadow)) == [1, 2]
    assert {b["source_digest"] for b in batches(shadow)} == {book.source_digest}


def test_stage_derives_the_binding_from_the_file_and_refuses_a_different_one(shadow, tmp_path):
    book = workbook(tmp_path, synthetic_tables())
    before = snapshot(shadow)
    with pytest.raises(api.LedgerError) as caught:
        stage(shadow, worker(), book, "owner_export:" + "0" * 64)
    assert caught.value.code == "validation_error"
    assert caught.value.details == {"reason": "source_binding_mismatch"}
    assert snapshot(shadow) == before
    assert stage(shadow, worker(), book, "owner_export:" + book.source_digest).status == "validated"


def test_stage_is_a_worker_job_only(shadow, tmp_path):
    book = workbook(tmp_path, synthetic_tables())
    for channel in ("OS", "MIA", "TELEGRAM_APPROVAL"):
        with pytest.raises(api.LedgerError) as caught:
            stage(shadow, make_ctx(channel, workspace=IMPORT_WS), book)
        assert caught.value.code == "channel_operation_denied"
    assert batches(shadow) == []


def test_stage_needs_core_to_be_shadow(ledger, tmp_path):
    fresh_workspace(ledger, IMPORT_WS, core="active")
    with pytest.raises(api.LedgerError, match="^authority_not_active$"):
        stage(ledger, worker(), workbook(tmp_path, synthetic_tables()))
    assert batches(ledger) == []


def test_prepare_and_persist_agree_on_the_manifest(shadow, tmp_path):
    book = workbook(tmp_path, synthetic_tables())
    batch = prepare(book, workspace_id=IMPORT_WS, authority_epoch=1)
    assert stage(shadow, worker(), book).manifest_digest == manifest_digest(batch)


def test_persisting_a_batch_prepared_for_another_workspace_or_epoch_is_refused(shadow, tmp_path):
    book = workbook(tmp_path, synthetic_tables())
    for workspace, epoch in (("ws-other", 1), (IMPORT_WS, 2)):
        batch = prepare(book, workspace_id=workspace, authority_epoch=epoch)
        with pytest.raises(api.LedgerError, match="^authority_changed$"):
            import_workbook.persist_staged_batch(shadow, worker(), batch)
    assert batches(shadow) == []


def test_stage_logs_codes_and_counts_only(shadow, tmp_path, caplog):
    caplog.set_level("INFO", logger="mia.os_import")
    tables = synthetic_tables()
    tables["clients"][1][CLIENT_FIELDS.index("name")] = "Unaccepted edit"
    stage(shadow, worker(), workbook(tmp_path, tables))
    text = caplog.text
    assert "os_import op=stage outcome=quarantined reason=persisted rows=6 quarantined=1" in text
    for leaked in ("Unaccepted edit", "Synthetic only", "PER_ONE", str(tmp_path)):
        assert leaked not in text
