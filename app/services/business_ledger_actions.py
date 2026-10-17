"""Approval-bound actions for the TEST-only authoritative business ledger.

This layer binds model-facing proposals to an exact ledger snapshot and endpoint
configuration.  It deliberately delegates validation and receipt verification to
the reviewed ``BusinessLedger`` port and delegates at-most-once execution to the
existing owner-action claim.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from app.capabilities.types import Principal
from app.core.config import MiaEnv
from app.services.business_ledger import (
    BusinessLedger,
    DealWrite,
    ExpenseWrite,
    LedgerError,
    PaymentWrite,
    ProjectWrite,
    ScheduleWrite,
)
from app.services.owner_actions import (
    OwnerActionExecution,
    OwnerActionTargetChanged,
    execute_owner_action,
    propose_owner_action,
)

KINDS = frozenset(
    {
        "business.save_deal",
        "business.save_schedule",
        "business.record_payment",
        "business.record_expense",
        "business.save_project",
        "business.recover_receipt",
    }
)
_WRITE_KINDS = KINDS - {"business.recover_receipt"}


def business_enabled(settings: object, principal: Principal, *, private_owner_chat: bool) -> bool:
    """Independent fail-closed gate used before any store or provider access."""
    return bool(
        principal.is_owner()
        and principal.source == "telegram"
        and private_owner_chat
        and principal.actor_id.isascii()
        and principal.actor_id.isdecimal()
        and principal.actor_id in settings.telegram_owner_user_id_set()
        and getattr(settings, "env", None) == MiaEnv.TEST
        and bool(getattr(settings, "owner_os_enabled", False))
        and bool(getattr(settings, "business_ledger_test_enabled", False))
        and not bool(getattr(settings, "assaf_os_enabled", False))
        and not bool(getattr(settings, "kill_switch", False))
    )


def make_ledger(settings: object, owner_id: str) -> BusinessLedger:
    """Small seam for offline tests; production always uses the signed ledger port."""
    return BusinessLedger(settings, owner_id)


def stable_request_id(principal: Principal, source_ref: str, operation: str) -> str:
    if not source_ref.strip():
        raise ValueError("source_ref_required")
    return str(uuid5(NAMESPACE_URL, f"mia:{principal.actor_id}:{source_ref}:{operation}"))


def stable_record_id(request_id: str, label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{request_id}:{label}"))


def _binding(settings: object) -> dict[str, str]:
    return {
        "workspace_id": str(settings.business_ledger_workspace_id),
        "script_hash": hashlib.sha256(
            str(settings.business_ledger_script_url).encode()
        ).hexdigest(),
        "key_hash": hashlib.sha256(str(settings.business_ledger_hmac_secret).encode()).hexdigest(),
        "source": "google_sheets",
        "mode": "TEST",
    }


def _snapshot_map(snapshot: Mapping[str, Any], name: str) -> dict[str, Mapping[str, Any]]:
    rows = snapshot.get(name)
    if not isinstance(rows, list):
        raise LedgerError("source_unavailable")
    return {
        str(row["id"]): row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }


def _target_for(
    settings: object, snapshot: Mapping[str, Any], kind: str, parameters: Mapping[str, Any]
) -> dict[str, Any]:
    target: dict[str, Any] = {**_binding(settings), "operation": kind}
    if kind == "business.save_deal":
        record = parameters["deal"]
        current = _snapshot_map(snapshot, "deals").get(record["id"])
        expected = parameters["expected_revision"]
        if (expected == 0 and current is not None) or (
            expected > 0 and (current is None or current.get("revision") != expected)
        ):
            raise ValueError("stale_target")
        target.update(
            record_id=record["id"],
            expected_revision=expected,
            current=current,
        )
    elif kind == "business.save_schedule":
        deal_id = str(parameters["deal_id"])
        current = _snapshot_map(snapshot, "deals").get(deal_id)
        charges = sorted(
            (
                row
                for row in snapshot.get("charges", [])
                if isinstance(row, dict) and row.get("deal_id") == deal_id
            ),
            key=lambda row: str(row.get("id", "")),
        )
        count = parameters["number_of_charges"]
        if (
            current is None
            or current.get("revision") != parameters["expected_revision"]
            or charges
            or (current.get("payment_plan") == "full" and count != 1)
            or (current.get("payment_plan") != "full" and not 2 <= count <= 36)
            or parameters["first_due_date"] < str(current.get("start_date"))
        ):
            raise ValueError("stale_target")
        quotient, remainder = divmod(int(current["amount_minor"]), count)
        prospective = [
            {
                "id": parameters["charge_ids"][index],
                "due_date": BusinessLedger._next_month(parameters["first_due_date"], index),
                "amount_minor": quotient + (1 if index < remainder else 0),
                "currency": "ILS",
            }
            for index in range(count)
        ]
        target.update(
            record_id=deal_id,
            expected_revision=parameters["expected_revision"],
            current=current,
            current_charges=charges,
            prospective_charges=prospective,
        )
    elif kind == "business.record_payment":
        payment = parameters["payment"]
        charge_id = str(payment["charge_id"])
        current = _snapshot_map(snapshot, "charges").get(charge_id)
        received = sum(
            int(row["amount_minor"])
            for row in snapshot.get("payments", [])
            if isinstance(row, dict) and row.get("charge_id") == charge_id
        )
        if (
            current is None
            or current.get("status") != "planned"
            or current.get("revision") != parameters["expected_charge_revision"]
            or payment["amount_minor"] > int(current["amount_minor"]) - received
        ):
            raise ValueError("stale_or_overpaid")
        target.update(
            record_id=payment["id"],
            expected_revision=parameters["expected_charge_revision"],
            current=current,
            received_minor=received,
        )
    elif kind == "business.record_expense":
        expense = parameters["expense"]
        project_id = expense.get("project_id")
        current = _snapshot_map(snapshot, "expenses").get(expense["id"])
        project = _snapshot_map(snapshot, "projects").get(project_id) if project_id else None
        if current is not None or (project_id and project is None):
            raise ValueError("stale_target")
        target.update(
            record_id=expense["id"],
            expected_revision=0,
            current=current,
            project=project,
        )
    elif kind == "business.save_project":
        project = parameters["project"]
        current = _snapshot_map(snapshot, "projects").get(project["id"])
        expected = parameters["expected_revision"]
        if (expected == 0 and current is not None) or (
            expected > 0 and (current is None or current.get("revision") != expected)
        ):
            raise ValueError("stale_target")
        target.update(
            record_id=project["id"],
            expected_revision=expected,
            current=current,
        )
    elif kind == "business.recover_receipt":
        target.update(
            record_id=parameters["expected_id"],
            expected_revision=parameters["expected_revision"],
            request_id=parameters["request_id"],
        )
    return target


def validate_parameters(kind: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a strict canonical DTO; bools/floats and extra keys are rejected."""
    models = {
        "business.save_deal": DealWrite,
        "business.save_schedule": ScheduleWrite,
        "business.record_payment": PaymentWrite,
        "business.record_expense": ExpenseWrite,
        "business.save_project": ProjectWrite,
    }
    if kind == "business.recover_receipt":
        allowed = {"request_id", "expected_id", "expected_revision"}
        if set(raw) != allowed:
            raise ValueError("invalid_recovery")
        request_id = str(raw.get("request_id") or "")
        expected_id = str(raw.get("expected_id") or "")
        revision = raw.get("expected_revision")
        # Reuse strict DTO validators for both UUID fields without adding a parallel schema.
        DealWrite.model_validate(
            {
                "request_id": request_id,
                "expected_revision": 0,
                "deal": {
                    "id": expected_id,
                    "title": "receipt",
                    "amount_minor": 1,
                    "currency": "ILS",
                    "payment_plan": "full",
                    "start_date": "2000-01-01",
                    "status": "active",
                    "project_id": None,
                    "notes": "",
                },
            }
        )
        if type(revision) is not int or not 1 <= revision <= 1_000_000:
            raise ValueError("invalid_recovery")
        return dict(raw)
    model = models.get(kind)
    if model is None:
        raise ValueError("unsupported_kind")
    return model.model_validate(dict(raw)).model_dump(mode="json")


