"""Slice C validates remote finance truth and never retries ambiguous writes."""

import json
from datetime import UTC, datetime

import pytest
from app.services import business_ledger as port
from pydantic import ValidationError

from tests.unit import test_business_ledger as base

NOW = "2026-10-12T10:11:12.000Z"
IDS = [f"{index:08d}-1111-4111-8111-{index:012d}" for index in range(1, 12)]


def finance_snapshot(*, coverage="financial_complete"):
    totals = {
        "deal_value_minor": 10000,
        "scheduled_minor": 10000,
        "received_minor": 1200,
        "outstanding_minor": 8800,
        "expense_minor": 500,
        "net_cash_minor": 700,
    }
    reason_codes = []
    if coverage == "unknown":
        totals = {key: None for key in totals}
        reason_codes = ["financial_coverage_incomplete"]
    return {
        "api_version": "business-ledger.finance.v1",
        "mode": "TEST",
        "source": "google_sheets",
        "observed_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "currency": "ILS",
        "coverage": coverage,
        "counts": {"deals": 1, "charges": 3, "payments": 1, "expenses": 1, "projects": 1},
        "totals": totals,
        "reason_codes": reason_codes,
        "deals": [
            {
                "id": IDS[0],
                "revision": 1,
                "title": "Website",
                "amount_minor": 10000,
                "currency": "ILS",
                "payment_plan": "installments",
                "start_date": "2026-01-01",
                "status": "active",
                "project_id": IDS[8],
                "notes": "",
                "updated_at": NOW,
            }
        ],
        "charges": [
            {
                "id": IDS[1],
                "revision": 1,
                "deal_id": IDS[0],
                "status": "planned",
                "amount_minor": 3334,
                "currency": "ILS",
                "due_date": "2026-01-31",
                "updated_at": NOW,
            },
            {
                "id": IDS[2],
                "revision": 1,
                "deal_id": IDS[0],
                "status": "planned",
                "amount_minor": 3333,
                "currency": "ILS",
                "due_date": "2026-02-28",
                "updated_at": NOW,
            },
            {
                "id": IDS[3],
                "revision": 1,
                "deal_id": IDS[0],
                "status": "planned",
                "amount_minor": 3333,
                "currency": "ILS",
                "due_date": "2026-03-31",
                "updated_at": NOW,
            },
        ],
        "payments": [
            {
                "id": IDS[4],
                "revision": 1,
                "charge_id": IDS[1],
                "status": "posted",
                "amount_minor": 1200,
                "currency": "ILS",
                "received_at": NOW,
                "updated_at": NOW,
            }
        ],
        "expenses": [
            {
                "id": IDS[5],
                "revision": 1,
                "project_id": None,
                "amount_minor": 500,
                "currency": "ILS",
                "occurred_on": "2026-01-15",
                "notes": "Hosting",
                "updated_at": NOW,
            }
        ],
        "projects": [
            {
                "id": IDS[8],
                "revision": 1,
                "title": "Delivery",
                "status": "active",
                "deadline": "2026-03-31",
                "notes": "",
                "updated_at": NOW,
            }
        ],
    }


def schedule():
    return {
        "request_id": IDS[6],
        "expected_revision": 1,
        "deal_id": IDS[0],
        "number_of_charges": 3,
        "first_due_date": "2026-01-31",
        "charge_ids": IDS[1:4],
    }


def payment():
    return {
        "request_id": IDS[6],
        "expected_charge_revision": 1,
        "confirmed_received": True,
        "payment": {
            "id": IDS[4],
            "charge_id": IDS[1],
            "amount_minor": 1200,
            "currency": "ILS",
            "received_at": NOW,
        },
    }


def expense():
    return {
        "request_id": IDS[6],
        "expected_revision": 0,
        "expense": {
            "id": IDS[5],
            "project_id": None,
            "amount_minor": 500,
            "currency": "ILS",
            "occurred_on": "2026-01-15",
            "notes": "Hosting",
        },
    }


def project():
    return {
        "request_id": IDS[6],
        "expected_revision": 0,
        "project": {
            "id": IDS[8],
            "title": "Delivery",
            "status": "active",
            "deadline": "2026-03-31",
            "notes": "",
        },
    }


