"""Local XLSX diff: coordinates only, including hidden sheets and formulas."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Support both `python -m scripts.os_workbook_diff` and direct script execution.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.os_ledger.xlsx import Workbook, XlsxError, read_xlsx


def changed_cells(left: Workbook, right: Workbook) -> list[tuple[str, str]]:
    """Compare cells and workbook structure, returning only coordinates and synthetic refs."""
    before = {(sheet.name, ref): cell for sheet in left.sheets for ref, cell in sheet.cells.items()}
    after = {(sheet.name, ref): cell for sheet in right.sheets for ref, cell in sheet.cells.items()}
    changed = {ref for ref in before.keys() | after.keys() if before.get(ref) != after.get(ref)}
    left_sheets = {sheet.name: sheet for sheet in left.sheets}
    right_sheets = {sheet.name: sheet for sheet in right.sheets}
    common = left_sheets.keys() & right_sheets.keys()
    if left.date_1904 != right.date_1904:
        changed.add(("<workbook>", "date1904"))
    changed.update((name, "<sheet>") for name in left_sheets.keys() ^ right_sheets.keys())
    changed.update((name, "<state>") for name in common
                   if left_sheets[name].state != right_sheets[name].state)
    if ([sheet.name for sheet in left.sheets if sheet.name in common]
            != [sheet.name for sheet in right.sheets if sheet.name in common]):
        changed.add(("<workbook>", "sheet_order"))
    return sorted(changed)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default includes caller-provided paths/arguments.
        raise XlsxError("invalid_arguments")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    try:
        args = parser.parse_args(argv)
        refs = changed_cells(read_xlsx(args.before), read_xlsx(args.after))
    except XlsxError as exc:
        print(f"os_import op=diff outcome=refused reason={exc}", file=sys.stderr)
        return 1
    print(f"os_import op=diff outcome=ok reason=compared changed_cells={len(refs)}")
    for sheet, ref in refs:
        # JSON escaping prevents a sheet title from injecting extra log lines.
        print(f"({json.dumps(sheet, ensure_ascii=False)}, {json.dumps(ref)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