def propose_business_action(
    store,
    *,
    settings: object,
    principal: Principal,
    private_owner_chat: bool,
    source_ref: str,
    kind: str,
    parameters: Mapping[str, Any],
    ledger: object | None = None,
    now=None,
):
    if not business_enabled(settings, principal, private_owner_chat=private_owner_chat):
        raise PermissionError("business ledger is unavailable")
    canonical = validate_parameters(kind, parameters)
    port = ledger or make_ledger(settings, principal.actor_id)
    snapshot = port.finance_snapshot() if kind != "business.recover_receipt" else {}
    target = _target_for(settings, snapshot, kind, canonical)
    return propose_owner_action(
        store,
        principal=principal,
        source_ref=source_ref,
        kind=kind,
        parameters=canonical,
        target=target,
        risk="R3",
        now=now,
    )


def _receipt_text(receipt: Mapping[str, Any]) -> str:
    return (
        f"Business ledger committed: {receipt['id']} revision {receipt['revision']} "
        f"(receipt {receipt['receipt_id']})"
    )


def _matching_receipt(receipt: Mapping[str, Any], parameters: Mapping[str, Any]) -> bool:
    return bool(
        receipt.get("status") == "committed"
        and receipt.get("request_id") == parameters.get("request_id")
        and receipt.get("id") == parameters.get("expected_id")
        and type(receipt.get("revision")) is int
        and receipt.get("revision") == parameters.get("expected_revision")
        and receipt.get("source") == "google_sheets"
        and receipt.get("mode") == "TEST"
        and isinstance(receipt.get("receipt_id"), str)
        and re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            str(receipt.get("receipt_id")),
        )
    )


