"""Synthetic ZIPs only; never load an owner workbook."""

from zipfile import ZIP_DEFLATED

import pytest

from tests.fixtures.workbooks.synthetic import NS, PKG, REL, make_xlsx


def test_os_import_xlsx_types_and_hidden_sheet(tmp_path):
    from app.services.os_ledger.xlsx import read_xlsx

    path = make_xlsx(tmp_path / "synthetic.xlsx", (
        '<c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="inlineStr"><is><t>החזר</t></is></c>'
        '<c r="C1"><v>12.30</v></c><c r="D1" t="b"><v>1</v></c>'
        '<c r="E1" s="1"><v>45000</v></c><c r="F1" s="2"><v>45001</v></c>'
        '<c r="G1" s="3"><v>4.00</v></c><c r="H1"><f>1+1</f><v>2</v></c>'
    ))
    book = read_xlsx(path)
    sheet = book.sheets[0]
    assert sheet.state == "hidden"
    assert sheet.cells["A1"].value == "בדיקה בלבד"
    assert sheet.cells["B1"].value == "החזר"
    assert sheet.cells["C1"].value == "12.30"
    assert sheet.cells["D1"].value is True
    assert sheet.cells["E1"].value == "45000"
    assert sheet.cells["E1"].date_format and sheet.cells["F1"].date_format
    assert not sheet.cells["G1"].date_format
    assert sheet.cells["H1"].formula == "1+1"
    assert book.source_digest == read_xlsx(path).source_digest


def test_os_import_synthetic_books_are_byte_reproducible(tmp_path, monkeypatch):
    """A fixture's source digest must not depend on the clock: zip members carry a DOS time, and
    "the same workbook" in the epoch tests would otherwise differ across a 2-second boundary."""
    import time

    from tests.fixtures.workbooks.synthetic import write_book

    tables = {"one": [["a", 1]], "_two": [["b"]]}
    built = (
        write_book(tmp_path / "a.xlsx", tables).read_bytes(),
        make_xlsx(tmp_path / "b.xlsx", '<c r="A1"><v>1</v></c>').read_bytes(),
    )
    real = time.localtime
    monkeypatch.setattr(
        time, "localtime", lambda seconds=None: real((seconds or time.time()) + 90_000_000))
    later = (
        write_book(tmp_path / "c.xlsx", tables).read_bytes(),
        make_xlsx(tmp_path / "d.xlsx", '<c r="A1"><v>1</v></c>').read_bytes(),
    )
    assert built == later


@pytest.mark.parametrize("name", ["/escape", "../escape", "xl/../escape", "C:/escape",
                                 "\\escape", "xl\\..\\escape"])
def test_os_import_xlsx_refuses_member_paths(tmp_path, name):
    from app.services.os_ledger.xlsx import XlsxError, read_xlsx

    path = make_xlsx(tmp_path / "synthetic.xlsx", extra={name: "x"})
    with pytest.raises(XlsxError, match="^unsafe_member$"):
        read_xlsx(path)


@pytest.mark.parametrize("declaration", ["<!DOCTYPE x>", '<!ENTITY x "x">'])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_os_import_xlsx_refuses_declarations_any_member(tmp_path, declaration, encoding):
    from app.services.os_ledger.xlsx import XlsxError, read_xlsx

    path = make_xlsx(tmp_path / "synthetic.xlsx", extra={
        "unreferenced.bin": declaration.encode(encoding),
    })
    with pytest.raises(XlsxError, match="^xml_declaration_forbidden$"):
        read_xlsx(path)


def test_os_import_xlsx_ratio_bomb(tmp_path):
    from app.services.os_ledger.xlsx import XlsxError, read_xlsx

    path = make_xlsx(tmp_path / "synthetic.xlsx", extra={"bomb": "x" * 100_000},
                     compression=ZIP_DEFLATED)
    with pytest.raises(XlsxError, match="^compression_ratio_limit$"):
        read_xlsx(path)


def test_os_import_xlsx_size_bomb(tmp_path, monkeypatch):
    from app.services.os_ledger import xlsx

    assert xlsx.MAX_UNCOMPRESSED_BYTES == 200 * 1024 * 1024
    path = make_xlsx(tmp_path / "synthetic.xlsx")
    monkeypatch.setattr(xlsx, "MAX_UNCOMPRESSED_BYTES", 100)
    with pytest.raises(xlsx.XlsxError, match="^uncompressed_size_limit$"):
        xlsx.read_xlsx(path)


def test_os_import_xlsx_cell_bomb_in_unreferenced_part(tmp_path, monkeypatch):
    from app.services.os_ledger import xlsx

    assert xlsx.MAX_CELLS == 2_000_000
    path = make_xlsx(tmp_path / "synthetic.xlsx", '<c r="A1"><v>1</v></c>', extra={
        "xl/worksheets/orphan.xml": f'<worksheet xmlns="{NS}"><c/><c/></worksheet>',
    })
    monkeypatch.setattr(xlsx, "MAX_CELLS", 2)
    with pytest.raises(xlsx.XlsxError, match="^cell_count_limit$"):
        xlsx.read_xlsx(path)


@pytest.mark.parametrize("cells", [
    '<c r="A1" t="s"><v>-1</v></c>', '<c r="A1"><v>NaN</v></c>',
    '<c r="A1" t="b"><v>2</v></c>', '<c r="A1" s="99"><v>1</v></c>',
    '<c r="A1"/><c r="A1"/>', '<c r="XFE1"/>',
])
def test_os_import_xlsx_refuses_invalid_cells(tmp_path, cells):
    from app.services.os_ledger.xlsx import XlsxError, read_xlsx

    with pytest.raises(XlsxError):
        read_xlsx(make_xlsx(tmp_path / "synthetic.xlsx", cells))


def test_os_import_xlsx_errors_never_expose_input(tmp_path):
    from app.services.os_ledger.xlsx import XlsxError, read_xlsx

    path = tmp_path / "private-name.xlsx"
    path.write_bytes(b"private-value")
    with pytest.raises(XlsxError) as raised:
        read_xlsx(path)
    assert str(raised.value) == "invalid_xlsx"


def test_os_import_xlsx_cell_limit_non_xml_target(tmp_path, monkeypatch):
    from app.services.os_ledger import xlsx

    path = make_xlsx(tmp_path / "synthetic.xlsx", extra={
        "xl/_rels/workbook.xml.rels": (
            f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" '
            f'Type="{REL}/worksheet" Target="worksheets/sheet1.bin"/></Relationships>'
        ),
        "xl/worksheets/sheet1.bin": (
            f'<worksheet xmlns="{NS}"><sheetData><row r="1">'
            '<c r="A1"/><c r="B1"/><c r="C1"/></row></sheetData></worksheet>'
        ),
    })
    monkeypatch.setattr(xlsx, "MAX_CELLS", 2)
    with pytest.raises(xlsx.XlsxError, match="^cell_count_limit$"):
        xlsx.read_xlsx(path)
