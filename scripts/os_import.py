"""Front end for the workbook importer (contract section 15, V2-30, V2-31b).

One local ``.xlsx`` and three modes. Output is reason codes and counts only: never a cell value, a
name, an amount, an id or a path.

DRY RUN (the default). Reads the file, prepares the batch in memory and prints the counts plus the
manifest digest the owner signs. It never touches a database, a settings file, a network or a
credential, and it has no write path.

    python -m scripts.os_import WORKBOOK --workspace WS --epoch N
        [--cancellations FILE.json] [--manifest-out FILE.json]

    exit status: 0 prepared and promotable, 2 prepared but quarantined, 1 refused.

STAGE and PROMOTE are the ``os-import`` job (``mia os-job os-import``, run from the deployed tag's
mounted ``scripts/``). Each builds its worker context with ``authorize.for_worker("import",
settings)``, calls ``authorize.require(ctx, "import_promote")`` before it touches the ledger, and
refuses ``binding_mismatch`` when ``--workspace`` or ``--epoch`` is not the context's own. They run
as the ledger runtime role: no privileged DSN.

    ... WORKBOOK --workspace WS --epoch N --stage
    ... WORKBOOK --workspace WS --epoch N --promote DIGEST [--cancellations FILE.json]

``--stage`` writes the batch and its rows (``import_workbook.stage``); nothing is promoted. Exit 0
staged clean, 2 staged but quarantined or failed (the batch can never be promoted). Staging the
same workbook again at the same epoch collides on the batch key; if the ledger holds that batch row
it reports ``already_staged`` with the exit status the first staging had (``batch=`` says what the
row can be: ``validated_or_imported`` for a clean workbook, since a promote moves it on, or
``quarantined`` or ``failed``). A collision with no batch row, and any other ledger error, keeps
its code and exits 1.
``--promote`` re-reads the same workbook and promotes it (``promote.promote``) with the manifest
digest the owner approved, and the cancellations he signed. Exit 0 committed or replayed, 1 refused.

The summary counts ``quarantined`` (rows set aside) and ``issues`` (problems with the batch itself,
such as an unknown tab) apart; promotion needs both to be zero.
"""

from __future__ import annotations

import argparse
import decimal
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    stage,
)
from app.services.os_ledger.xlsx import XlsxError, read_xlsx

