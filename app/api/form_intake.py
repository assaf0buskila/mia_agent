"""Private AssafWeb form ingress.

This route authenticates and bounds the raw request before JSON parsing.  It maps a
validated form event directly to CRM; it never enters Mia's public conversation or
model paths.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import get_settings
from app.services.form_intake import (
    FormIntakeCaptureConflict,
    FormIntakeInvalidPhone,
    FormIntakePayloadConflict,
    FormIntakeService,
)

router = APIRouter(prefix="/v1/internal/assafweb", tags=["internal"])

_SECRET_HEADER = "X-AssafWeb-Intake-Secret"
_MAX_BODY_BYTES = 16_384
_WANT = {
    "learning": "Learn",
    "business": "פתרון AI לעסק",
    "unsure": "לא בטוח/ה",
}
_NEXT_STEP = {
    "learning": "לחזור לפונה לגבי מסלול הלמידה",
    "business": "לחזור לפונה לשיחת אפיון פתרון לעסק",
    "unsure": "לחזור לפונה ולברר איזה מסלול מתאים",
}


class FormAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    utm_source: str = Field(default="", max_length=200)
    utm_medium: str = Field(default="", max_length=200)
    utm_campaign: str = Field(default="", max_length=200)
    utm_content: str = Field(default="", max_length=200)
    utm_term: str = Field(default="", max_length=200)


class AssafWebFormLead(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_id: UUID
    submitted_at: AwareDatetime
    interest: Literal["learning", "business", "unsure"]
    form: Literal["learn", "learn_hero", "home", "sherut"]
    submission_type: Literal["production", "test", "internal"]
    name: str = Field(default="", max_length=80)
    phone_e164: str = Field(default="", pattern=r"^(?:|\+[1-9][0-9]{7,14})$", max_length=16)
    email: str = Field(default="", max_length=254)
    business: str = Field(default="", max_length=200)
    note: str = Field(default="", max_length=2_000)
    page: str = Field(default="", max_length=180)
    attribution: FormAttribution = Field(default_factory=FormAttribution)

    @field_validator("submitted_at")
    @classmethod
    def normalize_submitted_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def require_identity(self) -> AssafWebFormLead:
        if not self.phone_e164 and not self.email:
            raise ValueError("phone_e164 or email is required")
        if self.email and ("@" not in self.email or self.email.startswith("@")):
            raise ValueError("email is invalid")
        return self


class FormIntakeResponse(BaseModel):
    status: Literal["captured", "replayed", "ignored"]
    contact_id: str | None = None
    activity_id: str | None = None


def _authenticate(request: Request) -> None:
    configured = get_settings().assafweb_form_intake_secret
    if not configured.strip():
        raise HTTPException(status_code=503, detail="form intake is not configured")
    supplied = request.headers.get(_SECRET_HEADER, "")
    try:
        expected_digest = hashlib.sha256(configured.encode("utf-8")).digest()
        supplied_digest = hashlib.sha256(supplied.encode("utf-8")).digest()
        authenticated = hmac.compare_digest(expected_digest, supplied_digest)
    except (AttributeError, TypeError, UnicodeError, ValueError):
        authenticated = False
    if not authenticated:
        raise HTTPException(status_code=401, detail="unauthorized")


async def _bounded_json(request: Request) -> bytes:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="application/json required")
    raw_length = request.headers.get("content-length", "")
    if raw_length:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content length") from exc
        if content_length < 0 or content_length > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request body too large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request body too large")
    return bytes(body)


def _canonical_payload_hash(lead: AssafWebFormLead) -> str:
    normalized = lead.model_dump(mode="json")
    normalized["source_id"] = str(lead.source_id)
    normalized["submitted_at"] = lead.submitted_at.isoformat()
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _summary(lead: AssafWebFormLead) -> str:
    attribution = lead.attribution
    attribution_parts = [
        f"source={attribution.utm_source}" if attribution.utm_source else "",
        f"medium={attribution.utm_medium}" if attribution.utm_medium else "",
        f"campaign={attribution.utm_campaign}" if attribution.utm_campaign else "",
        f"content={attribution.utm_content}" if attribution.utm_content else "",
        f"term={attribution.utm_term}" if attribution.utm_term else "",
    ]
    attribution_line = " | ".join(part for part in attribution_parts if part) or "direct"
    lines = [
        "ליד מטופס AssafWeb",
        f"מסלול: {_WANT[lead.interest]}",
        f"טופס: {lead.form}",
        f"שם: {lead.name}" if lead.name else "",
        f"טלפון: {lead.phone_e164}" if lead.phone_e164 else "",
        f"אימייל: {lead.email}" if lead.email else "",
        f"עסק: {lead.business}" if lead.business else "",
        f"עמוד: {lead.page}" if lead.page else "",
        f"ייחוס: {attribution_line}",
        f"צעד הבא: {_NEXT_STEP[lead.interest]}",
        # Free text is last so bounded truncation can never discard form attribution
        # or the deterministic next step.
        f"הערה: {lead.note}" if lead.note else "",
    ]
    return "\n".join(line for line in lines if line)[:2_000]


@router.post(
    "/form-leads",
    response_model=FormIntakeResponse,
    response_model_exclude_none=True,
)
async def capture_form_lead(
    request: Request,
    db: Session = Depends(get_db, scope="function"),
) -> FormIntakeResponse | JSONResponse:
    _authenticate(request)
    body = await _bounded_json(request)
    try:
        lead = AssafWebFormLead.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid form lead") from exc

    if lead.submission_type != "production":
        return FormIntakeResponse(status="ignored")

    payload_hash = _canonical_payload_hash(lead)
    summary = _summary(lead)
    settings = get_settings()
    fields = {
        "name": lead.name,
        "phone": lead.phone_e164,
        "email": lead.email,
        "date": lead.submitted_at.isoformat(),
        "business": lead.business,
        "source": f"AssafWeb form / {lead.form}",
        "language": "he",
        "want": _WANT[lead.interest],
        "status": "new",
        "summary": lead.note,
        "next_step": _NEXT_STEP[lead.interest],
    }
    try:
        result = FormIntakeService(db, timezone=settings.calendar_timezone).capture(
            source_id=str(lead.source_id),
            payload_sha256=payload_hash,
            submitted_at=lead.submitted_at.isoformat(),
            fields=fields,
            summary=summary,
            recipient_ids=tuple(sorted(settings.telegram_owner_user_id_set())),
        )
    except FormIntakePayloadConflict:
        db.rollback()
        return JSONResponse(status_code=409, content={"detail": "source_id payload conflict"})
    except FormIntakeCaptureConflict:
        db.rollback()
        return JSONResponse(status_code=409, content={"detail": "CRM identity conflict"})
    except FormIntakeInvalidPhone:
        db.rollback()
        return JSONResponse(status_code=422, content={"detail": "invalid form lead"})

    return FormIntakeResponse(
        status=result.status,
        contact_id=result.contact_id,
        activity_id=result.activity_id,
    )


@router.post("/form-leads/", include_in_schema=False)
async def reject_trailing_slash() -> JSONResponse:
    """Keep the shared-secret request on one exact URL; never redirect it."""
    return JSONResponse(status_code=404, content={"detail": "not found"})
