"""Transaction-scoped manual lead capture behind an exact owner approval."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import UTC, date, datetime
from hashlib import sha256
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.owner import _is_authorized_owner
from app.capabilities.policy import authorize
from app.capabilities.types import Principal
from app.core.config import Settings
from app.core.errors import PermissionDenied
from app.db.models import CrmActivityRow, CrmContactRow, CrmIssueRow, IdempotencyRow
from app.db.store import LeadStore
from app.domain.approvals import DECISION_APPROVED
from app.services.crm_v2 import (
    CRM_PROJECTION_LOCK_KEY,
    WRITER_OWNER,
    ActivityInput,
    CrmRevisionConflict,
    CrmService,
    normalize_email,
    normalize_phone,
)
from app.services.owner_actions import (
    OwnerActionTargetChanged,
    decide_owner_action,
    execute_owner_action,
    propose_owner_action,
    read_owner_action,
)
from app.services.phone_identity import normalize_new_input_phone


class QuickLeadInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: UUID
    name: str = Field(min_length=1, max_length=160)
    phone: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=320)
    business: str = Field(default="", max_length=160)
    need: str = Field(min_length=1, max_length=1000)
    source: Literal["phone_call", "in_person", "whatsapp", "referral", "other"]
    next_action: str = Field(default="", max_length=500)
    follow_up_date: date | None = None

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value: str) -> str:
        if not value:
            return ""
        if not re.fullmatch(r"\+?[0-9 ()-]+", value):
            raise ValueError("invalid phone")
        if not normalize_new_input_phone(value):
            raise ValueError("invalid phone")
        digits = re.sub(r"[ ()-]", "", value)
        if re.fullmatch(r"0[2-9][0-9]{7,8}", digits):
            return "+972" + digits[1:]
        if digits.startswith("+972"):
            if not re.fullmatch(r"\+972[2-9][0-9]{7,8}", digits):
                raise ValueError("invalid phone")
        elif not re.fullmatch(r"\+[1-9][0-9]{7,14}", digits):
            raise ValueError("invalid phone")
        return digits

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if not value:
            return ""
        normalized = normalize_email(value)
        if not normalized or re.search(r"\s", normalized):
            raise ValueError("invalid email")
        local, domain = normalized.split("@")
        if (
            len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local
            or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
            or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                       for label in domain.split("."))
        ):
            raise ValueError("invalid email")
        return normalized

    @model_validator(mode="after")
    def has_identity(self):
        if not self.phone and not self.email:
            raise ValueError("phone or email is required")
        return self

    def crm_fields(self) -> dict[str, str]:
        return {
            "name": self.name, "phone": self.phone, "email": self.email,
            "business": self.business, "want": self.need, "source": self.source,
            "status": "lead", "next_step": self.next_action,
            "date": self.follow_up_date.isoformat() if self.follow_up_date else "",
        }


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _LocalApprovalStore(LeadStore):
    """Reuse approval checks without the external-provider adapter's early commits.

    Only DB effects run here. The HTTP boundary commits the claim, contact, activity
    and approval execution receipt together; generic external writes keep their
    existing durable-before-provider behaviour.
    """

    def _claim_row(self, scope: str, key: str):
        return self.session.scalar(select(IdempotencyRow).where(
            IdempotencyRow.scope == scope, IdempotencyRow.key == key
        ))

    def claim_provider_write(self, *, scope: str, key: str) -> bool:
        if scope != "approval" or not key or self._claim_row(scope, key) is not None:
            return False
        self.session.add(IdempotencyRow(
            scope=scope, key=key, created_at=datetime.now(UTC).isoformat(),
            status="provider_claimed", expires_at="", result_json="{}",
        ))
        self.session.flush()
        return True

    def complete_provider_write(self, *, scope: str, key: str, result_json: str = "{}") -> bool:
        row = self._claim_row(scope, key)
        if scope != "approval" or row is None or row.status != "provider_claimed":
            return False
        row.status = "completed"
        row.result_json = result_json
        self.session.flush()
        return True

    def mark_provider_write_pending_review(self, *, scope: str, key: str) -> None:
        row = self._claim_row(scope, key)
        if scope == "approval" and row is not None:
            row.status = "pending_review"
            self.session.flush()

    def fail_operation(self, *, scope: str, key: str) -> None:
        row = self._claim_row(scope, key)
        if scope == "approval" and row is not None:
            row.status = "failed"
            self.session.flush()


class QuickLeadService:
    """Caller supplies a fresh session and controls commit/rollback."""

    def __init__(self, session: Session, *, settings: Settings, principal: Principal):
        self.session = session
        self.settings = settings
        self.principal = principal
        self.store = _LocalApprovalStore(session)
        self.crm = CrmService(
            session, timezone=settings.calendar_timezone,
            normalize_israeli_phones=settings.crm_israeli_phone_normalization_enabled,
        )

    def _lock(self) -> None:
        try:
            authorize("leads.get_recent", principal=self.principal)
        except PermissionDenied:
            raise PermissionError("owner write denied") from None
        if (
            self.principal.source != "telegram"
            or not _is_authorized_owner(
                actor_id=self.principal.actor_id,
                owner_ids=self.settings.telegram_owner_user_id_set(),
            ) or self.settings.kill_switch
        ):
            raise PermissionError("owner write denied")
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                                 {"key": CRM_PROJECTION_LOCK_KEY})
        elif dialect == "sqlite":
            if self.session.in_transaction():
                raise ValueError("Quick Lead requires a fresh transaction")
            self.session.execute(text("BEGIN IMMEDIATE"))
        else:
            raise ValueError("unsupported CRM database")

    def _source_ref(self, data: QuickLeadInput) -> str:
        return f"quick-lead:{self.principal.actor_id}:{data.request_id}"

    def _matches(self, data: QuickLeadInput) -> dict:
        queries = [value for value in (data.phone, data.email) if value]
        if data.phone.startswith("+"):
            # Legacy CRM accepts country-code digits and 00 prefixes without '+'.
            queries.extend((data.phone[1:], "00" + data.phone[1:]))
        if data.phone.startswith("+972"):
            queries.append("0" + data.phone[4:])
        # lookup falls back to text search for an invalid identity. Never let an
        # oversized legacy alias trigger that fuzzy fallback.
        return {item.id: item for query in queries
                if query == data.email or normalize_phone(query)
                for item in self.crm.lookup(query=query)}

    def _target(self, data: QuickLeadInput) -> dict:
        matches = self._matches(data)
        if len(matches) > 1:
            # A different target shape invalidates the approved absence snapshot.
            return {"ambiguous_contact_ids": sorted(matches)}
        if matches:
            return asdict(self.crm.snapshot_target(next(iter(matches))))
        return asdict(self.crm.snapshot_identity(data.crm_fields()))

    def _receipt(self, key: str, digest: str, response: dict, payload: dict) -> dict:
        self.session.add(IdempotencyRow(
            scope="quick_lead", key=key, created_at=datetime.now(UTC).isoformat(),
            status="completed", expires_at="",
            result_json=_json({"payload_hash": digest, "response": response, "lead": payload}),
        ))
        self.session.flush()
        return response

    def propose(self, data: QuickLeadInput) -> dict:
        self._lock()
        payload = data.model_dump(mode="json")
        digest = sha256(_json(payload).encode()).hexdigest()
        key = sha256(self._source_ref(data).encode()).hexdigest()
        prior = self.session.scalar(select(IdempotencyRow).where(
            IdempotencyRow.scope == "quick_lead", IdempotencyRow.key == key
        ))
        if prior is not None:
            receipt = json.loads(prior.result_json)
            return (receipt["response"] if receipt["payload_hash"] == digest
                    else {"status": "idempotency_conflict"})
        matches = self._matches(data)
        if len(matches) > 1:
            # Public capture cannot express two legacy phone aliases. Reuse CRM's
            # issue persistence, linkage and projection pause, with all matched IDs.
            identities = {key: value for key, value in
                          (("phone", data.phone), ("email", data.email)) if value}
            issue_id = self.crm._identity_issue(set(matches), identities, self._source_ref(data))
            issue = self.session.get(CrmIssueRow, issue_id)
            details = json.loads(issue.details_json)
            details["manual_lead"] = payload
            issue.details_json = _json(details)
            return self._receipt(key, digest, {
                "status": "identity_conflict", "contact_ids": sorted(matches),
                "issue_ids": [issue_id], "preview": payload,
            }, payload)
        if matches:
            contact = next(iter(matches.values()))
            return self._receipt(key, digest, {
                "status": "duplicate", "contact_id": contact.id, "revision": contact.revision,
                "preview": payload,
            }, payload)
        proposal = propose_owner_action(
            self.store, principal=self.principal, source_ref=self._source_ref(data),
            kind="crm.quick_lead", parameters={"lead": payload}, target=self._target(data),
        )
        return self._receipt(key, digest, {
            "status": "pending_approval", "approval_id": proposal.approval_id,
            "proposal_id": proposal.proposal_id, "expires_at": proposal.expires_at,
            "preview": {k: v for k, v in payload.items() if k != "request_id"},
        }, payload)

    def _created_receipt(self, data: QuickLeadInput) -> dict:
        activity = self.session.scalar(select(CrmActivityRow).where(
            CrmActivityRow.source_ref == self._source_ref(data),
            CrmActivityRow.action == "manual_lead_captured",
        ))
        contact = self.session.get(CrmContactRow, activity.contact_id) if activity else None
        if contact is None:
            return {"status": "unknown"}
        return {"status": "created", "contact_id": contact.id, "revision": contact.revision}

    def approve(self, approval_id: str) -> dict:
        self._lock()
        row = self.store.get_approval_by_approval_id(approval_id)
        envelope = read_owner_action(row)
        if (
            row is None or envelope is None or row.actor_id != self.principal.actor_id
            or envelope["kind"] != "crm.quick_lead"
            or set(envelope["parameters"]) != {"lead"}
        ):
            return {"status": "unbound"}
        try:
            data = QuickLeadInput.model_validate(envelope["parameters"]["lead"])
        except ValueError:
            return {"status": "schema_changed"}
        if row.executed_at:
            return self._created_receipt(data)
        decision = decide_owner_action(
            self.store, principal=self.principal, approval_id=approval_id,
            decision=DECISION_APPROVED,
        )
        if decision.status not in {"decided", "already_decided"}:
            return {"status": decision.status}

        def run(_kind, _parameters):
            # Roll back *all* CRM effects if capture/Activity/outbox fails, even if
            # the generic executor catches the exception and records uncertainty.
            with self.session.begin_nested():
                try:
                    captured = self.crm.capture(
                        data.crm_fields(), writer=WRITER_OWNER,
                        source_ref=self._source_ref(data), expected_revision=0,
                        activity=ActivityInput(
                            who="הבעלים", channel=data.source, action="manual_lead_captured",
                            result=data.need, source_ref=self._source_ref(data),
                        ),
                    )
                except CrmRevisionConflict as exc:
                    raise OwnerActionTargetChanged("contact changed") from exc
                if captured.status != "created" or captured.contact is None:
                    raise OwnerActionTargetChanged("identity changed")
                return "CRM lead created: " + captured.contact.id

        outcome = execute_owner_action(
            self.store, principal=self.principal, proposal_id=row.resource_id,
            validate_schema=lambda kind, params: kind == "crm.quick_lead"
            and params == {"lead": data.model_dump(mode="json")},
            validate_policy=lambda kind, _params: kind == "crm.quick_lead"
            and not self.settings.kill_switch,
            connection_ready=lambda _kind: True,
            current_target=lambda _kind, _params: self._target(data),
            execute=run,
        )
        return (self._created_receipt(data) if outcome.status == "executed"
                else {"status": outcome.status})
