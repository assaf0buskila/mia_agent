"""Browser owner authentication backed by short-lived opaque PostgreSQL sessions."""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.api.quick_leads import verify_owner_login
from app.core.config import Settings, get_settings
from app.db.session import sqlalchemy_database_url
from app.services.owner_sessions import (
    MAX_SESSION_TTL_SECONDS,
    OwnerSessionDenied,
    OwnerSessionRateLimited,
    OwnerSessionRecord,
    OwnerSessionReplay,
    OwnerSessionStore,
    login_proof_hash,
    session_csrf_token,
    valid_session_csrf,
)

router = APIRouter(prefix="/v1/owner/auth", tags=["owner-auth"])

SESSION_COOKIE = "__Host-mia-owner"
CHALLENGE_COOKIE = "__Host-mia-owner-challenge"
CSRF_HEADER = "X-CSRF-Token"
LOGIN_PROOF_HEADER = "X-AssafWeb-Owner-Login"
MAX_BODY_BYTES = 8192
CHALLENGE_TTL_SECONDS = 600
CHALLENGE_RATE_LIMIT = 30
LOGIN_RATE_LIMIT = 20
AUTH_RATE_WINDOW_SECONDS = 60

READ_SCOPES = frozenset(
    {
        "owner-os:read:contacts",
        "owner-os:read:activities",
        "owner-os:read:reminders",
    }
)
ACTION_SCOPES = frozenset({"quick_leads.create", "reminders.write"})
KNOWN_SCOPES = READ_SCOPES | ACTION_SCOPES

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OwnerSessionPrincipal:
    owner_id: str
    scopes: frozenset[str]
    session_hash: str


class OwnerLoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    login_proof: str = Field(min_length=1, max_length=4096, repr=False)


class OwnerSessionView(BaseModel):
    authenticated: bool = True
    scopes: list[str]
    expires_at: datetime
    csrf_token: str = Field(repr=False)


def _error(status_code: int, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Origin, Cookie"


def _feature_settings(settings: Settings) -> tuple[str, str]:
    if not getattr(settings, "owner_os_enabled", False):
        raise _error(404, "not_found")
    origin = str(getattr(settings, "owner_os_origin", "")).strip()
    workspace_id = str(getattr(settings, "owner_os_workspace_id", "")).strip()
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or not workspace_id
    ):
        raise _error(503, "owner_auth_unavailable")
    return origin.rstrip("/"), workspace_id


def _require_origin(request: Request, settings: Settings) -> str:
    expected, _ = _feature_settings(settings)
    supplied = request.headers.get("origin", "")
    if not supplied or supplied == "null" or supplied != expected:
        raise _error(403, "origin_denied")
    return supplied


def _configured_scopes(settings: Settings) -> frozenset[str]:
    raw = str(getattr(settings, "owner_os_session_scopes", ""))
    requested = frozenset(item.strip() for item in raw.split(",") if item.strip())
    granted = requested & KNOWN_SCOPES
    if not getattr(settings, "owner_os_actions_enabled", False):
        granted -= ACTION_SCOPES
    return granted


def _session_ttl(settings: Settings) -> int:
    try:
        configured = int(getattr(settings, "owner_os_session_ttl_seconds", 0))
    except (TypeError, ValueError):
        raise _error(503, "owner_auth_unavailable") from None
    if configured <= 0:
        raise _error(503, "owner_auth_unavailable")
    return min(configured, MAX_SESSION_TTL_SECONDS)


