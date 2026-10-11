"""Prepared, default-off adapters over canonical owner actions; never sends."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from hashlib import sha256
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.owner_sessions import OwnerSessionPrincipal, owner_mutation_scope, owner_scope
from app.api.quick_leads import _persist
from app.capabilities.types import Principal
from app.core.config import Settings, get_settings
from app.db.models import ApprovalRow, CrmActivityRow, CrmContactRow, IdempotencyRow
from app.db.session import sqlalchemy_database_url
from app.domain.approvals import APPROVAL_ID_RE
from app.services.owner_os_action_policy import validate_action_role
from app.services.owner_os_reads import read_snapshot
from app.services.quick_leads import QuickLeadInput, QuickLeadService
from app.services.reminders import (
    ReminderConflict,
    ReminderContactAmbiguous,
    ReminderNotFound,
    ReminderService,
    ReminderValidationError,
)

router = APIRouter(prefix="/v1/owner/os", tags=["owner-os-prepared-actions"])


@lru_cache(maxsize=4)
def _action_engine(url: str):
    return create_engine(sqlalchemy_database_url(url), connect_args={"connect_timeout": 5},
                         pool_timeout=5)


def _enabled(settings: Settings) -> None:
    if not settings.owner_os_enabled or not settings.owner_os_actions_enabled:
        raise HTTPException(503, "operation_unavailable")
    if settings.kill_switch:
        raise HTTPException(403, "forbidden")


@contextmanager
def action_session(settings: Settings):
    _enabled(settings)
    url = settings.owner_os_action_database_url
    if not url.startswith(("postgresql://", "postgresql+psycopg://", "postgres://")):
        raise HTTPException(503, "operation_unavailable")
    try:
        # Canonical Quick Lead conflict helpers expect pending issue links to flush
        # before repeated lookups. This is an explicit action transaction; reads use
        # their separate SELECT-only, autoflush-disabled snapshot.
        with Session(_action_engine(url), autoflush=True) as db:
            try:
                validate_action_role(db)
            except ValueError:
                raise HTTPException(503, "operation_unavailable")
            # Quick Lead requires a fresh transaction when it obtains its own lock.
            db.rollback()
            db.execute(text("SET LOCAL statement_timeout = 5000"))
            db.execute(text("SET LOCAL lock_timeout = 5000"))
            try:
                yield db
            finally:
                db.rollback()
    except SQLAlchemyError:
        raise HTTPException(503, "persistence_failed") from None


def _principal(owner: OwnerSessionPrincipal) -> Principal:
    return Principal.owner(source="telegram", actor_id=owner.owner_id)


@router.post("/quick-leads/proposals")
def quick_lead_proposal(
    data: QuickLeadInput, response: Response,
    owner: OwnerSessionPrincipal = Depends(
        owner_mutation_scope("quick_leads.create", require_fresh_login=True)
    ),
    settings: Settings = Depends(get_settings),
) -> dict:
    with action_session(settings) as db:
        result = QuickLeadService(db, settings=settings, principal=_principal(owner)).propose(data)
        return _persist(db, result, response)


@router.post("/quick-leads/approvals/{approval_id}")
def quick_lead_approval(
    approval_id: str, response: Response,
    owner: OwnerSessionPrincipal = Depends(
        owner_mutation_scope("quick_leads.create", require_fresh_login=True)
    ),
    settings: Settings = Depends(get_settings),
) -> dict:
    if APPROVAL_ID_RE.fullmatch(approval_id) is None:
        raise HTTPException(400, "invalid_query")
    with action_session(settings) as db:
        result = QuickLeadService(db, settings=settings, principal=_principal(owner)).approve(
            approval_id
        )
        return _persist(db, result, response)


@router.get("/quick-leads/receipts/{request_id}")
def quick_lead_receipt(
    request_id: UUID,
    owner: OwnerSessionPrincipal = Depends(owner_scope("quick_leads.create")),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Recover committed outcome after lost response/session, without another write."""
    _enabled(settings)
    source_ref = f"quick-lead:{owner.owner_id}:{request_id}"
    key = sha256(source_ref.encode()).hexdigest()
    with read_snapshot(settings) as db:
        stored = db.scalar(select(IdempotencyRow).where(
            IdempotencyRow.scope == "quick_lead", IdempotencyRow.key == key
        ))
        if stored is None:
            raise HTTPException(404, "record_not_found")
        try:
            receipt = json.loads(stored.result_json)["response"]
            status = receipt["status"]
            if status == "pending_approval":
                approval = db.scalar(select(ApprovalRow).where(
                    ApprovalRow.approval_id == receipt["approval_id"],
                    ApprovalRow.actor_id == owner.owner_id,
                ))
                if approval is None:
                    return {"status": "unknown"}
                if approval.executed_at:
                    activity = db.scalar(select(CrmActivityRow).where(
                        CrmActivityRow.source_ref == source_ref,
                        CrmActivityRow.action == "manual_lead_captured",
                    ))
                    contact = db.get(CrmContactRow, activity.contact_id) if activity else None
                    if contact is None:
                        return {"status": "unknown"}
                    return {"status": "created", "contact_id": contact.id,
                            "revision": contact.revision}
                if datetime.fromisoformat(approval.expires_at) <= datetime.now(UTC):
                    return {"status": "expired"}
            if status not in {"pending_approval", "duplicate", "identity_conflict"}:
                return {"status": "unknown"}
            return {k: receipt[k] for k in (
                "status", "approval_id", "proposal_id", "expires_at", "contact_id",
                "revision", "contact_ids", "issue_ids",
            ) if k in receipt}
        except (ValueError, TypeError, KeyError):
            raise HTTPException(503, "source_unavailable") from None


class ReminderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID
    text: str = Field(min_length=1, max_length=500)
    scheduled_at: AwareDatetime
    contact_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[\w-]+$")


class ReminderChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)
    scheduled_at: AwareDatetime | None = None


def _reminder(view) -> dict:
    return {"reminder_id": view.id, "revision": view.revision, "text": view.text,
            "scheduled_at": view.scheduled_at.isoformat(), "timezone": view.timezone,
            "contact_id": view.contact_id, "status": view.status,
            "delivery_state": view.delivery_state}


def _reminder_error(exc: Exception) -> None:
    if isinstance(exc, ReminderNotFound):
        raise HTTPException(404, "record_not_found") from None
    if isinstance(exc, (ReminderConflict, ReminderContactAmbiguous)):
        raise HTTPException(409, "conflict") from None
    if isinstance(exc, ReminderValidationError):
        raise HTTPException(422, "validation_error") from None
    raise exc


@router.post("/reminders")
def reminder_create(
    data: ReminderCreate,
    owner: OwnerSessionPrincipal = Depends(owner_mutation_scope("reminders.write")),
    settings: Settings = Depends(get_settings),
) -> dict:
    with action_session(settings) as db:
        if data.contact_id and db.get(CrmContactRow, data.contact_id) is None:
            raise HTTPException(404, "record_not_found")
        try:
            result = ReminderService(db).create(
                owner_id=owner.owner_id, source_event_id=f"owner-os:{data.request_id}",
                text=data.text, scheduled_at=data.scheduled_at,
                contact_query=data.contact_id or "", explicit_unlinked=data.contact_id is None,
            )
            # Existing service commits and returns its durable row. Different-payload
            # retries must not silently claim a new request was applied.
            r = result.reminder
            if (r.text, r.scheduled_at, r.contact_id) != (
                data.text, data.scheduled_at.astimezone(UTC), data.contact_id
            ):
                raise HTTPException(409, "idempotency_conflict")
            return {"status": "created" if result.created else "already_handled",
                    **_reminder(r)}
        except (ReminderValidationError, ReminderConflict, ReminderContactAmbiguous) as exc:
            _reminder_error(exc)


@router.post("/reminders/{reminder_id}/{action}")
def reminder_change(
    reminder_id: str, action: str, data: ReminderChange,
    owner: OwnerSessionPrincipal = Depends(owner_mutation_scope("reminders.write")),
    settings: Settings = Depends(get_settings),
) -> dict:
    if action not in {"complete", "snooze", "reschedule"}:
        raise HTTPException(400, "invalid_query")
    if len(reminder_id) != 32 or not all(c in "0123456789abcdef" for c in reminder_id):
        raise HTTPException(400, "invalid_query")
    with action_session(settings) as db:
        try:
            result = ReminderService(db).change(
                owner_id=owner.owner_id, reminder_id=reminder_id, action=action,
                expected_revision=data.expected_revision, scheduled_at=data.scheduled_at,
            )
            return {"status": "committed", **_reminder(result.reminder)}
        except (ReminderValidationError, ReminderConflict, ReminderNotFound) as exc:
            _reminder_error(exc)
