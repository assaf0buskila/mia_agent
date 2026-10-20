"""Pure staging of synthetic physical workbooks; the prototype stays the normalization oracle."""

import hashlib
import json
from copy import deepcopy

import pytest
from app.prototypes import assaf_os_export_provenance as oracle
from app.services.os_ledger import api
from app.services.os_ledger.import_workbook import (
    WorkbookImportError,
    import_rows,
    manifest,
    manifest_digest,
    prepare,
    quarantine_count,
    quarantine_reasons,
)
from app.services.os_ledger.xlsx import Cell, read_xlsx

from tests.fixtures.workbooks.synthetic import (
    JOURNAL,
    synthetic_records,
    synthetic_tables,
    tables_from_records,
    task_record,
    write_book,
)

CLIENT_FIELDS = [field["key"] for field in oracle.NATIVE_FIELDS["clients"]]


def prepared(tmp_path, tables=None, epoch=1, name="synthetic-stage.xlsx"):
    path = write_book(tmp_path / name, tables or synthetic_tables())
    return prepare(read_xlsx(path), workspace_id="synthetic-workspace", authority_epoch=epoch)


def test_os_import_prepare_manifest_and_binding(tmp_path):
    batch = prepared(tmp_path)
    result = manifest(batch)
    assert result["quarantine_count"] == 0
    assert result["counts"]["payments"] == 3
    assert result["money_by_currency"]["ILS"]["net_payments_minor"] == 7500
    assert result["money_by_currency"]["USD"]["net_payments_minor"] == 10000
    assert result["refunds"][0]["original_payment_id"] == "PAY_ILS"
    assert result["references"]["unresolved"] == 0
    assert all(row["recorded_minutes"] is None and row["hourly_minor"] is None
               for row in result["time_by_work"])
    assert result["cancellations"] == []
    assert result["input_digests"]["clients"]["PER_ONE"]
    assert result["revisions"]["clients"]["PER_ONE"] == 1
    assert manifest_digest(batch) == api.digest(result)
    assert manifest(batch) == manifest(batch)
    assert manifest(prepared(tmp_path)) == result
    expected = hashlib.sha256((tmp_path / "synthetic-stage.xlsx").read_bytes()).hexdigest()
    assert batch.source_digest == expected
    assert batch.source_id == "owner_export:" + expected
    assert batch.source_binding["source_digest"] == expected


def test_os_import_prepare_reads_the_hidden_journal_sheet(tmp_path):
    book = read_xlsx(write_book(tmp_path / "synthetic.xlsx", synthetic_tables()))
    hidden = {sheet.name: sheet.state for sheet in book.sheets}
    assert hidden[JOURNAL] == "hidden" and hidden["clients"] == "visible"
    # Without that hidden tab nothing is accepted: every row is quarantined, none dropped.
    tables = synthetic_tables()
    del tables[JOURNAL]
    batch = prepared(tmp_path, tables, name="no-journal-tab.xlsx")
    assert "accepted_journal_missing" in batch.issues
    assert {row.status for row in batch.rows} == {"quarantined"} and len(batch.rows) == 6


def test_os_import_prepare_manifest_revisions_are_the_workbook_revisions(tmp_path):
    batch = prepared(tmp_path, synthetic_tables({"PER_ONE": 7, "PAY_USD": 3}))
    result = manifest(batch)
    assert result["quarantine_count"] == 0
    assert result["revisions"]["clients"] == {"PER_ONE": 7}
    assert result["revisions"]["payments"]["PAY_USD"] == 3
    assert result["revisions"]["payments"]["PAY_ILS"] == 1


