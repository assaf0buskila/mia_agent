"""Atomic, replay-safe AssafWeb form intake into Mia's canonical CRM."""

from __future__ import annotations

import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import CrmFormIntakeReceiptRow
from app.services.crm_v2 import CrmPhoneInputInvalid, CrmPhoneNormalizationRequired, CrmService


@dataclass(frozen=True)
class FormIntakeResult:
    status: Literal["captured", "replayed"]
    contact_id: str
    activity_id: str


class FormIntakePayloadConflict(ValueError):
    """A source id was already consumed by a different normalized payload."""


class FormIntakeCaptureConflict(RuntimeError):
    """CRM identity resolution did not produce one contact and Activity."""


class FormIntakeInvalidPhone(ValueError):
    """A new phone failed the enabled input policy; no receipt was committed."""


class FormIntakeService:
    """Transaction-scoped intake adapter; the request dependency owns commit/rollback."""

    def __init__(self, session: Session, *, timezone: str = "Asia/Jerusalem") -> None:
        self.session = session
        self.crm = CrmService(session, timezone=timezone)

    def capture(
        self,
        *,
        source_id: str,
        payload_sha256: str,
        submitted_at: str,
        fields: Mapping[str, str],
        summary: str,
        recipient_ids: Sequence[str],
    ) -> FormIntakeResult:
        # Production is PostgreSQL.  Serialize the check/capture/receipt sequence so
        # two first deliveries of the same event cannot both reach CRM before the PK
        # becomes visible.  SQLite remains deterministic for isolated unit tests.
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"crm:form-intake:{source_id}"},
            )

        existing = self.session.scalars(
            select(CrmFormIntakeReceiptRow).where(
                CrmFormIntakeReceiptRow.source_id == source_id
            )
        ).one_or_none()
        if existing is not None:
            if not hmac.compare_digest(existing.payload_sha256, payload_sha256):
                raise FormIntakePayloadConflict("source_id payload conflict")
            return FormIntakeResult(
                status="replayed",
                contact_id=existing.contact_id,
                activity_id=existing.activity_id,
            )

        try:
            captured = self.crm.capture_form_lead(
                fields,
                source_id=source_id,
                submitted_at=submitted_at,
                summary=summary,
                recipient_ids=recipient_ids,
            )
        except CrmPhoneInputInvalid as exc:
            raise FormIntakeInvalidPhone("invalid phone") from exc
        except CrmPhoneNormalizationRequired as exc:
            raise FormIntakeCaptureConflict(
                "phone normalization requires reviewed identity migration"
            ) from exc
        if captured.contact is None or captured.activity is None or captured.issue_ids:
            raise FormIntakeCaptureConflict("CRM capture did not resolve one contact")

        receipt = CrmFormIntakeReceiptRow(
            source_id=source_id,
            payload_sha256=payload_sha256,
            contact_id=captured.contact.id,
            activity_id=captured.activity.id,
            created_at=datetime.now(UTC).isoformat(),
        )
        self.session.add(receipt)
        self.session.flush()
        return FormIntakeResult(
            status="captured",
            contact_id=receipt.contact_id,
            activity_id=receipt.activity_id,
        )