@lru_cache(maxsize=4)
def _session_factory(database_url: str) -> tuple[Engine, sessionmaker[Session]]:
    scheme = database_url.strip().split(":", 1)[0].lower()
    if scheme not in {"postgres", "postgresql", "postgresql+psycopg"}:
        raise ValueError("owner session database must be PostgreSQL")
    engine = create_engine(
        sqlalchemy_database_url(database_url),
        connect_args={"connect_timeout": 5},
        pool_timeout=5,
        pool_pre_ping=True,
    )
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_owner_session_db(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Generator[Session, None, None]:
    _require_origin(request, settings)
    database_url = str(getattr(settings, "owner_os_session_database_url", "")).strip()
    if not database_url:
        raise _error(503, "owner_auth_unavailable")
    try:
        _, factory = _session_factory(database_url)
        db = factory()
    except (SQLAlchemyError, ValueError):
        _logger.warning("owner_auth reason=session_db_unavailable")
        raise _error(503, "owner_auth_unavailable") from None
    try:
        try:
            db.execute(text("SET LOCAL statement_timeout = '5s'"))
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
            _validate_runtime_role(db)
        except HTTPException:
            raise
        except SQLAlchemyError:
            _logger.warning("owner_auth reason=session_role_validation_failed")
            raise _error(503, "owner_auth_unavailable") from None
        yield db
    finally:
        db.close()


def _validate_runtime_role(db: Session) -> None:
    """Fail closed unless the dedicated PostgreSQL role has only session-table rights."""
    role = db.execute(
        text(
            """
            SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication,
                   rolbypassrls, rolinherit
            FROM pg_roles
            WHERE rolname = current_user
            """
        )
    ).first()
    if role is None or any(bool(value) for value in role):
        raise _error(503, "owner_auth_unavailable")
    posture = db.execute(
        text(
            """
            SELECT
              NOT has_database_privilege(current_user, current_database(), 'CREATE'),
              NOT has_database_privilege(current_user, current_database(), 'TEMP'),
              NOT EXISTS (
                SELECT 1 FROM pg_auth_members
                WHERE member = (SELECT oid FROM pg_roles WHERE rolname = current_user)
              ),
              NOT EXISTS (
                SELECT 1 FROM pg_namespace
                WHERE nspname NOT IN ('pg_catalog', 'information_schema')
                  AND nspname NOT LIKE 'pg_toast%'
                  AND nspname NOT LIKE 'pg_temp_%'
                  AND has_schema_privilege(current_user, oid, 'CREATE')
              ),
              NOT EXISTS (
                SELECT 1
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                CROSS JOIN (VALUES
                  ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'),
                  ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
                ) AS p(privilege)
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                  AND n.nspname NOT LIKE 'pg_temp_%'
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND has_table_privilege(current_user, c.oid, p.privilege)
                  AND NOT (
                    n.nspname = current_schema() AND (
                    c.relname = 'owner_os_login_challenges'
                      AND p.privilege IN ('SELECT', 'INSERT', 'UPDATE')
                    OR c.relname = 'owner_os_login_proofs'
                      AND p.privilege = 'INSERT'
                    OR c.relname = 'owner_os_sessions'
                      AND p.privilege IN ('SELECT', 'INSERT', 'UPDATE')
                    OR c.relname = 'owner_os_revocations'
                      AND p.privilege IN ('SELECT', 'INSERT', 'UPDATE')
                    OR c.relname = 'owner_os_auth_attempts'
                      AND p.privilege IN ('SELECT', 'INSERT')
                    )
                  )
              ),
              NOT EXISTS (
                SELECT 1
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = c.oid
                CROSS JOIN (VALUES
                  ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')
                ) AS p(privilege)
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                  AND n.nspname NOT LIKE 'pg_temp_%'
                  AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                  AND has_column_privilege(current_user, c.oid, a.attnum, p.privilege)
                  AND NOT (
                    n.nspname = current_schema() AND (
                    c.relname = 'owner_os_login_challenges'
                      AND p.privilege IN ('SELECT', 'INSERT', 'UPDATE')
                    OR c.relname = 'owner_os_login_proofs'
                      AND p.privilege = 'INSERT'
                    OR c.relname = 'owner_os_sessions'
                      AND p.privilege IN ('SELECT', 'INSERT', 'UPDATE')
                    OR c.relname = 'owner_os_revocations'
                      AND p.privilege IN ('SELECT', 'INSERT', 'UPDATE')
                    OR c.relname = 'owner_os_auth_attempts'
                      AND p.privilege IN ('SELECT', 'INSERT')
                    )
                  )
              ),
              NOT EXISTS (
                SELECT 1
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                CROSS JOIN (VALUES ('USAGE'), ('SELECT'), ('UPDATE')) AS p(privilege)
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                  AND n.nspname NOT LIKE 'pg_temp_%'
                  AND c.relkind = 'S'
                  AND has_sequence_privilege(current_user, c.oid, p.privilege)
                  AND NOT (
                    n.nspname = current_schema()
                    AND
                    c.relname = 'owner_os_auth_attempts_id_seq'
                    AND p.privilege IN ('USAGE', 'SELECT')
                  )
              ),
              NOT EXISTS (
                SELECT 1
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                  AND n.nspname NOT LIKE 'pg_temp_%'
                  AND has_function_privilege(current_user, p.oid, 'EXECUTE')
              ),
              NOT EXISTS (
                SELECT 1 FROM pg_class WHERE relowner = current_user::regrole
                UNION ALL
                SELECT 1 FROM pg_namespace WHERE nspowner = current_user::regrole
                UNION ALL
                SELECT 1 FROM pg_proc WHERE proowner = current_user::regrole
                UNION ALL
                SELECT 1 FROM pg_database WHERE datdba = current_user::regrole
              )
            """
        )
    ).first()
    if posture is None or not all(bool(value) for value in posture):
        raise _error(503, "owner_auth_unavailable")
    required = db.execute(
        text(
            """
            SELECT
              has_table_privilege(current_user, 'owner_os_login_challenges', 'SELECT')
                AND has_table_privilege(current_user, 'owner_os_login_challenges', 'INSERT')
                AND has_table_privilege(current_user, 'owner_os_login_challenges', 'UPDATE'),
              has_table_privilege(current_user, 'owner_os_login_proofs', 'INSERT'),
              has_table_privilege(current_user, 'owner_os_sessions', 'SELECT')
                AND has_table_privilege(current_user, 'owner_os_sessions', 'INSERT')
                AND has_table_privilege(current_user, 'owner_os_sessions', 'UPDATE'),
              has_table_privilege(current_user, 'owner_os_revocations', 'SELECT')
                AND has_table_privilege(current_user, 'owner_os_revocations', 'INSERT')
                AND has_table_privilege(current_user, 'owner_os_revocations', 'UPDATE'),
              has_table_privilege(current_user, 'owner_os_auth_attempts', 'SELECT')
                AND has_table_privilege(current_user, 'owner_os_auth_attempts', 'INSERT'),
              has_sequence_privilege(current_user, 'owner_os_auth_attempts_id_seq', 'USAGE')
                AND has_sequence_privilege(
                  current_user, 'owner_os_auth_attempts_id_seq', 'SELECT'
                )
            """
        )
    ).first()
    if required is None or not all(bool(value) for value in required):
        raise _error(503, "owner_auth_unavailable")


def _cookie(response: Response, name: str, value: str, *, max_age: int) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )


def _clear_cookie(response: Response, name: str) -> None:
    response.delete_cookie(
        key=name,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )


def _request_cookie(request: Request, name: str) -> str:
    """Reject duplicate security-cookie names instead of accepting parser ambiguity."""
    values: list[str] = []
    for raw_header in request.headers.getlist("cookie"):
        for part in raw_header.split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key == name:
                values.append(value)
    if len(values) > 1:
        raise _error(400, "invalid_cookie")
    if not values:
        return ""
    value = values[0]
    if not value or len(value) > 256 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise _error(400, "invalid_cookie")
    return value


async def _bounded_login_body(request: Request) -> OwnerLoginBody:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise _error(415, "application_json_required")
    content_length = request.headers.get("content-length", "")
    if content_length:
        try:
            length = int(content_length)
        except ValueError:
            raise _error(400, "invalid_content_length") from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise _error(413, "request_body_too_large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise _error(413, "request_body_too_large")
    try:
        return OwnerLoginBody.model_validate_json(bytes(body))
    except ValidationError:
        raise _error(422, "invalid_login_request") from None


def _proof_fields(login_proof: str) -> tuple[str, datetime]:
    """Extract only fields already authenticated by verify_owner_login."""
    try:
        value: Any = json.loads(login_proof)
        signature = value["hash"]
        auth_date = int(value["auth_date"])
        if not isinstance(signature, str):
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise _error(401, "owner_auth_required") from None
    return login_proof_hash(signature), datetime.fromtimestamp(auth_date, tz=UTC)


def _record_principal(
    record: OwnerSessionRecord,
    *,
    settings: Settings,
    required_scope: str | None,
) -> OwnerSessionPrincipal:
    _, workspace_id = _feature_settings(settings)
    owner_ids = settings.telegram_owner_user_id_set()
    current_scopes = _configured_scopes(settings)
    scopes = record.scopes & current_scopes
    if record.owner_id not in owner_ids or record.workspace_id != workspace_id:
        raise _error(401, "owner_auth_required")
    if required_scope is not None and required_scope not in scopes:
        raise _error(403, "owner_scope_denied")
    return OwnerSessionPrincipal(
        owner_id=record.owner_id,
        scopes=scopes,
        session_hash=record.session_hash,
    )


def require_owner_session(
    request: Request,
    scope: str | None,
    *,
    db: Session,
    settings: Settings,
) -> OwnerSessionPrincipal:
    """Validate origin, opaque session, current owner allowlist/scopes/workspace."""
    _require_origin(request, settings)
    cookie_token = _request_cookie(request, SESSION_COOKIE)
    try:
        record = OwnerSessionStore(db).authenticate(cookie_token)
    except OwnerSessionDenied:
        raise _error(401, "owner_auth_required") from None
    except SQLAlchemyError:
        db.rollback()
        _logger.warning("owner_auth reason=session_lookup_failed")
        raise _error(503, "owner_auth_unavailable") from None
    return _record_principal(record, settings=settings, required_scope=scope)


def owner_scope(scope: str) -> Callable[..., OwnerSessionPrincipal]:
    if scope not in KNOWN_SCOPES:
        raise ValueError("unknown owner scope")

    def dependency(
        request: Request,
        db: Session = Depends(get_owner_session_db),
        settings: Settings = Depends(get_settings),
    ) -> OwnerSessionPrincipal:
        return require_owner_session(request, scope, db=db, settings=settings)

    return dependency


def owner_mutation_scope(
    scope: str,
    *,
    require_fresh_login: bool = False,
) -> Callable[..., OwnerSessionPrincipal]:
    if scope not in ACTION_SCOPES:
        raise ValueError("mutation scope must be a fixed action scope")

    def dependency(
        request: Request,
        csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
        login_proof: Annotated[str | None, Header(alias=LOGIN_PROOF_HEADER)] = None,
        db: Session = Depends(get_owner_session_db),
        settings: Settings = Depends(get_settings),
    ) -> OwnerSessionPrincipal:
        principal = require_owner_session(request, scope, db=db, settings=settings)
        cookie_token = _request_cookie(request, SESSION_COOKIE)
        if not valid_session_csrf(
            cookie_token,
            csrf_token or "",
            secret=settings.telegram_bot_token,
        ):
            raise _error(403, "csrf_denied")
        if require_fresh_login:
            try:
                fresh = verify_owner_login(login_proof or "", settings)
            except HTTPException:
                raise _error(401, "owner_auth_required") from None
            if fresh.actor_id != principal.owner_id:
                raise _error(401, "owner_auth_required")
            supplied_proof_hash, _ = _proof_fields(login_proof or "")
            try:
                session_record = OwnerSessionStore(db).authenticate(cookie_token)
            except OwnerSessionDenied:
                raise _error(401, "owner_auth_required") from None
            if not hmac.compare_digest(
                session_record.login_proof_hash,
                supplied_proof_hash,
            ):
                raise _error(401, "owner_auth_required")
        return principal

    return dependency


@router.get("/challenge")
def issue_challenge(
    request: Request,
    response: Response,
    db: Session = Depends(get_owner_session_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    _require_origin(request, settings)
    _no_store(response)
    try:
        store = OwnerSessionStore(db)
        store.record_auth_attempt(
            "challenge",
            limit=CHALLENGE_RATE_LIMIT,
            window_seconds=AUTH_RATE_WINDOW_SECONDS,
        )
        challenge = store.issue_challenge(ttl_seconds=CHALLENGE_TTL_SECONDS)
        db.commit()
    except OwnerSessionRateLimited:
        db.rollback()
        raise _error(429, "rate_limited") from None
    except SQLAlchemyError:
        db.rollback()
        _logger.warning("owner_auth reason=challenge_persist_failed")
        raise _error(503, "owner_auth_unavailable") from None
    _cookie(response, CHALLENGE_COOKIE, challenge.cookie_token, max_age=CHALLENGE_TTL_SECONDS)
    return {
        "csrf_token": challenge.csrf_token,
        "expires_at": challenge.expires_at.isoformat(),
    }


@router.post("/login", response_model=OwnerSessionView)
async def login(
    request: Request,
    response: Response,
    csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
    db: Session = Depends(get_owner_session_db),
    settings: Settings = Depends(get_settings),
) -> OwnerSessionView:
    _require_origin(request, settings)
    _no_store(response)
    body = await _bounded_login_body(request)
    challenge_cookie = _request_cookie(request, CHALLENGE_COOKIE)
    previous_cookie = _request_cookie(request, SESSION_COOKIE)
    try:
        OwnerSessionStore(db).record_auth_attempt(
            "login",
            limit=LOGIN_RATE_LIMIT,
            window_seconds=AUTH_RATE_WINDOW_SECONDS,
        )
        # Persist even rejected proof attempts so invalid HMACs cannot evade the budget.
        db.commit()
    except OwnerSessionRateLimited:
        db.rollback()
        raise _error(429, "rate_limited") from None
    except SQLAlchemyError:
        db.rollback()
        _logger.warning("owner_auth reason=login_rate_persist_failed")
        raise _error(503, "owner_auth_unavailable") from None
    try:
        verified = verify_owner_login(body.login_proof, settings)
    except HTTPException:
        raise _error(401, "owner_auth_required") from None
    proof_hash, proof_authenticated_at = _proof_fields(body.login_proof)
    scopes = _configured_scopes(settings)
    _, workspace_id = _feature_settings(settings)
    if not scopes:
        raise _error(503, "owner_auth_unavailable")
    try:
        issued = OwnerSessionStore(db).create_session(
            owner_id=verified.actor_id,
            workspace_id=workspace_id,
            scopes=scopes,
            proof_hash=proof_hash,
            proof_authenticated_at=proof_authenticated_at,
            challenge_cookie=challenge_cookie,
            challenge_csrf=csrf_token or "",
            previous_cookie=previous_cookie,
            ttl_seconds=_session_ttl(settings),
        )
        db.commit()
    except (OwnerSessionDenied, OwnerSessionReplay):
        db.rollback()
        raise _error(401, "owner_auth_required") from None
    except SQLAlchemyError:
        db.rollback()
        _logger.warning("owner_auth reason=login_persist_failed")
        raise _error(503, "owner_auth_unavailable") from None

    ttl = _session_ttl(settings)
    _cookie(response, SESSION_COOKIE, issued.cookie_token, max_age=ttl)
    _clear_cookie(response, CHALLENGE_COOKIE)
    return OwnerSessionView(
        scopes=sorted(issued.record.scopes),
        expires_at=issued.record.expires_at,
        csrf_token=session_csrf_token(
            issued.cookie_token,
            secret=settings.telegram_bot_token,
        ),
    )


@router.get("/session", response_model=OwnerSessionView)
def session_status(
    request: Request,
    response: Response,
    db: Session = Depends(get_owner_session_db),
    settings: Settings = Depends(get_settings),
) -> OwnerSessionView:
    _no_store(response)
    principal = require_owner_session(request, None, db=db, settings=settings)
    cookie_token = _request_cookie(request, SESSION_COOKIE)
    try:
        record = OwnerSessionStore(db).authenticate(cookie_token)
    except (OwnerSessionDenied, SQLAlchemyError):
        raise _error(401, "owner_auth_required") from None
    return OwnerSessionView(
        scopes=sorted(principal.scopes),
        expires_at=record.expires_at,
        csrf_token=session_csrf_token(cookie_token, secret=settings.telegram_bot_token),
    )


def _session_mutation_principal(
    request: Request,
    csrf_token: str,
    db: Session,
    settings: Settings,
) -> tuple[OwnerSessionPrincipal, OwnerSessionRecord]:
    principal = require_owner_session(request, None, db=db, settings=settings)
    cookie_token = _request_cookie(request, SESSION_COOKIE)
    if not valid_session_csrf(
        cookie_token,
        csrf_token,
        secret=settings.telegram_bot_token,
    ):
        raise _error(403, "csrf_denied")
    try:
        record = OwnerSessionStore(db).authenticate(cookie_token)
    except OwnerSessionDenied:
        raise _error(401, "owner_auth_required") from None
    return principal, record


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
    db: Session = Depends(get_owner_session_db),
    settings: Settings = Depends(get_settings),
) -> None:
    _no_store(response)
    principal, _ = _session_mutation_principal(
        request, csrf_token or "", db, settings
    )
    try:
        OwnerSessionStore(db).revoke_current(
            session_hash=principal.session_hash,
            owner_id=principal.owner_id,
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        _logger.warning("owner_auth reason=logout_persist_failed")
        raise _error(503, "owner_auth_unavailable") from None
    _clear_cookie(response, SESSION_COOKIE)


@router.post("/revoke-all", status_code=204)
def revoke_all(
    request: Request,
    response: Response,
    csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
    db: Session = Depends(get_owner_session_db),
    settings: Settings = Depends(get_settings),
) -> None:
    _no_store(response)
    _, record = _session_mutation_principal(request, csrf_token or "", db, settings)
    try:
        OwnerSessionStore(db).revoke_all_for_authenticated_owner(principal=record)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        _logger.warning("owner_auth reason=revoke_all_persist_failed")
        raise _error(503, "owner_auth_unavailable") from None
    _clear_cookie(response, SESSION_COOKIE)
