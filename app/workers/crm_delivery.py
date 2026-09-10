"""Lease-based CRM destination delivery and editable Sheets import."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event, RLock
from typing import Literal, Protocol
from uuid import uuid4

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.orm import Session, aliased

from app.db.models import (
    CrmContactRow,
    CrmIssueContactRow,
    CrmIssueRow,
    CrmOutboxRow,
    CrmSyncSnapshotRow,
    CrmWorkerStateRow,
)
from app.domain.tools import AdapterHttpError, AdapterResponseError
from app.services.crm_v2 import (
    CRM_PROJECTION_LOCK_KEY,
    CrmService,
    normalize_email,
    normalize_phone,
)

POLL_SECONDS = 5
IMPORT_SECONDS = 60
LEASE_SECONDS = 30
MAX_BATCH = 25
MAX_SHEET_ROWS = 100_000
_PROCESS_CRM_EFFECT_LOCK = RLock()

DeliveryOutcome = Literal["confirmed", "failed", "unknown", "conflict"]


class CrmSheetsPort(Protocol):
    def ensure_crm_workspace(self) -> None: ...

    def read_crm_contacts_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]: ...

    def read_crm_activity_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]: ...

    def upsert_crm_contact(self, cells: list[str]) -> None: ...

    def upsert_crm_activity(self, cells: list[str]) -> None: ...

    def update_crm_contact_fields(
        self, *, row_number: int, fields: dict[int, str]
    ) -> None: ...

    def append_crm_contact(self, cells: list[str]) -> None: ...

    def append_crm_activity(self, cells: list[str]) -> None: ...


TelegramHandler = Callable[[Mapping[str, object], bool], DeliveryOutcome]


@dataclass(frozen=True)
class WorkerRun:
    imported_rows: int = 0
    import_failed: bool = False
    claimed: int = 0
    confirmed: int = 0
    failed: int = 0
    unknown: int = 0
    conflicts: int = 0


class CrmDeliveryWorker:
    """Small synchronous worker suitable for a dedicated thread or one-off run.

    ``telegram_handler(payload, reconcile_only)`` is the sole Telegram bridge. The
    integration layer must reuse the existing receipt ledger and must never send when
    ``reconcile_only`` is true.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        sheets: CrmSheetsPort,
        telegram_handler: TelegramHandler | None = None,
        allowed_telegram_recipient_ids: frozenset[str] = frozenset(),
        worker_id: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._sheets = sheets
        self._telegram_handler = telegram_handler
        self._allowed_telegram_recipient_ids = frozenset(
            value for value in allowed_telegram_recipient_ids if value.isdigit()
        )
        self.worker_id = worker_id or f"crm-worker-{uuid4().hex[:12]}"
        self._clock = now or (lambda: datetime.now(UTC))

    def _now_dt(self) -> datetime:
        value = self._clock()
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def _now(self) -> str:
        return self._now_dt().isoformat()

    def run_once(self, *, force_import: bool = False, limit: int = MAX_BATCH) -> WorkerRun:
        import_failed = False
        try:
            imported = self.sync_from_sheets(force=force_import)
        except (
            AdapterHttpError,
            AdapterResponseError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            imported = 0
            import_failed = True
        self._recover_expired_leases()
        reconciled = self._reconcile_unknown(limit=max(1, min(limit, MAX_BATCH)))
        counts = {
            "claimed": 0,
            "confirmed": reconciled.count("confirmed"),
            "failed": reconciled.count("failed"),
            "unknown": reconciled.count("unknown"),
            "conflicts": reconciled.count("conflict"),
        }
        for _ in range(max(1, min(limit, MAX_BATCH))):
            job_id = self._claim_one(skip_contacts=import_failed)
            if job_id is None:
                break
            counts["claimed"] += 1
            outcome = self._deliver(job_id)
            key = {
                "confirmed": "confirmed",
                "failed": "failed",
                "unknown": "unknown",
                "conflict": "conflicts",
            }[outcome]
            counts[key] += 1
        return WorkerRun(imported_rows=imported, import_failed=import_failed, **counts)

    def run_forever(self, stop_event: Event) -> None:
        while not stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - the next poll is the recovery boundary
                pass
            stop_event.wait(POLL_SECONDS)

    def sync_from_sheets(self, *, force: bool = False) -> int:
        now = self._now_dt()
        with self._session_factory() as session:
            state = session.get(CrmWorkerStateRow, "contacts_last_import")
            if not force and state is not None:
                try:
                    last = datetime.fromisoformat(state.value)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=UTC)
                    if now - last < timedelta(seconds=IMPORT_SECONDS):
                        return 0
                except ValueError:
                    pass

        with _PROCESS_CRM_EFFECT_LOCK, self._session_factory() as session:
            self._acquire_effect_lock(session)
            state = session.get(CrmWorkerStateRow, "contacts_last_import")
            if not force and state is not None:
                try:
                    locked_last = datetime.fromisoformat(state.value)
                    if locked_last.tzinfo is None:
                        locked_last = locked_last.replace(tzinfo=UTC)
                    if now - locked_last < timedelta(seconds=IMPORT_SECONDS):
                        session.commit()
                        return 0
                except ValueError:
                    pass
            self._sheets.ensure_crm_workspace()
            rows: list[tuple[int, list[str]]] = []
            start_row = 2
            while start_row <= MAX_SHEET_ROWS + 1:
                chunk = self._sheets.read_crm_contacts_chunk(start_row=start_row, limit=100)
                rows.extend((start_row + offset, row) for offset, row in enumerate(chunk))
                if len(chunk) < 100:
                    break
                start_row += 100

            service = CrmService(session, now=self._clock)
            seen_ids: set[str] = set()
            for row_number, row in rows:
                if any(str(value or "").strip() for value in row):
                    imported = service.import_sheet_contact(row, row_number=row_number)
                    if imported.contact is not None:
                        seen_ids.add(imported.contact.id)
            service.note_missing_sheet_contacts(seen_ids)
            state = session.get(CrmWorkerStateRow, "contacts_last_import")
            if state is None:
                state = CrmWorkerStateRow(key="contacts_last_import")
                session.add(state)
            state.value = now.isoformat()
            state.updated_at = now.isoformat()
            session.commit()
        return len(rows)

    @staticmethod
    def _acquire_effect_lock(session: Session) -> None:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": CRM_PROJECTION_LOCK_KEY},
            )

    @staticmethod
    def _resolution_issue_ids(payload: Mapping[str, object]) -> set[str]:
        issue_ids: set[str] = set()
        legacy = str(payload.get("resolution_issue_id") or "")
        if legacy:
            issue_ids.add(legacy)
        values = payload.get("resolution_issue_ids")
        if isinstance(values, list):
            issue_ids.update(str(value) for value in values if value)
        return issue_ids

    @staticmethod
    def _has_blocking_contact_issue(
        session: Session, contact_id: str, approved_issue_ids: set[str]
    ) -> bool:
        unresolved = session.scalars(
            select(CrmIssueRow).where(
                or_(
                    CrmIssueRow.contact_id == contact_id,
                    CrmIssueRow.id.in_(
                        select(CrmIssueContactRow.issue_id).where(
                            CrmIssueContactRow.contact_id == contact_id
                        )
                    ),
                ),
                CrmIssueRow.status.in_(("open", "resolving")),
            )
        ).all()
        unresolved_ids = {issue.id for issue in unresolved}
        return any(
            issue.status != "resolving" or issue.id not in approved_issue_ids
            for issue in unresolved
        ) or bool(approved_issue_ids - unresolved_ids)

    def _recover_expired_leases(self) -> None:
        now = self._now()
        with _PROCESS_CRM_EFFECT_LOCK, self._session_factory() as session:
            self._acquire_effect_lock(session)
            session.execute(
                update(CrmOutboxRow)
                .where(
                    CrmOutboxRow.status == "in_flight",
                    CrmOutboxRow.lease_expires_at <= now,
                )
                .values(
                    status="unknown",
                    lease_owner="",
                    lease_expires_at="",
                    last_error="worker lease expired after a possible external effect",
                )
            )
            session.commit()

    def _claim_one(self, *, skip_contacts: bool = False) -> str | None:
        now = self._now()
        lease_until = (self._now_dt() + timedelta(seconds=LEASE_SECONDS)).isoformat()
        with self._session_factory() as session:
            predecessor = aliased(CrmOutboxRow)
            statement = select(CrmOutboxRow).where(
                    CrmOutboxRow.status.in_(("pending", "failed")),
                    CrmOutboxRow.next_attempt_at <= now,
                    ~select(predecessor.id)
                    .where(
                        predecessor.aggregate_id == CrmOutboxRow.aggregate_id,
                        predecessor.destination == CrmOutboxRow.destination,
                        or_(
                            predecessor.created_at < CrmOutboxRow.created_at,
                            and_(
                                predecessor.created_at == CrmOutboxRow.created_at,
                                predecessor.id < CrmOutboxRow.id,
                            ),
                        ),
                        predecessor.status.notin_(("confirmed", "conflict")),
                    )
                    .exists(),
                )
            if skip_contacts:
                statement = statement.where(CrmOutboxRow.destination != "contacts")
            cursor: tuple[str, str] | None = None
            while True:
                page = statement
                if cursor is not None:
                    page = page.where(
                        or_(
                            CrmOutboxRow.created_at > cursor[0],
                            and_(
                                CrmOutboxRow.created_at == cursor[0],
                                CrmOutboxRow.id > cursor[1],
                            ),
                        )
                    )
                candidates = session.scalars(
                    page.order_by(CrmOutboxRow.created_at, CrmOutboxRow.id).limit(MAX_BATCH)
                ).all()
                if not candidates:
                    session.commit()
                    return None
                for candidate in candidates:
                    cursor = (candidate.created_at, candidate.id)
                    if candidate.destination == "contacts":
                        try:
                            candidate_payload = json.loads(candidate.payload_json)
                        except (TypeError, json.JSONDecodeError):
                            candidate_payload = {}
                        approved_issue_ids = self._resolution_issue_ids(candidate_payload)
                        if self._has_blocking_contact_issue(
                            session, candidate.aggregate_id, approved_issue_ids
                        ):
                            continue
                    claimed = session.execute(
                        update(CrmOutboxRow)
                        .where(
                            CrmOutboxRow.id == candidate.id,
                            CrmOutboxRow.status == candidate.status,
                        )
                        .values(
                            status="in_flight",
                            lease_owner=self.worker_id,
                            lease_expires_at=lease_until,
                            last_attempt_at=now,
                            attempts=CrmOutboxRow.attempts + 1,
                        )
                        .returning(CrmOutboxRow.id)
                    ).scalar_one_or_none()
                    if claimed is not None:
                        session.commit()
                        return candidate.id

    def _deliver(self, job_id: str) -> DeliveryOutcome:
        with _PROCESS_CRM_EFFECT_LOCK, self._session_factory() as session:
            self._acquire_effect_lock(session)
            job = session.get(CrmOutboxRow, job_id)
            if job is None or job.status != "in_flight" or job.lease_owner != self.worker_id:
                return "conflict"
            if job.lease_expires_at <= self._now():
                self._finish(session, job, "unknown")
                session.commit()
                return "unknown"
            try:
                payload = json.loads(job.payload_json)
                if not isinstance(payload, dict):
                    raise ValueError("job payload is not an object")
                if job.destination == "contacts":
                    contact = session.get(CrmContactRow, job.aggregate_id)
                    if contact is None or contact.revision != int(payload.get("revision", -1)):
                        outcome = "conflict"
                    elif self._has_blocking_contact_issue(
                        session,
                        job.aggregate_id,
                        self._resolution_issue_ids(payload),
                    ):
                        outcome = "conflict"
                    else:
                        outcome = self._deliver_contact(session, payload)
                else:
                    outcome = self._dispatch(job.destination, payload, reconcile_only=False)
            except AdapterResponseError:
                outcome = "failed"
            except ValueError:
                outcome = "failed"
            except (AdapterHttpError, OSError, RuntimeError, TypeError):
                outcome = "unknown"
            self._finish(session, job, outcome)
            session.commit()
            return outcome

    def _dispatch(
        self, destination: str, payload: Mapping[str, object], *, reconcile_only: bool
    ) -> DeliveryOutcome:
        if destination == "contacts":
            if reconcile_only:
                return self._reconcile_contact(payload)
            return "failed"
        if destination == "activity":
            if reconcile_only:
                return self._reconcile_activity(payload)
            cells = payload.get("cells")
            if not isinstance(cells, list) or not all(isinstance(cell, str) for cell in cells):
                return "failed"
            existing = self._find_activity_rows(str(payload.get("activity_id") or cells[5]))
            if existing:
                return "confirmed"
            self._sheets.append_crm_activity(cells)
            return "confirmed"
        if destination == "telegram":
            recipient_id = str(payload.get("recipient_id") or "")
            if recipient_id not in self._allowed_telegram_recipient_ids:
                return "failed"
            if self._telegram_handler is None:
                return "failed"
            return self._telegram_handler(payload, reconcile_only)
        return "failed"

    def _reconcile_unknown(self, *, limit: int) -> list[DeliveryOutcome]:
        with self._session_factory() as session:
            ids = session.scalars(
                select(CrmOutboxRow.id)
                .where(CrmOutboxRow.status == "unknown")
                .order_by(CrmOutboxRow.last_attempt_at, CrmOutboxRow.id)
                .limit(limit)
            ).all()
        outcomes: list[DeliveryOutcome] = []
        for job_id in ids:
            with _PROCESS_CRM_EFFECT_LOCK, self._session_factory() as session:
                self._acquire_effect_lock(session)
                job = session.get(CrmOutboxRow, job_id)
                if job is None or job.status != "unknown":
                    continue
                try:
                    payload = json.loads(job.payload_json)
                    outcome = (
                        self._dispatch(job.destination, payload, reconcile_only=True)
                        if isinstance(payload, dict)
                        else "failed"
                    )
                except (AdapterHttpError, AdapterResponseError, OSError, RuntimeError, TypeError):
                    outcome = "unknown"
                self._finish(session, job, outcome)
                session.commit()
                outcomes.append(outcome)
        return outcomes

    def _reconcile_contact(self, payload: Mapping[str, object]) -> DeliveryOutcome:
        cells = payload.get("cells")
        if not isinstance(cells, list) or len(cells) != 15:
            return "failed"
        contact_id = str(payload.get("contact_id") or cells[14])
        matches = self._find_contact_rows(contact_id)
        if len(matches) > 1:
            return "conflict"
        if not matches:
            return "unknown"
        legacy = payload.get("legacy_binding")
        if isinstance(legacy, dict):
            observed = legacy.get("cells")
            return (
                "confirmed"
                if isinstance(observed, list) and matches[0][1][:14] == observed
                else "unknown"
            )
        return "confirmed" if matches[0][1][:15] == cells else "unknown"

    def _reconcile_activity(self, payload: Mapping[str, object]) -> DeliveryOutcome:
        cells = payload.get("cells")
        if not isinstance(cells, list) or len(cells) != 6:
            return "failed"
        activity_id = str(payload.get("activity_id") or cells[5])
        return "confirmed" if self._find_activity_rows(activity_id) else "unknown"

    def _deliver_contact(
        self, session: Session, payload: Mapping[str, object]
    ) -> DeliveryOutcome:
        cells = payload.get("cells")
        base = payload.get("base_cells")
        if (
            not isinstance(cells, list)
            or len(cells) != 15
            or not all(isinstance(cell, str) for cell in cells)
            or not isinstance(base, list)
            or len(base) != 14
            or not all(isinstance(cell, str) for cell in base)
        ):
            return "failed"
        contact_id = str(payload.get("contact_id") or cells[14])
        legacy = payload.get("legacy_binding")
        if isinstance(legacy, dict):
            observed = legacy.get("cells")
            if not isinstance(observed, list) or len(observed) != 14:
                return "failed"
            candidates = [
                (row_number, row)
                for row_number, row in self._all_contact_rows()
                if not row[14] and row[:14] == observed
            ]
            if len(candidates) != 1:
                CrmService(session, now=self._clock).record_projection_issue(
                    contact_id,
                    issue_type="legacy_binding_changed",
                    details={"matches": len(candidates)},
                )
                return "conflict"
            self._sheets.update_crm_contact_fields(
                row_number=candidates[0][0], fields={14: contact_id}
            )
            return "confirmed"

        matches = self._find_contact_rows(contact_id)
        if len(matches) > 1:
            CrmService(session, now=self._clock).record_projection_issue(
                contact_id,
                issue_type="duplicate_sheet_id",
                details={"matches": len(matches)},
            )
            return "conflict"
        if not matches:
            was_projected = session.scalars(
                select(CrmSyncSnapshotRow.contact_id)
                .where(CrmSyncSnapshotRow.contact_id == contact_id)
                .limit(1)
            ).first()
            if was_projected is not None:
                CrmService(session, now=self._clock).record_projection_issue(
                    contact_id,
                    issue_type="missing_sheet_row",
                )
                return "conflict"
            desired_phone = normalize_phone(cells[1])
            desired_email = normalize_email(cells[2])
            for _row_number, other in self._all_contact_rows():
                if other[14] and other[14] != contact_id and (
                    (desired_phone and normalize_phone(other[1]) == desired_phone)
                    or (desired_email and normalize_email(other[2]) == desired_email)
                ):
                    CrmService(session, now=self._clock).record_projection_issue(
                        contact_id, issue_type="destination_identity_collision"
                    )
                    return "conflict"
            self._sheets.append_crm_contact(cells)
            return "confirmed"

        row_number, current = matches[0]
        destination_changed = any(
            current[index] != base[index] and current[index] != cells[index]
            for index in range(14)
        )
        if destination_changed:
            CrmService(session, now=self._clock).import_sheet_contact(
                current, row_number=row_number
            )
            return "conflict"
        desired_phone = normalize_phone(cells[1])
        desired_email = normalize_email(cells[2])
        for other_row_number, other in self._all_contact_rows():
            if other_row_number == row_number:
                continue
            if (
                (desired_phone and normalize_phone(other[1]) == desired_phone)
                or (desired_email and normalize_email(other[2]) == desired_email)
            ):
                CrmService(session, now=self._clock).record_projection_issue(
                    contact_id, issue_type="destination_identity_collision"
                )
                return "conflict"
        updates = {
            index: cells[index]
            for index in range(14)
            if current[index] == base[index] and current[index] != cells[index]
        }
        if updates:
            self._sheets.update_crm_contact_fields(row_number=row_number, fields=updates)
        return "confirmed"

    def _all_contact_rows(self) -> list[tuple[int, list[str]]]:
        found: list[tuple[int, list[str]]] = []
        start_row = 2
        while start_row <= MAX_SHEET_ROWS + 1:
            rows = self._sheets.read_crm_contacts_chunk(start_row=start_row, limit=100)
            for offset, row in enumerate(rows):
                padded = [str(value or "") for value in row[:15]]
                padded.extend([""] * (15 - len(padded)))
                found.append((start_row + offset, padded))
            if len(rows) < 100:
                break
            start_row += 100
        return found

    def _find_contact_rows(self, contact_id: str) -> list[tuple[int, list[str]]]:
        return [item for item in self._all_contact_rows() if item[1][14] == contact_id]

    def _find_activity_rows(self, activity_id: str) -> list[list[str]]:
        found: list[list[str]] = []
        start_row = 2
        while start_row <= MAX_SHEET_ROWS + 1:
            rows = self._sheets.read_crm_activity_chunk(start_row=start_row, limit=100)
            found.extend(row for row in rows if len(row) > 5 and str(row[5]) == activity_id)
            if len(rows) < 100:
                break
            start_row += 100
        return found

    def _finish(self, session: Session, job: CrmOutboxRow, outcome: DeliveryOutcome) -> None:
        now = self._now()
        expected_status = job.status
        predicates = [CrmOutboxRow.id == job.id, CrmOutboxRow.status == expected_status]
        if expected_status == "in_flight":
            predicates.append(CrmOutboxRow.lease_owner == self.worker_id)
        elif expected_status != "unknown":
            raise RuntimeError("CRM delivery completion started from an invalid state")
        owned = session.execute(
            update(CrmOutboxRow)
            .where(*predicates)
            .values(status=outcome, lease_owner="", lease_expires_at="")
            .returning(CrmOutboxRow.id)
        ).scalar_one_or_none()
        if owned is None:
            raise RuntimeError("CRM delivery lease ownership changed before completion")
        session.expire(job)
        if outcome == "confirmed":
            job.confirmed_at = now
            job.last_error = ""
            if job.destination == "contacts":
                try:
                    payload = json.loads(job.payload_json)
                    cells = payload.get("cells", [])
                    fields = dict(zip(
                        (
                            "name", "phone", "email", "date", "business", "source",
                            "language", "want", "status", "summary", "next_step",
                            "created", "updated", "pinged",
                        ),
                        cells[:14],
                        strict=True,
                    ))
                    CrmService(session, now=self._clock).mark_contact_snapshot(
                        job.aggregate_id,
                        fields=fields,
                        revision=int(payload["revision"]),
                    )
                    for resolution_issue_id in self._resolution_issue_ids(payload):
                        issue = session.get(CrmIssueRow, resolution_issue_id)
                        if issue is not None and issue.status == "resolving":
                            issue.status = "resolved"
                            issue.resolved_at = now
                except (KeyError, TypeError, ValueError):
                    job.status = "conflict"
                    job.last_error = "confirmed payload could not be snapshotted"
        elif outcome == "failed":
            delay = min(300, 5 * (2 ** min(job.attempts, 6)))
            job.next_attempt_at = (self._now_dt() + timedelta(seconds=delay)).isoformat()
            job.last_error = "confirmed destination failure"
        elif outcome == "unknown":
            job.last_error = "outcome unknown; readback required before retry"
        else:
            job.last_error = "destination state conflicts with attempted write"
            try:
                payload = json.loads(job.payload_json)
            except (TypeError, json.JSONDecodeError):
                payload = {}
            for resolution_issue_id in self._resolution_issue_ids(payload):
                issue = session.get(CrmIssueRow, resolution_issue_id)
                if issue is not None and issue.status == "resolving":
                    issue.status = "open"
                    issue.resolution = ""
                    issue.resolved_at = ""


def run_worker_forever(worker: CrmDeliveryWorker, stop_event: Event) -> None:
    """Lifespan-friendly public entry point."""
    worker.run_forever(stop_event)


def wait_for_poll(stop_event: Event) -> bool:
    """Tiny seam for deterministic scheduling tests."""
    return stop_event.wait(POLL_SECONDS)
