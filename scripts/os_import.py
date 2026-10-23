"""Local dry-run front end for the workbook importer (contract section 15, V2-30).

It reads one local ``.xlsx``, prepares the batch in memory and prints reason codes and counts, plus
the manifest digest the owner signs. It never touches a database, a settings file, a network or a
credential, and it has no write path: staging and promotion run from the ``os-import`` job (V2-31),
which holds the worker context and the ledger. Output never carries a cell value, a name, an
amount, an id or a path.

    python -m scripts.os_import WORKBOOK --workspace WS --epoch N
        [--cancellations FILE.json] [--manifest-out FILE.json]

Exit status: 0 prepared and promotable, 2 prepared but quarantined, 1 refused.

The summary counts ``quarantined`` (rows set aside) and ``issues`` (problems with the batch itself,
such as an unknown tab) apart; promotion needs both to be zero.
"""

from __future__ import annotations

import argparse
import decimal
import json
import sys
from pathlib import Path

# Support both `python -m scripts.os_import` and direct script execution.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.os_ledger import api
from app.services.os_ledger.import_workbook import (
    WorkbookImportError,
    manifest,
    prepare,
    quarantine_count,
    quarantine_reasons,
)
from app.services.os_ledger.xlsx import XlsxError, read_xlsx


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default echoes caller-provided paths and arguments.
        raise WorkbookImportError("invalid_arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--epoch", required=True, type=int)
    parser.add_argument("--cancellations", type=Path, default=None)
    parser.add_argument("--manifest-out", type=Path, default=None)
    return parser


def _read_cancellations(path: Path | None) -> list:
    if path is None:
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise WorkbookImportError("invalid_cancellations") from None
    if not isinstance(value, list):
        raise WorkbookImportError("invalid_cancellations")
    return value


def _write_manifest(path: Path, document: dict) -> None:
    try:
        # Exclusive create: a review artifact is never silently overwritten.
        with open(path, "x", encoding="utf-8", newline="\n") as target:
            json.dump(document, target, ensure_ascii=False, sort_keys=True, indent=2)
            target.write("\n")
    except OSError:
        raise WorkbookImportError("manifest_output_unwritable") from None


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        cancellations = _read_cancellations(args.cancellations)
        batch = prepare(
            read_xlsx(args.workbook), workspace_id=args.workspace, authority_epoch=args.epoch,
        )
        document = manifest(batch, cancellations=cancellations)
        if args.manifest_out is not None:
            _write_manifest(args.manifest_out, document)
    except (XlsxError, WorkbookImportError) as exc:
        print(f"os_import op=stage outcome=refused reason={exc}", file=sys.stderr)
        return 1
    except (decimal.DecimalException, ArithmeticError):
        # Cell-level numeric failures are quarantine reasons in ``prepare``; this is the backstop
        # that keeps any other decimal error from printing a traceback.
        print("os_import op=stage outcome=refused reason=unsafe_numeric_value", file=sys.stderr)
        return 1
    # Two counts, never summed: rows the importer set aside, and problems with the batch itself
    # (a missing or unknown tab, a failed state validation). Promotion needs both to be zero.
    rows = len(batch.rows)
    quarantined = sum(row.status == "quarantined" for row in batch.rows)
    issues = len(batch.issues)
    outcome = "ok" if not quarantine_count(batch) else "quarantined"
    print(
        f"os_import op=stage outcome={outcome} reason=dry_run rows={rows} "
        f"quarantined={quarantined} issues={issues}"
    )
    for reason, count in quarantine_reasons(batch).items():
        print(f"os_import op=stage_reason outcome=quarantined reason={reason} rows={count}")
    print(
        f"os_import op=manifest outcome={outcome} reason=prepared rows={rows} "
        f"quarantined={quarantined} issues={issues} digest={api.digest(document)}"
    )
    return 0 if outcome == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
