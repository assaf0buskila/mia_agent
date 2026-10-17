"""Private Owner OS v1 read router."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.api.owner_sessions import owner_scope
from app.core.config import Settings, get_settings
from app.services.owner_os_reads import (
    API_VERSION,
    OwnerOsInvalidQuery,
    OwnerOsReader,
    OwnerOsReadError,
    blocked_capability,
    envelope,
    pagination,
    read_capability,
    read_snapshot,
    section,
)

router = APIRouter(prefix="/v1/owner/os", tags=["owner-os"])
_contacts_owner = owner_scope("owner-os:read:contacts")
_activities_owner = owner_scope("owner-os:read:activities")
_reminders_owner = owner_scope("owner-os:read:reminders")
_KNOWN_TODAY_SECTIONS = frozenset(
    {"next_actions", "tasks", "reminders", "operational_status"}
)
_SECTION_SCOPES = {
    "next_actions": "owner-os:read:contacts",
    "tasks": "owner-os:read:tasks",
    "reminders": "owner-os:read:reminders",
    "operational_status": "owner-os:read:operations",
}


def _request_id() -> str:
    return "req_" + hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:24]


def _error(request: Request, error: OwnerOsReadError) -> JSONResponse:
    del request
    response = JSONResponse(
        status_code=error.status_code,
        content={
            "api_version": API_VERSION,
            "request_id": _request_id(),
            "error": {"code": error.code, "retryable": error.retryable},
        },
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _forbidden(request: Request, code: str = "forbidden_scope") -> JSONResponse:
    error = OwnerOsInvalidQuery(code)
    error.code = code
    error.status_code = 403
    return _error(request, error)


def _ok(payload: dict[str, object]) -> JSONResponse:
    response = JSONResponse(payload)
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _parse_limit(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise OwnerOsInvalidQuery("invalid limit") from None
    if str(parsed) != value.strip() or not 1 <= parsed <= 50:
        raise OwnerOsInvalidQuery("invalid limit")
    return parsed


def _query_keys(request: Request, allowed: frozenset[str]) -> None:
    if any(key not in allowed for key in request.query_params):
        raise OwnerOsInvalidQuery("unknown query parameter")


def _parse_time(value: str | None, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise OwnerOsInvalidQuery(f"invalid {field}") from None
    if parsed.tzinfo is None:
        raise OwnerOsInvalidQuery(f"naive {field}")
    return parsed


def _principal_owner(principal: object, settings: Settings) -> str:
    owner_id = str(getattr(principal, "owner_id", ""))
    if owner_id not in settings.telegram_owner_user_id_set():
        error = OwnerOsInvalidQuery("owner not bound")
        error.code = "owner_not_bound"
        error.status_code = 403
        raise error
    return owner_id


def _run(
    request: Request,
    principal: object,
    settings: Settings,
    operation: Callable[[OwnerOsReader], dict[str, object]],
) -> JSONResponse:
    try:
        owner_id = _principal_owner(principal, settings)
        with read_snapshot(settings) as session:
            reader = OwnerOsReader(
                session,
                owner_id=owner_id,
                workspace_id=settings.owner_os_workspace_id,
                cursor_secret=settings.owner_os_cursor_secret,
            )
            return _ok(operation(reader))
    except OwnerOsReadError as exc:
        return _error(request, exc)
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        error = OwnerOsReadError("query failed")
        if sqlstate == "57014":
            error.code = "query_timeout"
            error.status_code = 504
        return _error(request, error)
    except SQLAlchemyError:
        return _error(request, OwnerOsReadError("query failed"))


def _list_envelope(
    reader: OwnerOsReader,
    *,
    page: Any,
    coverage_scope: str,
    capability: str,
    first_page: bool,
    blocked: tuple[dict[str, object], ...],
) -> dict[str, object]:
    coverage = "complete" if first_page and not page.has_more else "partial"
    item_reasons = {
        reason
        for item in page.items
        for reason in item.get("reason_codes", [])
        if isinstance(reason, str)
    }
    reasons = tuple(
        ([] if coverage == "complete" else ["paginated_snapshot"]) + sorted(item_reasons)
    )
    return envelope(
        reader,
        data=section(page, coverage_scope=coverage_scope, first_page=first_page),
        coverage=coverage,
        coverage_scope=coverage_scope,
        reason_codes=reasons,
        capabilities=(read_capability(capability), *blocked),
        pagination_value=pagination(page),
    )


@router.get("/contacts")
def contacts(
    request: Request,
    limit: str = Query(default="20"),
    cursor: str | None = Query(default=None, max_length=128),
    principal: object = Depends(_contacts_owner),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    def operation(reader: OwnerOsReader) -> dict[str, object]:
        _query_keys(request, frozenset({"limit", "cursor"}))
        bounded = _parse_limit(limit)
        page = reader.list_contacts(limit=bounded, cursor=cursor)
        return _list_envelope(
            reader,
            page=page,
            coverage_scope="stored_pg_contacts",
            capability="contacts.read",
            first_page=cursor is None,
            blocked=(blocked_capability("contacts.edit", "write_api_missing"),),
        )

    return _run(request, principal, settings, operation)


@router.get("/contacts/{contact_id}")
def contact_detail(
    contact_id: str,
    request: Request,
    principal: object = Depends(_contacts_owner),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    def operation(reader: OwnerOsReader) -> dict[str, object]:
        _query_keys(request, frozenset())
        record = reader.get_contact(contact_id)
        return envelope(
            reader,
            data=record,
            coverage="complete",
            coverage_scope="stored_pg_contact_detail",
            reason_codes=("business_classification_not_collected", "watermark_missing"),
            capabilities=(
                read_capability("contacts.read"),
                blocked_capability("contacts.edit", "write_api_missing"),
            ),
        )

    return _run(request, principal, settings, operation)


@router.get("/contacts/{contact_id}/activities")
def contact_activities(
    contact_id: str,
    request: Request,
    limit: str = Query(default="20"),
    cursor: str | None = Query(default=None, max_length=128),
    principal: object = Depends(_activities_owner),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    def operation(reader: OwnerOsReader) -> dict[str, object]:
        _query_keys(request, frozenset({"limit", "cursor"}))
        bounded = _parse_limit(limit)
        page = reader.list_activities(contact_id=contact_id, limit=bounded, cursor=cursor)
        return _list_envelope(
            reader,
            page=page,
            coverage_scope="stored_pg_contact_activities",
            capability="activities.read",
            first_page=cursor is None,
            blocked=(blocked_capability("activities.create", "write_api_missing"),),
        )

    return _run(request, principal, settings, operation)


@router.get("/reminders")
def reminders(
    request: Request,
    limit: str = Query(default="20"),
    cursor: str | None = Query(default=None, max_length=128),
    status: str = Query(default="active"),
    from_value: str | None = Query(default=None, alias="from"),
    to_value: str | None = Query(default=None, alias="to"),
    principal: object = Depends(_reminders_owner),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    def operation(reader: OwnerOsReader) -> dict[str, object]:
        _query_keys(request, frozenset({"limit", "cursor", "status", "from", "to"}))
        bounded = _parse_limit(limit)
        page = reader.list_reminders(
            limit=bounded,
            cursor=cursor,
            status=status,
            from_at=_parse_time(from_value, "from"),
            to_at=_parse_time(to_value, "to"),
        )
        return _list_envelope(
            reader,
            page=page,
            coverage_scope="stored_pg_owner_reminders",
            capability="reminders.read",
            first_page=cursor is None,
            blocked=(blocked_capability("reminders.write", "write_api_missing"),),
        )

    return _run(request, principal, settings, operation)


@router.get("/today")
def today(
    request: Request,
    sections: str | None = Query(default=None),
    limit: str = Query(default="20"),
    next_actions_cursor: str | None = Query(default=None, max_length=128),
    reminders_cursor: str | None = Query(default=None, max_length=128),
    principal: object = Depends(_contacts_owner),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    scopes = frozenset(getattr(principal, "scopes", ()))
    if sections is None:
        selected = {"next_actions"}
        if "owner-os:read:reminders" in scopes:
            selected.add("reminders")
    else:
        selected = {item.strip() for item in sections.split(",") if item.strip()}
        if not selected or not selected <= _KNOWN_TODAY_SECTIONS:
            return _error(request, OwnerOsInvalidQuery("unknown today section"))
        missing = [name for name in selected if _SECTION_SCOPES[name] not in scopes]
        if missing:
            return _forbidden(request)

    def operation(reader: OwnerOsReader) -> dict[str, object]:
        _query_keys(
            request,
            frozenset({"sections", "limit", "next_actions_cursor", "reminders_cursor"}),
        )
        bounded = _parse_limit(limit)
        data: dict[str, object] = {}
        capabilities: list[dict[str, object]] = []
        coverages: list[str] = []
        if "next_actions" in selected:
            page = reader.list_next_actions(limit=bounded, cursor=next_actions_cursor)
            current = section(
                page,
                coverage_scope="stored_pg_contact_next_steps",
                first_page=next_actions_cursor is None,
            )
            data["next_actions"] = current
            coverages.append(str(current["coverage"]))
            capabilities.append(read_capability("contacts.read"))
        if "reminders" in selected:
            page = reader.list_reminders(limit=bounded, cursor=reminders_cursor)
            current = section(
                page,
                coverage_scope="stored_pg_owner_reminders",
                first_page=reminders_cursor is None,
            )
            data["reminders"] = current
            coverages.append(str(current["coverage"]))
            capabilities.append(read_capability("reminders.read"))
        if "tasks" in selected:
            data["tasks"] = {
                "section_state": "unavailable",
                "items": None,
                "coverage": "unknown",
                "coverage_scope": "stored_pg_owner_tasks",
                "freshness": "unknown",
                "reason_codes": ["owner_attribution_unproven"],
                "pagination": None,
            }
            coverages.append("unknown")
        if "operational_status" in selected:
            data["operational_status"] = {
                "state": "unknown",
                "worker": {"state": "unknown", "observed_at": None},
                "delivery_attention": None,
                "reason_codes": ["operational_read_api_missing"],
            }
            coverages.append("unknown")
        capabilities.extend(
            (
                blocked_capability("tasks.read", "owner_attribution_unproven"),
                blocked_capability("operations.read", "operational_read_api_missing"),
                blocked_capability("contacts.edit", "write_api_missing"),
                blocked_capability("reminders.write", "write_api_missing"),
            )
        )
        coverage = "complete" if coverages and set(coverages) == {"complete"} else "partial"
        reasons = () if coverage == "complete" else ("section_partial_or_unavailable",)
        return envelope(
            reader,
            data=data,
            coverage=coverage,
            coverage_scope="stored_pg_today_sections",
            reason_codes=reasons,
            capabilities=capabilities,
            pagination_value=None,
        )

    return _run(request, principal, settings, operation)