def _matching_write_receipt(
    kind: str, receipt: Mapping[str, Any], parameters: Mapping[str, Any]
) -> bool:
    if kind == "business.save_deal":
        expected_id = parameters["deal"]["id"]
        expected_revision = parameters["expected_revision"] + 1
    elif kind == "business.save_schedule":
        expected_id = parameters["deal_id"]
        expected_revision = parameters["expected_revision"] + 1
    elif kind == "business.record_payment":
        expected_id = parameters["payment"]["id"]
        expected_revision = 1
    elif kind == "business.record_expense":
        expected_id = parameters["expense"]["id"]
        expected_revision = 1
    elif kind == "business.save_project":
        expected_id = parameters["project"]["id"]
        expected_revision = parameters["expected_revision"] + 1
    else:
        return False
    return _matching_receipt(
        receipt,
        {
            "request_id": parameters["request_id"],
            "expected_id": expected_id,
            "expected_revision": expected_revision,
        },
    )


def execute_business_action(
    store, *, settings: object, principal: Principal, proposal_id: str
) -> OwnerActionExecution:
    """Execute a finance proposal with no construction of unrelated providers."""
    if not business_enabled(settings, principal, private_owner_chat=True):
        return OwnerActionExecution("unauthorized")
    port_box: dict[str, Any] = {}

    def schema(kind: str, parameters: Mapping[str, Any]) -> bool:
        try:
            validate_parameters(kind, parameters)
        except (ValidationError, ValueError, TypeError):
            return False
        return kind in KINDS

    def policy(kind: str, _parameters: Mapping[str, Any]) -> bool:
        return kind in KINDS and business_enabled(settings, principal, private_owner_chat=True)

    def connect(kind: str) -> bool:
        if kind not in KINDS:
            return False
        try:
            port_box["ledger"] = make_ledger(settings, principal.actor_id)
        except LedgerError:
            return False
        return True

    def current(kind: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        port = port_box["ledger"]
        try:
            snapshot = port.finance_snapshot() if kind != "business.recover_receipt" else {}
            return _target_for(settings, snapshot, kind, parameters)
        except (LedgerError, KeyError, TypeError, ValueError):
            return {}

    def run(kind: str, parameters: Mapping[str, Any]) -> str:
        port = port_box["ledger"]
        try:
            if kind == "business.save_deal":
                receipt = port.save(DealWrite.model_validate(parameters))
            elif kind == "business.save_schedule":
                receipt = port.save_schedule(ScheduleWrite.model_validate(parameters))
            elif kind == "business.record_payment":
                receipt = port.record_payment(PaymentWrite.model_validate(parameters))
            elif kind == "business.record_expense":
                receipt = port.record_expense(ExpenseWrite.model_validate(parameters))
            elif kind == "business.save_project":
                receipt = port.save_project(ProjectWrite.model_validate(parameters))
            elif kind == "business.recover_receipt":
                receipt = port.receipt(str(parameters["request_id"]))
                if not _matching_receipt(receipt, parameters):
                    raise RuntimeError("receipt mismatch")
                return _receipt_text(receipt)
            else:
                raise PermissionError("unsupported owner action")
        except LedgerError as exc:
            if kind != "business.recover_receipt" and exc.code in {
                "conflict",
                "record_not_found",
                "validation_error",
            }:
                raise OwnerActionTargetChanged(exc.code) from exc
            raise
        if not _matching_write_receipt(kind, receipt, parameters):
            raise RuntimeError("ledger receipt mismatch")
        return _receipt_text(receipt)

    return execute_owner_action(
        store,
        principal=principal,
        proposal_id=proposal_id,
        validate_schema=schema,
        validate_policy=policy,
        connection_ready=connect,
        current_target=current,
        execute=run,
    )
