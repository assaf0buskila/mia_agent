"""Typed private-owner tools for TEST business reads and approval proposals."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.services.business_ledger import LedgerError
from app.services.business_ledger_actions import (
    business_enabled,
    make_ledger,
    propose_business_action,
    stable_record_id,
    stable_request_id,
)
from app.services.owner_os_reads import OwnerOsReader, read_snapshot
from app.tools.owner.types import ToolContext, ToolResult

_RESULT_MAX_CHARS = 16 * 1024
_RESULT_PAYLOAD_BUDGET = 15 * 1024


def _guard(ctx: ToolContext) -> ToolResult | None:
    if not business_enabled(ctx.settings, ctx.principal, private_owner_chat=ctx.private_owner_chat):
        return ToolResult(ok=False, error="business ledger is unavailable")
    return None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _snapshot(ctx: ToolContext) -> dict[str, Any]:
    return make_ledger(ctx.settings, ctx.principal.actor_id).finance_snapshot()


def _base(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": snapshot["source"],
        "mode": snapshot["mode"],
        "observed_at": snapshot["observed_at"],
        "period": {"as_of": snapshot["observed_at"]},
        "currency": snapshot["currency"],
        "coverage": snapshot["coverage"],
        "reason_codes": snapshot["reason_codes"],
    }


def _limit(arguments: Mapping[str, Any]) -> int:
    value = arguments.get("limit")
    if type(value) is not int or not 1 <= value <= 10:
        raise ValueError("limit must be 1..10")
    return value


def _page_payload(
    base: Mapping[str, Any],
    rows: list[dict[str, Any]],
    *,
    total: int | None,
    upstream_has_more: bool = False,
    upstream_page_complete: bool = True,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    """Fit only whole DTO rows; never let ToolResult slice serialized JSON."""
    accepted: list[dict[str, Any]] = []
    for row in rows:
        trial = {**base, "items": [*accepted, row]}
        if len(_json(trial)) > _RESULT_PAYLOAD_BUDGET:
            break
        accepted.append(row)
    budget_limited = len(accepted) < len(rows)
    has_more = budget_limited or upstream_has_more or (total is not None and len(accepted) < total)
    payload = {
        **base,
        "items": accepted,
        "returned": len(accepted),
        "total": total,
        "has_more": has_more,
        "page_complete": upstream_page_complete and not budget_limited,
        "partial": has_more or not upstream_page_complete,
        "result_reason_codes": ["result_budget_limited"] if budget_limited else [],
        "next_cursor": None if budget_limited else next_cursor,
    }
    if len(_json(payload)) > _RESULT_MAX_CHARS:
        raise ValueError("result_budget_failed")
    return payload


def _read(ctx: ToolContext, arguments: dict[str, Any], view: str) -> ToolResult:
    denied = _guard(ctx)
    if denied:
        return denied
    try:
        snapshot = _snapshot(ctx)
        base = _base(snapshot)
        if view == "overview":
            base.update(counts=snapshot["counts"], totals=snapshot["totals"])
        else:
            limit = _limit(arguments)
            if view == "outstanding":
                payments: dict[str, int] = {}
                for row in snapshot["payments"]:
                    payments[row["charge_id"]] = (
                        payments.get(row["charge_id"], 0) + row["amount_minor"]
                    )
                rows = [
                    {
                        "charge_id": row["id"],
                        "deal_id": row["deal_id"],
                        "revision": row["revision"],
                        "status": row["status"],
                        "due_date": row["due_date"],
                        "currency": row["currency"],
                        "scheduled_minor": row["amount_minor"],
                        "received_minor": (
                            payments.get(row["id"], 0)
                            if snapshot["coverage"] == "financial_complete"
                            else None
                        ),
                        "outstanding_minor": (
                            max(0, row["amount_minor"] - payments.get(row["id"], 0))
                            if snapshot["coverage"] == "financial_complete"
                            and row["status"] == "planned"
                            else (0 if snapshot["coverage"] == "financial_complete" else None)
                        ),
                    }
                    for row in snapshot["charges"]
                ]
            else:
                rows = list(snapshot[view])
            base = _page_payload(base, rows[:limit], total=len(rows))
        return ToolResult(
            ok=True,
            text=_json(base),
            max_chars=_RESULT_MAX_CHARS,
            evidence="business_ledger_snapshot",
        )
    except (LedgerError, KeyError, TypeError, ValueError) as exc:
        code = exc.code if isinstance(exc, LedgerError) else "source_unavailable"
        return ToolResult(ok=False, error=code)


def _business_overview(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    del arguments
    return _read(ctx, {}, "overview")


def _business_deals(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    return _read(ctx, arguments, "deals")


def _business_payments(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    return _read(ctx, arguments, "payments")


def _business_outstanding(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    return _read(ctx, arguments, "outstanding")


def _business_expenses(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    return _read(ctx, arguments, "expenses")


def _business_projects(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    return _read(ctx, arguments, "projects")


def _business_crm(ctx: ToolContext, arguments: dict[str, Any], *, next_actions: bool) -> ToolResult:
    denied = _guard(ctx)
    if denied:
        return denied
    try:
        limit = _limit(arguments)
        with read_snapshot(ctx.settings) as session:
            reader = OwnerOsReader(
                session,
                owner_id=ctx.principal.actor_id,
                workspace_id=ctx.settings.owner_os_workspace_id,
                cursor_secret=ctx.settings.owner_os_cursor_secret,
            )
            page = (
                reader.list_next_actions(limit=limit)
                if next_actions
                else reader.list_contacts(limit=limit)
            )
            base = {
                "source": "postgresql_read_only",
                "observed_at": reader.observed_at,
            }
            result = _page_payload(
                base,
                page.items,
                total=None,
                upstream_has_more=page.has_more,
                upstream_page_complete=page.page_complete,
                next_cursor=page.next_cursor,
            )
        return ToolResult(
            ok=True,
            text=_json(result),
            max_chars=_RESULT_MAX_CHARS,
            evidence="owner_os_read_snapshot",
        )
    except Exception as exc:  # fixed public code; never provider/database text
        code = str(getattr(exc, "code", "source_unavailable"))
        return ToolResult(ok=False, error=code)


def _business_crm_leads(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    return _business_crm(ctx, arguments, next_actions=False)


def _business_unhandled_leads(ctx: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    return _business_crm(ctx, arguments, next_actions=True)


def _proposal_result(ctx: ToolContext, kind: str, parameters: dict[str, Any]) -> ToolResult:
    denied = _guard(ctx)
    if denied:
        return denied
    try:
        proposal = propose_business_action(
            ctx.store,
            settings=ctx.settings,
            principal=ctx.principal,
            private_owner_chat=ctx.private_owner_chat,
            source_ref=ctx.source_ref,
            kind=kind,
            parameters=parameters,
            now=ctx.now,
        )
    except (LedgerError, ValidationError, ValueError, PermissionError) as exc:
        code = exc.code if isinstance(exc, LedgerError) else "invalid_arguments"
        return ToolResult(ok=False, error=code)
    return ToolResult(
        ok=True,
        text="הוכנה בקשה מדויקת לבדיקה. הפעולה תתבצע רק לאחר אישור הבעלים.",
        approval_id=proposal.approval_id,
        evidence="owner_action_proposal",
    )


def _request(ctx: ToolContext, operation: str) -> str:
    return stable_request_id(ctx.principal, ctx.source_ref, operation)


def _business_propose_deal(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    request_id = _request(ctx, "save_deal")
    return _proposal_result(
        ctx,
        "business.save_deal",
        {
            "request_id": request_id,
            "expected_revision": a.get("expected_revision"),
            "deal": {
                "id": a.get("deal_id"),
                "title": a.get("title"),
                "amount_minor": a.get("amount_minor"),
                "currency": "ILS",
                "payment_plan": a.get("payment_plan"),
                "start_date": a.get("start_date"),
                "status": a.get("status"),
                "project_id": a.get("project_id"),
                "notes": a.get("notes"),
            },
        },
    )


def _business_propose_schedule(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    request_id = _request(ctx, "save_schedule")
    count = a.get("number_of_charges")
    charge_ids = (
        [stable_record_id(request_id, f"charge:{index}") for index in range(count)]
        if type(count) is int and 1 <= count <= 36
        else []
    )
    return _proposal_result(
        ctx,
        "business.save_schedule",
        {
            "request_id": request_id,
            "expected_revision": a.get("expected_revision"),
            "deal_id": a.get("deal_id"),
            "number_of_charges": count,
            "first_due_date": a.get("first_due_date"),
            "charge_ids": charge_ids,
        },
    )


def _business_propose_payment(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    request_id = _request(ctx, "record_payment")
    return _proposal_result(
        ctx,
        "business.record_payment",
        {
            "request_id": request_id,
            "expected_charge_revision": a.get("expected_charge_revision"),
            # Approval is the owner's explicit confirmation that cash was received.
            "confirmed_received": True,
            "payment": {
                "id": stable_record_id(request_id, "payment"),
                "charge_id": a.get("charge_id"),
                "amount_minor": a.get("amount_minor"),
                "currency": "ILS",
                "received_at": a.get("received_at"),
            },
        },
    )


def _business_propose_expense(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    request_id = _request(ctx, "record_expense")
    return _proposal_result(
        ctx,
        "business.record_expense",
        {
            "request_id": request_id,
            "expected_revision": 0,
            "expense": {
                "id": stable_record_id(request_id, "expense"),
                "project_id": a.get("project_id"),
                "amount_minor": a.get("amount_minor"),
                "currency": "ILS",
                "occurred_on": a.get("occurred_on"),
                "notes": a.get("notes"),
            },
        },
    )


def _business_propose_project(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    request_id = _request(ctx, "save_project")
    project_id = a.get("project_id") or stable_record_id(request_id, "project")
    return _proposal_result(
        ctx,
        "business.save_project",
        {
            "request_id": request_id,
            "expected_revision": a.get("expected_revision"),
            "project": {
                "id": project_id,
                "title": a.get("title"),
                "status": a.get("status"),
                "deadline": a.get("deadline"),
                "notes": a.get("notes"),
            },
        },
    )


def _business_recover_receipt(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    return _proposal_result(
        ctx,
        "business.recover_receipt",
        {
            "request_id": a.get("request_id"),
            "expected_id": a.get("expected_id"),
            "expected_revision": a.get("expected_revision"),
        },
    )