@pytest.mark.parametrize("change", ["edited", "no_journal", "idless", "duplicate", "bad_id"])
def test_os_import_prepare_never_drops_unaccepted_rows(tmp_path, change):
    tables = synthetic_tables()
    if change == "edited":
        tables["clients"][1][CLIENT_FIELDS.index("name")] = "Unaccepted edit"
    elif change == "no_journal":
        tables[JOURNAL] = tables[JOURNAL][:1]
    elif change in {"idless", "bad_id"}:
        extra = deepcopy(tables["clients"][1])
        extra[CLIENT_FIELDS.index("person_id")] = "" if change == "idless" else "PER_" + "X" * 80
        tables["clients"].append(extra)
    else:
        tables["clients"].append(deepcopy(tables["clients"][1]))
    batch = prepared(tmp_path, tables)
    assert len(batch.rows) == (7 if change in {"idless", "bad_id", "duplicate"} else 6)
    assert any(row.status == "quarantined" for row in batch.rows)
    assert manifest(batch)["quarantine_count"] > 0
    if change in {"idless", "bad_id"}:
        # An id that is missing or not well formed is never carried: record_id stays None.
        idless = [row for row in batch.rows if row.record_id is None]
        assert len(idless) == 1 and idless[0].raw_record
        assert "missing_or_invalid_record_id" in idless[0].quarantine
    if change == "edited":
        assert "accepted_input_changed" in batch.rows[0].quarantine
    # Every row, quarantined or not, becomes an insertable os_import_rows record.
    rows = import_rows(batch)
    assert len(rows) == len(batch.rows)
    assert all((row.status == "quarantined") == (row.quarantine is not None) for row in rows)
    assert all(row.record_id is None or len(row.record_id) <= 64 for row in rows)


@pytest.mark.parametrize("column_index,value", [
    (2, 2), (3, "0" * 64), (4, "0" * 64), (5, "{}"), (6, "invalid"), (7, ""),
])
def test_os_import_prepare_journal_tampering_quarantines(tmp_path, column_index, value):
    tables = synthetic_tables()
    tables[JOURNAL][1][column_index] = value
    assert manifest(prepared(tmp_path, tables))["quarantine_count"] > 0


def test_os_import_prepare_a_date_serial_is_quarantined_not_guessed(tmp_path):
    book = read_xlsx(write_book(tmp_path / "synthetic.xlsx", synthetic_tables()))
    clients = next(sheet for sheet in book.sheets if sheet.name == "clients")
    clients.cells["B2"] = Cell("45000", "number", True)
    batch = prepare(book, workspace_id="synthetic-workspace", authority_epoch=1)
    assert "date_serial_binding_unspecified" in batch.rows[0].quarantine
    assert quarantine_reasons(batch)["date_serial_binding_unspecified"] == 1


def test_os_import_prepare_signed_cancellations_and_epochs(tmp_path):
    batch = prepared(tmp_path)
    cancellations = [{"entity": "tasks", "record_id": "TAS_OLD"}]
    assert manifest(batch, cancellations=cancellations)["cancellations"] == cancellations
    assert manifest_digest(batch, cancellations=cancellations) != manifest_digest(batch)
    assert prepared(tmp_path, epoch=2).batch_id != batch.batch_id
    assert manifest_digest(prepared(tmp_path, epoch=2)) != manifest_digest(batch)


@pytest.mark.parametrize("bad,code", [
    ([{"entity": "tasks"}], "invalid_cancellation"),
    ([{"entity": "nope", "record_id": "TAS_OLD"}], "invalid_cancellation"),
    ([{"entity": "tasks", "record_id": "x"}], "invalid_cancellation"),
    ([{"entity": "tasks", "record_id": "TAS_OLD"}] * 2, "duplicate_cancellation"),
    ([{"entity": "clients", "record_id": "PER_ONE"}], "cancellation_conflicts_with_record"),
])
def test_os_import_prepare_refuses_a_bad_cancellation_list(tmp_path, bad, code):
    with pytest.raises(WorkbookImportError, match=f"^{code}$"):
        manifest(prepared(tmp_path), cancellations=bad)


def test_os_import_prepare_binding_refuses_invalid_workspace_or_epoch(tmp_path):
    book = read_xlsx(write_book(tmp_path / "synthetic.xlsx", synthetic_tables()))
    for workspace, epoch in [("", 1), ("x" * 129, 1), ("ws", 0), ("ws", True)]:
        with pytest.raises(WorkbookImportError, match="^invalid_local_binding$"):
            prepare(book, workspace_id=workspace, authority_epoch=epoch)