_DIGEST = re.compile(r"[0-9a-f]{64}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
# What a batch that is already in the ledger looks like to a second ``stage`` of the same workbook
# at the same epoch (the batch id derives from the workspace, the epoch and the file digest, so the
# batch key and the (workspace, epoch, digest) key collide together): PostgreSQL's unique
# violation, SQLSTATE 23505, which tx.database_error() reports as ``integrity_23505``, and the
# conformance suite's name for the same invariant.
_BATCH_ALREADY_THERE = frozenset({"integrity_23505", "import_batch_pk"})


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default echoes caller-provided paths and arguments.
        raise WorkbookImportError("invalid_arguments")


def _digest(value: str) -> str:
    if _DIGEST.fullmatch(value) is None:
        raise WorkbookImportError("invalid_arguments")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--epoch", required=True, type=int)
    parser.add_argument("--cancellations", type=Path, default=None)
    parser.add_argument("--manifest-out", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stage", action="store_true")
    mode.add_argument("--promote", type=_digest, default=None, metavar="DIGEST")
    return parser


def _check_modes(args: argparse.Namespace) -> None:
    """The review artifact belongs to the dry run, the signed cancellations to promote."""
    if args.stage or args.promote is not None:
        if args.manifest_out is not None or (args.stage and args.cancellations is not None):
            raise WorkbookImportError("invalid_arguments")


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


# ---- the dry run ---------------------------------------------------------------------------------


def _dry_run(args: argparse.Namespace) -> int:
    try:
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


# ---- stage and promote ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Deps:
    """What the write modes run on: the settings and the ledger built from them."""

    settings: Any
    ledger: Any


def _live_deps() -> Deps:
    """Production wiring: the settings of the job's env files and the ledger on the runtime DSN."""
    from app.core.config import get_settings

    settings = get_settings()
    return Deps(settings=settings, ledger=api.build_ledger(settings))


def _refusal(op: str, exc: api.LedgerError) -> None:
    """One stderr line: the code, its reason code and the counts the ledger attaches."""
    parts = [f"reason={exc.code}"]
    detail = exc.reason or exc.details.get("reason")
    if isinstance(detail, str) and _CODE.fullmatch(detail):
        parts.append(f"detail={detail}")
    count = exc.details.get("count")
    if type(count) is int:
        parts.append(f"count={count}")
    orphans = exc.details.get("counts")
    if isinstance(orphans, dict):
        parts += [f"orphans_{entity}={orphans[entity]}" for entity in api.ENTITIES
                  if type(orphans.get(entity)) is int]
    print(f"os_import op={op} outcome=refused {' '.join(parts)}", file=sys.stderr)


def _staged(reason: str, status: str, rows: int, quarantined: int, issues: int,
            reasons: dict[str, int], batch: str | None = None) -> int:
    outcome = "ok" if status == "validated" else status
    line = (
        f"os_import op=stage outcome={outcome} reason={reason} rows={rows} "
        f"quarantined={quarantined} issues={issues}"
    )
    print(line if batch is None else f"{line} batch={batch}")
    for why, count in reasons.items():
        print(f"os_import op=stage_reason outcome=quarantined reason={why} rows={count}")
    return 0 if status == "validated" else 2


def _batch_is_there(deps: Deps, ctx: api.TrustedOwnerContext, batch_id: str) -> bool:
    """Whether the ledger holds a settled import batch row with this id for the context.

    ``LedgerTx`` has no batch reader, so this is the read the import API allows:
    ``insert_import_rows`` with no rows writes nothing and names the row's state. A settled batch
    is ``import_status_invalid`` (its rows are insert-only once it settles); a missing one is
    ``ledger_integrity_failed`` with reason ``import_row_batch_fk``. The transaction ends in a
    rollback or an empty commit. Any other answer is the probe's own failure and propagates under
    its own code.
    """
    try:
        with deps.ledger.transaction(ctx, domain="core", kind="import") as tx:
            tx.insert_import_rows(batch_id, ())
    except api.LedgerError as exc:
        if exc.code == "import_status_invalid":
            return True
        if exc.code == "ledger_integrity_failed" and exc.reason == "import_row_batch_fk":
            return False
        raise
    return False  # a batch still ``staged`` outside its own transaction is not a settled one


def _stage(deps: Deps, ctx: api.TrustedOwnerContext, workbook: Any) -> int:
    try:
        result = stage(deps.ledger, ctx, workbook)
    except api.LedgerError as exc:
        if exc.code != "ledger_integrity_failed" or exc.reason not in _BATCH_ALREADY_THERE:
            raise
        collision = exc
    else:
        return _staged(
            "persisted", result.status, result.rows, result.quarantined,
            len(result.batch.issues), dict(result.reasons),
        )
    # A unique-key collision is "already staged" only when the batch row is in the ledger; any
    # other key (a future one) keeps its own error.
    batch = prepare(
        workbook, workspace_id=ctx.workspace, authority_epoch=ctx.authority_epoch,
    )
    if not _batch_is_there(deps, ctx, batch.batch_id):
        raise collision
    # What the row is settled as cannot be read (``LedgerTx`` has no batch reader), so the line
    # claims only what is certain. ``prepare`` is deterministic: a workbook with quarantined rows
    # or issues settled as ``quarantined`` or ``failed``, both terminal. A clean one settled as
    # ``validated`` and is ``imported`` once promoted, so the line names both and claims neither.
    quarantined = sum(row.status == "quarantined" for row in batch.rows)
    status = "quarantined" if quarantined else ("failed" if batch.issues else "validated")
    return _staged(
        "already_staged", status, len(batch.rows), quarantined, len(batch.issues),
        quarantine_reasons(batch) if status != "validated" else {},
        batch="validated_or_imported" if status == "validated" else status,
    )


def _promote(deps: Deps, ctx: api.TrustedOwnerContext, workbook: Any, approved: str,
             cancellations: list) -> int:
    from app.services.os_ledger.promote import promote

    batch = prepare(workbook, workspace_id=ctx.workspace, authority_epoch=ctx.authority_epoch)
    result = promote(deps.ledger, ctx, batch, approved, cancellations=cancellations)
    print(
        f"os_import op=promote outcome=ok reason={'replayed' if result.replayed else 'committed'} "
        f"created={result.created} updated={result.updated} unchanged={result.unchanged} "
        f"cancelled={result.cancelled}"
    )
    return 0


def _write(args: argparse.Namespace, deps_factory: Callable[[], Deps]) -> int:
    op = "promote" if args.promote is not None else "stage"
    try:
        from app.services.os_ledger import authorize

        deps = deps_factory()
        ctx = authorize.for_worker("import", deps.settings)
        authorize.require(ctx, "import_promote")
        if (args.workspace, args.epoch) != (ctx.workspace, ctx.authority_epoch):
            raise WorkbookImportError("binding_mismatch")
        cancellations = _read_cancellations(args.cancellations)
        workbook = read_xlsx(args.workbook)
        if op == "stage":
            return _stage(deps, ctx, workbook)
        return _promote(deps, ctx, workbook, args.promote, cancellations)
    except api.LedgerError as exc:
        _refusal(op, exc)
    except (XlsxError, WorkbookImportError) as exc:
        print(f"os_import op={op} outcome=refused reason={exc}", file=sys.stderr)
    except (decimal.DecimalException, ArithmeticError):
        print(f"os_import op={op} outcome=refused reason=unsafe_numeric_value", file=sys.stderr)
    except Exception:
        # Never print driver, provider or connection text from a job that holds the ledger.
        print(f"os_import op={op} outcome=refused reason=internal_error", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None, *, deps_factory: Callable[[], Deps] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        _check_modes(args)
    except WorkbookImportError as exc:
        print(f"os_import op=stage outcome=refused reason={exc}", file=sys.stderr)
        return 1
    if args.stage or args.promote is not None:
        return _write(args, deps_factory or _live_deps)
    return _dry_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
