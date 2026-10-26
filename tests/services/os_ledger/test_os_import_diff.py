"""Local diff output discloses coordinates only."""

import pytest

from tests.fixtures.workbooks.synthetic import NS, REL, make_xlsx, write_book


def test_os_import_diff_one_cell_without_values(tmp_path, capsys):
    from scripts.os_workbook_diff import main

    left = make_xlsx(tmp_path / "left.xlsx", '<c r="A1"><v>12.30</v></c>')
    right = make_xlsx(tmp_path / "right.xlsx", '<c r="A1"><v>12.31</v></c>')
    assert main([str(left), str(right)]) == 0
    output = capsys.readouterr()
    assert output.out == (
        'os_import op=diff outcome=ok reason=compared changed_cells=1\n'
        '("journal", "A1")\n'
    )
    assert output.err == ""


def test_os_import_diff_formula_and_deleted_cells(tmp_path):
    from app.services.os_ledger.xlsx import read_xlsx
    from scripts.os_workbook_diff import changed_cells

    left = make_xlsx(tmp_path / "left.xlsx", (
        '<c r="A1"><f>1+1</f><v>2</v></c><c r="B1"><v>1</v></c>'
    ))
    right = make_xlsx(tmp_path / "right.xlsx", '<c r="A1"><f>2</f><v>2</v></c>')
    assert changed_cells(read_xlsx(left), read_xlsx(right)) == [
        ("journal", "A1"), ("journal", "B1"),
    ]


def test_os_import_diff_failure_is_sanitized(capsys):
    from scripts.os_workbook_diff import main

    assert main(["private-file-does-not-exist.xlsx", "another-private-file.xlsx"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == 'os_import op=diff outcome=refused reason=invalid_xlsx\n'


@pytest.mark.parametrize("reverse", [False, True])
def test_os_import_diff_date_system(tmp_path, reverse):
    from app.services.os_ledger.xlsx import read_xlsx
    from scripts.os_workbook_diff import changed_cells

    tables = {"journal": [[45678]]}
    left = write_book(tmp_path / "left.xlsx", tables, date_1904=reverse)
    right = write_book(tmp_path / "right.xlsx", tables, date_1904=not reverse)
    assert changed_cells(read_xlsx(left), read_xlsx(right)) == [("<workbook>", "date1904")]


@pytest.mark.parametrize("state", ["hidden", "veryHidden"])
@pytest.mark.parametrize("reverse", [False, True])
def test_os_import_diff_sheet_state(tmp_path, state, reverse):
    from app.services.os_ledger.xlsx import read_xlsx
    from scripts.os_workbook_diff import changed_cells

    def workbook(state):
        return {"xl/workbook.xml": (
            f'<workbook xmlns="{NS}" xmlns:r="{REL}"><sheets>'
            f'<sheet name="journal" sheetId="1" state="{state}" r:id="rId1"/>'
            '</sheets></workbook>'
        )}

    left = make_xlsx(tmp_path / "left.xlsx", extra=workbook("visible"))
    right = make_xlsx(tmp_path / "right.xlsx", extra=workbook(state))
    before, after = (right, left) if reverse else (left, right)
    assert changed_cells(read_xlsx(before), read_xlsx(after)) == [("journal", "<state>")]


@pytest.mark.parametrize("reverse", [False, True])
def test_os_import_diff_empty_sheet_added_or_removed(tmp_path, reverse):
    from app.services.os_ledger.xlsx import read_xlsx
    from scripts.os_workbook_diff import changed_cells

    left = write_book(tmp_path / "left.xlsx", {"first": [[1]], "last": []})
    right = write_book(tmp_path / "right.xlsx", {"first": [[1]], "empty": [], "last": []})
    before, after = (right, left) if reverse else (left, right)
    assert changed_cells(read_xlsx(before), read_xlsx(after)) == [("empty", "<sheet>")]


def test_os_import_diff_sheet_order(tmp_path):
    from app.services.os_ledger.xlsx import read_xlsx
    from scripts.os_workbook_diff import changed_cells

    left = write_book(tmp_path / "left.xlsx", {"first": [[1]], "last": [[2]]})
    right = write_book(tmp_path / "right.xlsx", {"last": [[2]], "first": [[1]]})
    assert changed_cells(read_xlsx(left), read_xlsx(right)) == [("<workbook>", "sheet_order")]


def test_os_import_diff_identical_workbooks(tmp_path, capsys):
    from app.services.os_ledger.xlsx import read_xlsx
    from scripts.os_workbook_diff import changed_cells, main

    tables = {"first": [[1]], "_hidden": [["private-sheet-content"]], "empty": []}
    left = write_book(tmp_path / "left.xlsx", tables, date_1904=True)
    right = write_book(tmp_path / "right.xlsx", tables, date_1904=True)
    assert changed_cells(read_xlsx(left), read_xlsx(right)) == []
    assert main([str(left), str(right)]) == 0
    output = capsys.readouterr()
    assert output.out == 'os_import op=diff outcome=ok reason=compared changed_cells=0\n'
    assert output.err == ""


def test_os_import_diff_cell_only_exact_ref(tmp_path):
    from app.services.os_ledger.xlsx import read_xlsx
    from scripts.os_workbook_diff import changed_cells

    left = make_xlsx(tmp_path / "left.xlsx", '<c r="A1"><v>12.30</v></c>')
    right = make_xlsx(tmp_path / "right.xlsx", '<c r="A1"><v>12.31</v></c>')
    assert changed_cells(read_xlsx(left), read_xlsx(right)) == [("journal", "A1")]


def test_os_import_diff_cli_counts_metadata_without_content_or_paths(tmp_path, capsys):
    from app.services.os_ledger.xlsx import read_xlsx
    from scripts.os_workbook_diff import changed_cells, main

    left = write_book(tmp_path / "private-before.xlsx", {
        "z": [["private-before-value"]], "a": [["private-sheet-content"]],
    })
    right = write_book(tmp_path / "private-after.xlsx", {
        "a": [["private-sheet-content"]], "empty": [], "z": [["private-after-value"]],
    }, date_1904=True)
    assert changed_cells(read_xlsx(left), read_xlsx(right)) == [
        ("<workbook>", "date1904"), ("<workbook>", "sheet_order"),
        ("empty", "<sheet>"), ("z", "A1"),
    ]
    assert main([str(left), str(right)]) == 0
    output = capsys.readouterr()
    assert output.out == (
        'os_import op=diff outcome=ok reason=compared changed_cells=4\n'
        '("<workbook>", "date1904")\n'
        '("<workbook>", "sheet_order")\n'
        '("empty", "<sheet>")\n'
        '("z", "A1")\n'
    )
    assert "private-" not in output.out
    assert str(tmp_path) not in output.out
    assert output.err == ""
