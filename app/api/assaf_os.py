"""Authenticated owner HTTP surface for the shared Assaf OS workbook."""

from __future__ import annotations

import re

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from app.api.owner_sessions import OwnerSessionPrincipal, owner_mutation_scope, owner_scope
from app.core.config import Settings, get_settings
from app.services.assaf_os import AssafOsError, AssafOsPort, DecisionRequest, ProposeRequest
from app.services.assaf_os_snapshot_cache import read_cached_snapshot, remember_snapshot

router = APIRouter(prefix="/v1/owner/os/unified", tags=["owner-os-unified"])
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,63}$")
_REQUEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


def _port(settings: Settings, owner: OwnerSessionPrincipal) -> AssafOsPort:
    try:
        return AssafOsPort(settings, owner.owner_id, actor_source="OS")
    except AssafOsError as exc:
        raise HTTPException(exc.status, exc.code) from None


async def _run(call):
    try:
        return await run_in_threadpool(call)
    except AssafOsError as exc:
        raise HTTPException(exc.status, exc.code) from None


@router.get("/schema")
async def schema(
    owner: OwnerSessionPrincipal = Depends(owner_scope("assaf-os:read")),
    settings: Settings = Depends(get_settings),
):
    port = _port(settings, owner)
    return await _run(port.schema)


@router.get("")
async def snapshot(
    background: BackgroundTasks,
    owner: OwnerSessionPrincipal = Depends(owner_scope("assaf-os:read")),
    settings: Settings = Depends(get_settings),
):
    port = _port(settings, owner)
    result = await _run(port.snapshot)
    # V2-42: refresh the instant-open cache once the response is on the wire. This is the only
    # caller of remember_snapshot, it never raises, and it cannot change or delay this response.
    background.add_task(remember_snapshot, settings, owner.owner_id, result)
    return result


@router.post("/proposals")
async def propose(
    body: ProposeRequest,
    owner: OwnerSessionPrincipal = Depends(owner_mutation_scope("assaf-os:propose")),
    settings: Settings = Depends(get_settings),
):
    if settings.kill_switch:
        raise HTTPException(403, "kill_switch")
    port = _port(settings, owner)
    return await _run(lambda: port.propose(body))


@router.get("/proposals/{proposal_id}")
async def proposal(
    proposal_id: str,
    owner: OwnerSessionPrincipal = Depends(owner_scope("assaf-os:read")),
    settings: Settings = Depends(get_settings),
):
    if not _ID.fullmatch(proposal_id):
        raise HTTPException(422, "invalid_proposal_id")
    port = _port(settings, owner)
    return await _run(lambda: port.proposal(proposal_id))


@router.post("/proposals/{proposal_id}/approve")
async def approve(
    body: DecisionRequest,
    proposal_id: str,
    owner: OwnerSessionPrincipal = Depends(owner_mutation_scope("assaf-os:approve")),
    settings: Settings = Depends(get_settings),
):
    if not _ID.fullmatch(proposal_id):
        raise HTTPException(422, "invalid_proposal_id")
    if settings.kill_switch:
        raise HTTPException(403, "kill_switch")
    port = _port(settings, owner)
    return await _run(lambda: port.decide("approve", proposal_id, body))


@router.post("/proposals/{proposal_id}/reject")
async def reject(
    body: DecisionRequest,
    proposal_id: str,
    owner: OwnerSessionPrincipal = Depends(owner_mutation_scope("assaf-os:approve")),
    settings: Settings = Depends(get_settings),
):
    if not _ID.fullmatch(proposal_id):
        raise HTTPException(422, "invalid_proposal_id")
    if settings.kill_switch:
        raise HTTPException(403, "kill_switch")
    port = _port(settings, owner)
    return await _run(lambda: port.decide("reject", proposal_id, body))


@router.get("/receipts/{request_id}")
async def receipt(
    request_id: str,
    owner: OwnerSessionPrincipal = Depends(owner_scope("assaf-os:read")),
    settings: Settings = Depends(get_settings),
):
    if not _REQUEST.fullmatch(request_id):
        raise HTTPException(422, "invalid_request_id")
    port = _port(settings, owner)
    return await _run(lambda: port.receipt(request_id))


@router.get("/cached")
async def cached_snapshot(
    owner: OwnerSessionPrincipal = Depends(owner_scope("assaf-os:read")),
    settings: Settings = Depends(get_settings),
):
    # V2-42: the last validated snapshot, served without the Apps Script. The port is constructed
    # only to re-prove the enable flag, the owner allow-list and the configuration shapes (the
    # same 404 and 503 as the snapshot route). None of its calls or caches is ever touched.
    _port(settings, owner)
    return await run_in_threadpool(read_cached_snapshot, settings, owner.owner_id)
