"""Owner-only manual capture contract for a future mobile frontend.

No model or provider adapters participate. Telegram's signed login proof supplies
identity; an exact stored approval supplies write authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.owner import _is_authorized_owner
from app.capabilities.types import Principal
from app.core.config import MiaEnv, Settings, get_settings
from app.services.quick_leads import QuickLeadInput, QuickLeadService

router = APIRouter(prefix="/v1/owner/quick-leads", tags=["owner-quick-leads"])
LOGIN_MAX_AGE_SECONDS = 600
_LOGIN_FIELDS = frozenset(
    {"id", "first_name", "last_name", "username", "photo_url", "auth_date", "hash"}
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def verify_owner_login(proof: str, settings: Settings, *, now: int | None = None) -> Principal:
    """Verify Telegram legacy Login Widget HMAC, freshness and numeric allowlist.

    The proof is a short-lived bearer credential. Never put it in a URL or log it.
    A username is display data and contributes no owner authority.
    """
    denied = HTTPException(status_code=401, detail="owner_auth_required")
    if not settings.telegram_bot_token or not proof or len(proof) > 4096:
        raise denied
    try:
        data = json.loads(proof, object_pairs_hook=_unique_object)
        if not isinstance(data, dict) or not set(data) <= _LOGIN_FIELDS:
            raise ValueError("invalid fields")
        if not {"id", "auth_date", "hash"} <= set(data):
            raise ValueError("missing fields")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (str, int))
            or any(ord(char) < 32 for char in str(value))
            for value in data.values()
        ):
            raise ValueError("invalid values")
        actor_id = str(data["id"])
        auth_date = str(data["auth_date"])
        if not auth_date.isascii() or not auth_date.isdigit():
            raise ValueError("invalid timestamp")
        if not _is_authorized_owner(
            actor_id=actor_id, owner_ids=settings.telegram_owner_user_id_set()
        ):
            raise ValueError("not owner")
        moment = int(time.time()) if now is None else now
        age = moment - int(auth_date)
        if age < -30 or age > LOGIN_MAX_AGE_SECONDS:
            raise ValueError("stale proof")
        supplied_hash = str(data.pop("hash"))
        check_string = "\n".join(f"{key}={data[key]}" for key in sorted(data))
        secret = hashlib.sha256(settings.telegram_bot_token.encode()).digest()
        expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied_hash):
            raise ValueError("invalid signature")
    except (ValueError, TypeError, OverflowError):
        raise denied from None
    return Principal.owner(source="telegram", actor_id=actor_id)


def authenticated_owner(
    request: Request,
    response: Response,
    proof: Annotated[str | None, Header(alias="X-AssafWeb-Owner-Login")] = None,
    settings: Settings = Depends(get_settings),
) -> Principal:
    response.headers["Cache-Control"] = "no-store"
    # Require an explicit approved browser origin as well as cryptographic identity.
    # Custom credential header means a cross-origin form cannot invoke this API.
    origin = request.headers.get("origin", "")
    if (
        not origin
        or origin == "null"
        or origin not in settings.cors_origin_list()
        or (settings.env == MiaEnv.PROD and urlsplit(origin).scheme != "https")
    ):
        raise HTTPException(status_code=403, detail="origin_denied")
    return verify_owner_login(proof or "", settings)


def _persist(db: Session, result: dict, response: Response) -> dict:
    # A successful dependency teardown is too late to be the persistence receipt.
    # Commit explicitly before returning any pending/created/conflict confirmation.
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=503, detail="persistence_failed") from None
    if result["status"] == "pending_approval":
        response.status_code = 202
    elif result["status"] not in {"created", "rejected"}:
        response.status_code = 409
    return result


@router.post("/proposals")
def propose_quick_lead(
    data: QuickLeadInput,
    response: Response,
    principal: Principal = Depends(authenticated_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    service = QuickLeadService(db, settings=settings, principal=principal)
    try:
        result = service.propose(data)
    except PermissionError:
        raise HTTPException(status_code=403, detail="owner_write_denied") from None
    return _persist(db, result, response)


@router.post("/approvals/{approval_id}")
def approve_quick_lead(
    approval_id: str,
    response: Response,
    principal: Principal = Depends(authenticated_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    service = QuickLeadService(db, settings=settings, principal=principal)
    try:
        result = service.approve(approval_id)
    except PermissionError:
        raise HTTPException(status_code=403, detail="owner_write_denied") from None
    return _persist(db, result, response)
