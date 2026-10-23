"""Local diff output discloses coordinates only."""

from tests.fixtures.workbooks.synthetic import make_xlsx


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