def test_os_import_prepare_state_with_tasks_and_a_dangling_reference(tmp_path):
    records = synthetic_records()
    records["tasks"] = [task_record("TAS_ONE"), task_record("TAS_TWO")]
    tables = tables_from_records(records)
    fields = [field["key"] for field in oracle.NATIVE_FIELDS["tasks"]]
    tables["tasks"][2][fields.index("person_id")] = "PER_MISSING"
    batch = prepared(tmp_path, tables)
    by_id = {row.record_id: row for row in batch.rows if row.entity == "tasks"}
    assert by_id["TAS_ONE"].status == "staged"
    # A reference to an id that is not in the workbook never resolves; the row is quarantined.
    assert by_id["TAS_TWO"].status == "quarantined"
    assert "record_invalid" in by_id["TAS_TWO"].quarantine
    assert quarantine_count(batch) == 1


def test_os_import_prepare_cli_counts_and_digest_only(tmp_path, capsys):
    from scripts.os_import import main

    path = write_book(tmp_path / "synthetic.xlsx", synthetic_tables())
    assert main([str(path), "--workspace", "synthetic-workspace", "--epoch", "1"]) == 0
    output = capsys.readouterr()
    digest = manifest_digest(prepared(tmp_path, name="again.xlsx"))
    # The same bytes under another name are the same batch: the digest is of the content.
    assert output.out == (
        "os_import op=stage outcome=ok reason=dry_run rows=6 quarantined=0\n"
        f"os_import op=manifest outcome=ok reason=prepared rows=6 quarantined=0 digest={digest}\n"
    )
    assert output.err == ""


def test_os_import_prepare_cli_quarantine_lists_reason_counts_never_values(tmp_path, capsys):
    from scripts.os_import import main

    tables = synthetic_tables()
    tables["clients"][1][CLIENT_FIELDS.index("name")] = "Unaccepted edit"
    path = write_book(tmp_path / "synthetic.xlsx", tables)
    assert main([str(path), "--workspace", "synthetic-workspace", "--epoch", "1"]) == 2
    output = capsys.readouterr()
    assert output.err == ""
    # One quarantined row, plus the batch issue its absence causes (works lose their client).
    first = "os_import op=stage outcome=quarantined reason=dry_run rows=6 quarantined=2\n"
    assert output.out.startswith(first)
    assert "reason=accepted_input_changed rows=1\n" in output.out
    assert "reason=state_validation_failed rows=1\n" in output.out
    for leaked in ("Unaccepted edit", "Synthetic only", "PER_ONE", str(tmp_path)):
        assert leaked not in output.out


def test_os_import_prepare_cli_writes_the_manifest_and_reads_cancellations(tmp_path, capsys):
    from scripts.os_import import main

    path = write_book(tmp_path / "synthetic.xlsx", synthetic_tables())
    cancel = tmp_path / "cancel.json"
    cancel.write_text(json.dumps([{"entity": "tasks", "record_id": "TAS_OLD"}]), encoding="utf-8")
    out = tmp_path / "manifest.json"
    args = [str(path), "--workspace", "synthetic-workspace", "--epoch", "1",
            "--cancellations", str(cancel), "--manifest-out", str(out)]
    assert main(args) == 0
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["cancellations"] == [{"entity": "tasks", "record_id": "TAS_OLD"}]
    assert f"digest={api.digest(document)}\n" in capsys.readouterr().out
    # An existing review artifact is never overwritten.
    assert main(args) == 1
    assert capsys.readouterr().err == (
        "os_import op=stage outcome=refused reason=manifest_output_unwritable\n")


@pytest.mark.parametrize("argv,reason", [
    (["private.xlsx", "--apply"], "invalid_arguments"),
    (["private.xlsx"], "invalid_arguments"),
    (["private.xlsx", "--workspace", "ws", "--epoch", "one"], "invalid_arguments"),
    (["private.xlsx", "--workspace", "ws", "--epoch", "1"], "invalid_xlsx"),
])
def test_os_import_prepare_cli_refuses_and_sanitizes_errors(capsys, argv, reason):
    from scripts.os_import import main

    assert main(argv) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"os_import op=stage outcome=refused reason={reason}\n"
