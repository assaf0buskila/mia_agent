"""Pure staging of synthetic physical workbooks; the prototype stays the normalization oracle."""

import decimal
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
    staged_records,
)
from app.services.os_ledger.xlsx import Cell, read_xlsx

from tests.fixtures.workbooks.synthetic import (
    JOURNAL,
    STYLE_DATE,
    Typed,
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
        "os_import op=stage outcome=ok reason=dry_run rows=6 quarantined=0 issues=0\n"
        "os_import op=manifest outcome=ok reason=prepared rows=6 quarantined=0 issues=0 "
        f"digest={digest}\n"
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
    # One quarantined row, plus the batch issue its absence causes (works lose their client):
    # the two are counted apart, never summed.
    first = ("os_import op=stage outcome=quarantined reason=dry_run rows=6 quarantined=1 "
             "issues=1\n")
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


# ---- V2-30b: numeric overflow, trailing blank columns, the CLI's two counts ----------------

PAYMENT_FIELDS = [field["key"] for field in oracle.NATIVE_FIELDS["payments"]]
AMOUNT = PAYMENT_FIELDS.index("actual_amount_ils")


def overflowing(text):
    tables = synthetic_tables()
    tables["payments"][1][AMOUNT] = Typed(text)  # PAY_ILS, as a raw numeric cell
    return tables


@pytest.mark.parametrize("text", [
    "1E+999999999", "-1E+999999999", "1E+99999999999999999999", "1E-999999999",
    "1E-99999999999999999999",
])
def test_os_import_prepare_an_extreme_exponent_is_unsafe_numeric_value_not_a_crash(
    tmp_path, text
):
    # abs(Decimal("1E+999999999")) raises decimal.Overflow and the constructor raises
    # decimal.InvalidOperation for a larger exponent; neither may escape the importer.
    batch = prepared(tmp_path, overflowing(text))
    paid = next(row for row in batch.rows if row.record_id == "PAY_ILS")
    assert paid.status == "quarantined" and "unsafe_numeric_value" in paid.quarantine
    assert quarantine_reasons(batch)["unsafe_numeric_value"] == 1
    assert quarantine_count(batch) > 0


def test_os_import_prepare_an_extreme_exponent_in_the_journal_is_an_issue_not_a_crash(tmp_path):
    tables = synthetic_tables()
    tables[JOURNAL][1][2] = Typed("1E+999999999")  # the revision column
    batch = prepared(tmp_path, tables)
    assert "invalid_journal_acceptance" in batch.issues
    assert quarantine_count(batch) > 0


def test_os_import_prepare_cli_prints_the_overflow_as_a_quarantine_reason_never_a_value(
    tmp_path, capsys
):
    from scripts.os_import import main

    path = write_book(tmp_path / "overflow.xlsx", overflowing("1E+999999999"))
    assert main([str(path), "--workspace", "synthetic-workspace", "--epoch", "1"]) == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert "reason=unsafe_numeric_value rows=1\n" in output.out
    assert "999999999" not in output.out


def test_os_import_prepare_cli_refuses_any_decimal_error_that_escapes(
    tmp_path, capsys, monkeypatch
):
    import scripts.os_import as cli

    def overflow(*args, **kwargs):
        raise decimal.Overflow()

    monkeypatch.setattr(cli, "prepare", overflow)
    path = write_book(tmp_path / "synthetic.xlsx", synthetic_tables())
    assert cli.main([str(path), "--workspace", "synthetic-workspace", "--epoch", "1"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "os_import op=stage outcome=refused reason=unsafe_numeric_value\n"


def with_blank_trailing_columns(tables):
    """Blank cells right of every row of every tab: an empty string, then a formatted blank."""
    return {name: [[*row, "", Typed(None, style=STYLE_DATE)] for row in table]
            for name, table in tables.items()}


def test_os_import_prepare_blank_trailing_columns_map_like_none(tmp_path):
    clean = prepared(tmp_path, name="clean.xlsx")
    wide = prepared(tmp_path, with_blank_trailing_columns(synthetic_tables()), name="wide.xlsx")
    assert wide.issues == () and quarantine_count(wide) == 0
    assert [(r.entity, r.record_id, r.status, r.quarantine) for r in wide.rows] == [
        (r.entity, r.record_id, r.status, r.quarantine) for r in clean.rows]
    assert staged_records(wide) == staged_records(clean)
    assert manifest(wide)["input_digests"] == manifest(clean)["input_digests"]
    assert manifest(wide)["counts"] == manifest(clean)["counts"] and len(clean.rows) == 6


@pytest.mark.parametrize("tab", ["clients", JOURNAL])
def test_os_import_prepare_blank_trailing_columns_on_one_tab_map_like_none(tmp_path, tab):
    tables = synthetic_tables()
    tables[tab] = with_blank_trailing_columns({tab: tables[tab]})[tab]
    batch = prepared(tmp_path, tables)
    assert batch.issues == () and quarantine_count(batch) == 0


def test_os_import_prepare_a_named_extra_header_column_still_does_not_match(tmp_path):
    tables = synthetic_tables()
    tables["clients"][0] = [*tables["clients"][0], "unknown column"]
    batch = prepared(tmp_path, tables)
    assert "entity_sheet_missing" in batch.issues and "unsupported_sheet_headers" in batch.issues


def test_os_import_prepare_a_value_under_a_blank_header_is_still_unexpected(tmp_path):
    tables = with_blank_trailing_columns(synthetic_tables())
    tables["clients"][1][-2] = "stray"  # under the blank header cell
    batch = prepared(tmp_path, tables)
    assert batch.rows[0].entity == "clients"
    assert "unexpected_row_columns" in batch.rows[0].quarantine
    assert [row.status for row in batch.rows[1:]] == ["staged"] * 5


def test_os_import_prepare_cli_counts_rows_and_batch_issues_apart(tmp_path, capsys):
    from scripts.os_import import main

    # A batch issue only: a nonempty tab with unknown headers quarantines no row.
    tables = synthetic_tables()
    tables["stray"] = [["unknown header"], ["value"]]
    path = write_book(tmp_path / "issue.xlsx", tables)
    assert main([str(path), "--workspace", "synthetic-workspace", "--epoch", "1"]) == 2
    out = capsys.readouterr().out
    assert out.startswith(
        "os_import op=stage outcome=quarantined reason=dry_run rows=6 quarantined=0 issues=1\n")
    assert "op=manifest outcome=quarantined reason=prepared rows=6 quarantined=0 issues=1 " in out
    # A quarantined row only: an edited leaf payment, with no batch issue.
    tables = synthetic_tables()
    tables["payments"][2][PAYMENT_FIELDS.index("notes")] = "Unaccepted edit"
    path = write_book(tmp_path / "row.xlsx", tables)
    assert main([str(path), "--workspace", "synthetic-workspace", "--epoch", "1"]) == 2
    out = capsys.readouterr().out
    assert out.startswith(
        "os_import op=stage outcome=quarantined reason=dry_run rows=6 quarantined=1 issues=0\n")
    assert "op=manifest outcome=quarantined reason=prepared rows=6 quarantined=1 issues=0 " in out
    assert "Unaccepted edit" not in out
