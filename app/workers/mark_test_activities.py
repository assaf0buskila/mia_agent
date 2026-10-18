"""Guarded operator for excluding two reviewed CRM activities from lead metrics."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from app.db.models import CrmActivityRow, CrmFormIntakeReceiptRow
from app.db.session import get_session_factory

_APPLY_CONFIRMATION = "mark-reviewed-test-activities"
_EXPECTED_COUNT = 2


class ActivitySelectionError(ValueError):
    """The requested rows did not match the immutable intake evidence."""


@dataclass(frozen=True)
class TestActivityReport:
    dry_run: bool
    matched: int
    changed: int
    source_ids: tuple[str, ...]
    activity_ids: tuple[str, ...]
    contact_id: str


def _parse_pair(raw: str) -> tuple[str, str]:
    source_id, separator, activity_id = raw.partition("=")
    source_id = source_id.strip()
    activity_id = activity_id.strip()
    if not separator or not source_id or not activity_id:
        raise argparse.ArgumentTypeError("expected SOURCE_ID=ACTIVITY_ID")
    return source_id, activity_id


def mark_reviewed_test_activities(
    session: Session,
    *,
    source_activity_pairs: Sequence[tuple[str, str]],
    contact_id: str,
    apply: bool = False,
) -> TestActivityReport:
    """Validate exactly two receipt-backed activities, then optionally flag only them."""
    pairs = tuple((source.strip(), activity.strip()) for source, activity in source_activity_pairs)
    safe_contact_id = contact_id.strip()
    if len(pairs) != _EXPECTED_COUNT:
        raise ActivitySelectionError("exactly two source/activity pairs are required")
    if not safe_contact_id or any(not source or not activity for source, activity in pairs):
        raise ActivitySelectionError("source, activity and contact ids are required")
    if len({source for source, _ in pairs}) != _EXPECTED_COUNT:
        raise ActivitySelectionError("source ids must be distinct")
    if len({activity for _, activity in pairs}) != _EXPECTED_COUNT:
        raise ActivitySelectionError("activity ids must be distinct")

    rows: list[CrmActivityRow] = []
    for source_id, activity_id in pairs:
        receipt = session.get(CrmFormIntakeReceiptRow, source_id)
        if receipt is None:
            raise ActivitySelectionError(f"receipt not found for source {source_id}")
        if receipt.activity_id != activity_id or receipt.contact_id != safe_contact_id:
            raise ActivitySelectionError(
                f"receipt relationship mismatch for source {source_id}"
            )

        activity = session.get(CrmActivityRow, activity_id)
        expected_source_ref = f"assafweb-form:{source_id}:activity"
        if activity is None:
            raise ActivitySelectionError(f"activity not found for source {source_id}")
        if (
            activity.contact_id != safe_contact_id
            or activity.source_ref != expected_source_ref
            or activity.channel != "form"
            or activity.action != "contact_captured"
        ):
            raise ActivitySelectionError(f"activity evidence mismatch for source {source_id}")
        rows.append(activity)

    changed = sum(not row.is_test for row in rows)
    if apply:
        for row in rows:
            row.is_test = True
        session.commit()

    return TestActivityReport(
        dry_run=not apply,
        matched=len(rows),
        changed=changed if apply else 0,
        source_ids=tuple(source for source, _ in pairs),
        activity_ids=tuple(activity for _, activity in pairs),
        contact_id=safe_contact_id,
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-activity",
        action="append",
        type=_parse_pair,
        required=True,
        metavar="SOURCE_ID=ACTIVITY_ID",
        help="Repeat exactly twice; each pair must match one immutable intake receipt.",
    )
    parser.add_argument("--contact-id", required=True, help="Expected shared CRM contact id.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the flags. Without this option the command is read-only.",
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"With --apply, must be exactly {_APPLY_CONFIRMATION!r}.",
    )
    args = parser.parse_args(argv)
    if args.apply and args.confirm != _APPLY_CONFIRMATION:
        parser.error(f"--apply requires --confirm {_APPLY_CONFIRMATION}")
    if not args.apply and args.confirm:
        parser.error("--confirm is only valid with --apply")
    return args


def main() -> None:
    args = _parse_args()
    try:
        with get_session_factory()() as session:
            report = mark_reviewed_test_activities(
                session,
                source_activity_pairs=args.source_activity,
                contact_id=args.contact_id,
                apply=args.apply,
            )
            if not args.apply:
                session.rollback()
    except ActivitySelectionError as exc:
        sys.exit(f"refusing activity update: {exc}")
    print(json.dumps(asdict(report), sort_keys=True))


if __name__ == "__main__":
    main()