def receipt(request_id, entity_id, revision):
    return {
        "status": "committed",
        "request_id": request_id,
        "id": entity_id,
        "revision": revision,
        "receipt_id": IDS[10],
        "committed_at": NOW,
        "source": "google_sheets",
        "mode": "TEST",
    }


def service():
    return port.BusinessLedger(base.settings(), base.auth.OWNER_ID)


def test_complete_and_unknown_finance_snapshots_are_validated(monkeypatch):
    ledger = service()
    monkeypatch.setattr(ledger, "_call", lambda operation, data: finance_snapshot())
    result = ledger.finance_snapshot()
    assert result["totals"]["deal_value_minor"] == 10000
    assert result["totals"]["received_minor"] == 1200
    assert result["totals"]["net_cash_minor"] == 700
    assert result["deals"][0]["updated_at"] == NOW
    assert result["payments"][0]["received_at"] == NOW

    monkeypatch.setattr(
        ledger, "_call", lambda operation, data: finance_snapshot(coverage="unknown")
    )
    unknown = ledger.finance_snapshot()
    assert unknown["coverage"] == "unknown"
    assert all(value is None for value in unknown["totals"].values())


def test_cancelled_before_scheduling_is_complete_but_active_without_plan_is_not(monkeypatch):
    cancelled = finance_snapshot()
    cancelled["deals"][0].update(status="cancelled", revision=2)
    cancelled["charges"] = []
    cancelled["payments"] = []
    cancelled["counts"].update(charges=0, payments=0)
    cancelled["totals"].update(
        deal_value_minor=0,
        scheduled_minor=0,
        received_minor=0,
        outstanding_minor=0,
        net_cash_minor=-500,
    )
    ledger = service()
    monkeypatch.setattr(ledger, "_call", lambda operation, data: cancelled)
    assert ledger.finance_snapshot()["totals"]["outstanding_minor"] == 0

    active = finance_snapshot()
    active["charges"] = []
    active["payments"] = []
    active["counts"].update(charges=0, payments=0)
    monkeypatch.setattr(ledger, "_call", lambda operation, data: active)
    with pytest.raises(port.LedgerError, match="source_unavailable"):
        ledger.finance_snapshot()

    active["coverage"] = "unknown"
    active["reason_codes"] = ["financial_coverage_incomplete"]
    active["totals"] = {key: None for key in active["totals"]}
    assert ledger.finance_snapshot()["totals"]["outstanding_minor"] is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["totals"].update(received_minor=0),
        lambda data: data["counts"].update(charges=2),
        lambda data: data["payments"][0].update(charge_id=IDS[9]),
        lambda data: data["charges"][0].update(amount_minor=3335),
        lambda data: data["charges"][0].update(due_date="2025-12-31"),
        lambda data: data["charges"].append(dict(data["charges"][0])),
    ],
)
def test_remote_finance_claims_fail_closed(monkeypatch, mutate):
    data = finance_snapshot()
    mutate(data)
    ledger = service()
    monkeypatch.setattr(ledger, "_call", lambda operation, payload: data)
    with pytest.raises(port.LedgerError, match="source_unavailable"):
        ledger.finance_snapshot()


