"""Explicit owner TEST ledger reads and human-confirmed deal writes only."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.owner_sessions import OwnerSessionPrincipal, owner_mutation_scope, owner_scope
from app.core.config import Settings, get_settings
from app.services.business_ledger import (
    BusinessLedger,
    DealWrite,
    ExpenseWrite,
    LedgerError,
    PaymentWrite,
    ProjectWrite,
    ScheduleWrite,
)

router = APIRouter(prefix="/v1/owner/os/business", tags=["business-ledger-test"])


def _ledger(settings: Settings, owner: OwnerSessionPrincipal) -> BusinessLedger:
    try:
        return BusinessLedger(settings, owner.owner_id)
    except LedgerError as exc:
        raise HTTPException(exc.status, exc.code) from None


def _result(call):
    try:
        return call()
    except LedgerError as exc:
        raise HTTPException(exc.status, exc.code) from None


def _no_query(request: Request) -> None:
    if request.query_params:
        raise HTTPException(400, "invalid_query")


@router.get("")
def snapshot(
    request: Request,
    owner: OwnerSessionPrincipal = Depends(owner_scope("business-ledger:read")),
    settings: Settings = Depends(get_settings),
) -> dict:
    _no_query(request)
    return _result(_ledger(settings, owner).snapshot)


@router.post("/deals")
def save_deal(
    request: Request,
    change: DealWrite,
    owner: OwnerSessionPrincipal = Depends(owner_mutation_scope("business-ledger:write")),
    settings: Settings = Depends(get_settings),
) -> dict:
    _no_query(request)
    return _result(lambda: _ledger(settings, owner).save(change))


@router.get("/finance")
def finance_snapshot(
    request: Request,
    owner: OwnerSessionPrincipal = Depends(owner_scope("business-ledger:read")),
    settings: Settings = Depends(get_settings),
) -> dict:
    _no_query(request)
    return _result(_ledger(settings, owner).finance_snapshot)


@router.post("/schedules")
def save_schedule(
    request: Request,
    change: ScheduleWrite,
    owner: OwnerSessionPrincipal = Depends(owner_mutation_scope("business-ledger:write")),
    settings: Settings = Depends(get_settings),
) -> dict:
    _no_query(request)
    return _result(lambda: _ledger(settings, owner).save_schedule(change))


@router.post("/payments")
def record_payment(
    request: Request,
    change: PaymentWrite,
    owner: OwnerSessionPrincipal = Depends(owner_mutation_scope("business-ledger:write")),
    settings: Settings = Depends(get_settings),
) -> dict:
    _no_query(request)
    return _result(lambda: _ledger(settings, owner).record_payment(change))


@router.post("/expenses")
def record_expense(
    request: Request,
    change: ExpenseWrite,
    owner: OwnerSessionPrincipal = Depends(owner_mutation_scope("business-ledger:write")),
    settings: Settings = Depends(get_settings),
) -> dict:
    _no_query(request)
    return _result(lambda: _ledger(settings, owner).record_expense(change))


@router.post("/projects")
def save_project(
    request: Request,
    change: ProjectWrite,
    owner: OwnerSessionPrincipal = Depends(owner_mutation_scope("business-ledger:write")),
    settings: Settings = Depends(get_settings),
) -> dict:
    _no_query(request)
    return _result(lambda: _ledger(settings, owner).save_project(change))


@router.get("/receipts/{request_id}")
def receipt(
    request: Request,
    request_id: UUID,
    owner: OwnerSessionPrincipal = Depends(owner_scope("business-ledger:read")),
    settings: Settings = Depends(get_settings),
) -> dict:
    _no_query(request)
    return _result(lambda: _ledger(settings, owner).receipt(str(request_id)))
