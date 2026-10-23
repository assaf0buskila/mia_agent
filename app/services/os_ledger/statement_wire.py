"""Wire rendering for the statement bodies (V2-26; V2_PG_CONTRACTS.md §10.1, §10.2, §12.4, §22 E12).

Private to V2-26: the three bodies the statement routes share (the §10.1 binding,
``batch_created`` and ``receipt_v2``) are rendered here, exactly as §10 and E12 spell them. V2-20's
``contract.py`` has its own ``binding`` and ``receipt_v2`` (other signatures); they are not shared
yet. Every owner-facing v2 body carries the binding (E12 a);
``observed_version`` is REQUIRED on ``receipt_v2``, ``batch_created`` and ``draft_saved`` (E12 b).
Nothing here reads a clock or a database: callers pass the values in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.services.os_ledger import api

API_VERSION = "assaf-os.v2"
SOURCE = "postgresql"


def iso(value: datetime | None) -> str | None:
    """UTC with milliseconds and a Z, the form every v2 timestamp uses."""
    if value is None:
        return None
    moment = value.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def binding(ctx: api.TrustedOwnerContext, observed_version: int | None = None) -> dict[str, Any]:
    """The §10.1 keys. ``observed_version`` is omitted only when ``None`` (never for a read)."""
    body: dict[str, Any] = {
        "api_version": API_VERSION, "source": SOURCE, "workspace_id": ctx.workspace,
        "authority_epoch": ctx.authority_epoch,
    }
    if observed_version is not None:
        body["observed_version"] = int(observed_version)
    return body


@dataclass(frozen=True)
class BatchSubmitted:
    """The result of ``submit``; the route renders it as ``batch_created``."""

    proposal_id: str
    statement_id: str
    status: str
    conflict: bool
    superseded_proposal_id: str | None
    summary: dict[str, int]
    observed_version: int


def batch_created(ctx: api.TrustedOwnerContext, submitted: BatchSubmitted) -> dict[str, Any]:
    return {
        **binding(ctx, submitted.observed_version),
        "proposal_id": submitted.proposal_id,
        "statement_id": submitted.statement_id,
        "status": submitted.status,
        "conflict": submitted.conflict,
        "superseded_proposal_id": submitted.superseded_proposal_id,
        "summary": dict(submitted.summary),
    }


def receipt_v2(ctx: api.TrustedOwnerContext, result: api.ReceiptResult,
               server_now: datetime) -> dict[str, Any]:
    """``receipt_v2`` for a statement batch (kind ``statement_batch``).

    ``undo`` is null (it is non-null only for ``owner_direct``); ``records`` are the current
    expense records, non-empty only for a committed batch that booked an expense.
    """
    receipt, proposal = result.receipt, result.proposal
    if not result.found or receipt is None or proposal is None:
        return {
            **binding(ctx, result.observed_version), "status": "unknown", "request_id": None,
            "proposal_id": None, "kind": None, "actor_source": None, "commit_version": None,
            "committed_at": None, "server_now": iso(server_now), "records": [], "undo": None,
            "undo_of": None, "reason_codes": ["receipt_not_found"],
        }
    return {
        **binding(ctx, result.observed_version),
        "status": receipt.status,
        "request_id": receipt.request_id,
        "proposal_id": receipt.proposal_id,
        "kind": receipt.kind,
        "actor_source": proposal.source,
        "commit_version": receipt.commit_version,
        "committed_at": iso(receipt.completed_at) if receipt.status == "committed" else None,
        "server_now": iso(server_now),
        "records": [{"entity": entity, "record": dict(record)}
                    for entity, record in result.records],
        "undo": None,
        "undo_of": receipt.undo_of,
        "reason_codes": list(receipt.reason_codes),
    }