@pytest.mark.parametrize(
    "model,payload,change",
    [
        (port.ScheduleWrite, schedule(), lambda data: data.update(number_of_charges=2)),
        (port.PaymentWrite, payment(), lambda data: data.update(confirmed_received=False)),
        (port.PaymentWrite, payment(), lambda data: data.update(confirmed_received=1)),
        (
            port.PaymentWrite,
            payment(),
            lambda data: data["payment"].update(received_at="2026-10-12T10:11:12Z"),
        ),
        (port.ExpenseWrite, expense(), lambda data: data.update(expected_revision=1)),
        (port.ExpenseWrite, expense(), lambda data: data.update(expected_revision=False)),
        (port.ExpenseWrite, expense(), lambda data: data.update(expected_revision=0.0)),
        (port.ExpenseWrite, expense(), lambda data: data["expense"].update(notes="=FORMULA")),
        (port.ProjectWrite, project(), lambda data: data["project"].update(deadline="2026-02-30")),
    ],
)
def test_invalid_financial_writes_are_rejected_before_dispatch(monkeypatch, model, payload, change):
    monkeypatch.setattr(port, "_post", lambda *_: pytest.fail("provider must not be called"))
    change(payload)
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    "operation,model,payload,entity_id,revision,method",
    [
        ("save_schedule", port.ScheduleWrite, schedule(), IDS[0], 2, "save_schedule"),
        ("record_payment", port.PaymentWrite, payment(), IDS[4], 1, "record_payment"),
        ("record_expense", port.ExpenseWrite, expense(), IDS[5], 1, "record_expense"),
        ("save_project", port.ProjectWrite, project(), IDS[8], 1, "save_project"),
    ],
)
def test_financial_writes_bind_operation_payload_and_receipt(
    monkeypatch, operation, model, payload, entity_id, revision, method
):
    ledger = service()
    calls = []

    def call(actual_operation, data):
        calls.append((actual_operation, data))
        return receipt(payload["request_id"], entity_id, revision)

    monkeypatch.setattr(ledger, "_call", call)
    result = getattr(ledger, method)(model.model_validate(payload))
    assert result["id"] == entity_id
    assert result["revision"] == revision
    assert calls == [(operation, model.model_validate(payload).model_dump(mode="json"))]
    if operation == "record_payment":
        assert calls[0][1]["payment"]["received_at"] == NOW


@pytest.mark.parametrize(
    "model,payload,method",
    [
        (port.ScheduleWrite, schedule(), "save_schedule"),
        (port.PaymentWrite, payment(), "record_payment"),
        (port.ExpenseWrite, expense(), "record_expense"),
        (port.ProjectWrite, project(), "save_project"),
    ],
)
def test_ambiguous_financial_write_is_unknown_and_never_retried(
    monkeypatch, model, payload, method
):
    calls = []

    def lost(*args):
        calls.append(args)
        raise port.LedgerError("source_unavailable")

    monkeypatch.setattr(port, "_post", lost)
    with pytest.raises(port.LedgerError, match="unknown"):
        getattr(service(), method)(model.model_validate(payload))
    assert len(calls) == 1


@pytest.mark.parametrize("malformed_status", [[], {}])
@pytest.mark.parametrize(
    "model,payload,method",
    [
        (port.ScheduleWrite, schedule(), "save_schedule"),
        (port.PaymentWrite, payment(), "record_payment"),
        (port.ExpenseWrite, expense(), "record_expense"),
        (port.ProjectWrite, project(), "save_project"),
    ],
)
def test_malformed_signed_status_is_unknown_after_one_dispatch(
    monkeypatch, malformed_status, model, payload, method
):
    calls = []

    def malformed(_, body):
        calls.append(body)
        return base.signed_reply(body, status=malformed_status)

    monkeypatch.setattr(port, "_post", malformed)
    with pytest.raises(port.LedgerError, match="unknown"):
        getattr(service(), method)(model.model_validate(payload))
    assert len(calls) == 1


def test_api_exposes_exact_slice_c_routes_with_existing_owner_boundary():
    from app.api.business_ledger import router

    methods = {
        (method, route.path) for route in router.routes for method in (route.methods or set())
    }
    assert {
        ("GET", "/v1/owner/os/business/finance"),
        ("POST", "/v1/owner/os/business/schedules"),
        ("POST", "/v1/owner/os/business/payments"),
        ("POST", "/v1/owner/os/business/expenses"),
        ("POST", "/v1/owner/os/business/projects"),
    } <= methods


def test_write_operation_set_is_explicit_and_receipt_recovery_remains_read_only():
    assert port.WRITE_OPERATIONS == {
        "save_deal",
        "save_schedule",
        "record_payment",
        "record_expense",
        "save_project",
        "save_crm_projection",
    }
    assert "receipt" not in port.WRITE_OPERATIONS
    assert "crm_projection_receipt" not in port.WRITE_OPERATIONS
    assert json.dumps(sorted(port.WRITE_OPERATIONS))
