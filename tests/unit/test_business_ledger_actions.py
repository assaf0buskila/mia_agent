from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.capabilities.types import Principal
from app.core.config import MiaEnv, Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import DECISION_APPROVED
from app.domain.owner.proposal_cards import render_owner_proposal_card
from app.services.business_ledger import LedgerError
from app.services.business_ledger_actions import (
    business_enabled,
    execute_business_action,
    propose_business_action,
    stable_record_id,
    stable_request_id,
    validate_parameters,
)
from app.services.owner_actions import (
    OwnerActionExecution,
    decide_owner_action,
    execute_approved_owner_action_with_adapters,
    read_owner_action,
)
from app.tools.owner.business_ledger import (
    _business_deals,
    _business_outstanding,
    _business_overview,
    _business_propose_payment,
)
from app.tools.owner.types import ToolContext
from app.tools.registries.owner_tools import tool_definitions
from pydantic import ValidationError

OWNER = "123456"
WORKSPACE = "workspace-test"
DEAL = "11111111-1111-4111-8111-111111111111"
CHARGE = "22222222-2222-4222-8222-222222222222"
PROJECT = "33333333-3333-4333-8333-333333333333"
STAMP = "2026-10-12T09:00:00.000Z"


def _settings(**changes) -> Settings:
    values = dict(
        _env_file=None,
        env=MiaEnv.TEST,
        telegram_owner_user_ids=OWNER,
        owner_os_enabled=True,
        business_ledger_test_enabled=True,
        owner_os_workspace_id=WORKSPACE,
        business_ledger_workspace_id=WORKSPACE,
        business_ledger_script_url="https://script.google.com/macros/s/" + "x" * 24 + "/exec",
        business_ledger_hmac_secret="test-secret-which-is-at-least-32-bytes",
    )
    values.update(changes)
    return Settings(**values)


def _owner(actor: str = OWNER, source: str = "telegram") -> Principal:
    return Principal.owner(source=source, actor_id=actor)


def _store():
    init_db()
    session = get_session_factory()()
    return LeadStore(session), session


def _snapshot() -> dict:
    return {
        "api_version": "business-ledger.finance.v1",
        "mode": "TEST",
        "source": "google_sheets",
        "observed_at": STAMP,
        "currency": "ILS",
        "coverage": "financial_complete",
        "counts": {"deals": 1, "charges": 1, "payments": 0, "expenses": 0, "projects": 1},
        "totals": {
            "deal_value_minor": 10000,
            "scheduled_minor": 10000,
            "received_minor": 0,
            "outstanding_minor": 10000,
            "expense_minor": 0,
            "net_cash_minor": 0,
        },
        "reason_codes": [],
        "deals": [
            {
                "id": DEAL,
                "revision": 2,
                "title": "Deal",
                "amount_minor": 10000,
                "currency": "ILS",
                "payment_plan": "full",
                "start_date": "2026-10-01",
                "status": "active",
                "project_id": PROJECT,
                "notes": "",
                "updated_at": STAMP,
            }
        ],
        "charges": [
            {
                "id": CHARGE,
                "revision": 1,
                "deal_id": DEAL,
                "status": "planned",
                "amount_minor": 10000,
                "currency": "ILS",
                "due_date": "2026-10-15",
                "updated_at": STAMP,
            }
        ],
        "payments": [],
        "expenses": [],
        "projects": [
            {
                "id": PROJECT,
                "revision": 1,
                "title": "Project",
                "status": "active",
                "deadline": None,
                "notes": "",
                "updated_at": STAMP,
            }
        ],
    }


class FakeLedger:
    def __init__(self):
        self.data = _snapshot()
        self.writes: list[tuple[str, object]] = []
        self.receipts: dict[str, dict] = {}
        self.fail_after_commit = False
        self.bad_receipt = False

    def finance_snapshot(self):
        return deepcopy(self.data)

    def record_payment(self, change):
        self.writes.append(("payment", change))
        receipt = {
            "status": "committed",
            "request_id": change.request_id,
            "id": change.payment.id,
            "revision": 1,
            "receipt_id": "44444444-4444-4444-8444-444444444444",
            "committed_at": STAMP,
            "source": "google_sheets",
            "mode": "TEST",
        }
        self.receipts[change.request_id] = receipt
        if self.fail_after_commit:
            raise LedgerError("unknown")
        if self.bad_receipt:
            return {**receipt, "request_id": str(uuid4())}
        return receipt

    def receipt(self, request_id):
        if request_id not in self.receipts:
            raise LedgerError("record_not_found", 404)
        return deepcopy(self.receipts[request_id])


def _payment_parameters(source_ref: str = "tg:update:1") -> dict:
    request_id = stable_request_id(_owner(), source_ref, "record_payment")
    return {
        "request_id": request_id,
        "expected_charge_revision": 1,
        "confirmed_received": True,
        "payment": {
            "id": stable_record_id(request_id, "payment"),
            "charge_id": CHARGE,
            "amount_minor": 2500,
            "currency": "ILS",
            "received_at": STAMP,
        },
    }


@pytest.mark.parametrize(
    "settings,principal,private",
    [
        (_settings(business_ledger_test_enabled=False), _owner(), True),
        (_settings(env=MiaEnv.DEV), _owner(), True),
        (_settings(env=MiaEnv.PROD), _owner(), True),
        (_settings(kill_switch=True), _owner(), True),
        (_settings(), Principal.client(source="website", actor_id=OWNER), True),
        (_settings(), _owner(source="website"), True),
        (_settings(), _owner("999"), True),
        (_settings(), _owner(), False),
    ],
)
def test_business_gate_rejects_default_prod_kill_visitor_nonprivate_and_removed_owner(
    settings, principal, private
):
    assert not business_enabled(settings, principal, private_owner_chat=private)


def test_tool_definitions_are_default_off_and_explicitly_enabled():
    default = {item["function"]["name"] for item in tool_definitions()}
    enabled = {item["function"]["name"] for item in tool_definitions(allow_business=True)}
    assert "business_overview" not in default
    assert "business_propose_payment" not in default
    assert {"business_overview", "business_propose_payment"} <= enabled


def test_handlers_reject_before_ledger_or_store_access(monkeypatch):
    class ExplodingStore:
        def __getattr__(self, _name):
            raise AssertionError("store touched")

    ctx = ToolContext(
        store=ExplodingStore(),
        brain=None,
        settings=_settings(business_ledger_test_enabled=False),
        principal=_owner(),
        embedding_port=None,
        private_owner_chat=True,
        source_ref="tg:1",
    )
    monkeypatch.setattr(
        "app.tools.owner.business_ledger.make_ledger",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("ledger constructed")),
    )
    assert _business_overview(ctx, {}).error == "business ledger is unavailable"
    assert _business_propose_payment(ctx, {}).error == "business ledger is unavailable"


def test_strict_payment_dto_requires_real_true_integer_and_canonical_timestamp():
    valid = _payment_parameters()
    for mutate in (
        lambda value: value.update(confirmed_received=1),
        lambda value: value["payment"].update(amount_minor=1.0),
        lambda value: value["payment"].update(received_at="2026-10-12T09:00:00Z"),
        lambda value: value.update(extra="x"),
    ):
        invalid = deepcopy(valid)
        mutate(invalid)
        with pytest.raises((ValidationError, ValueError)):
            validate_parameters("business.record_payment", invalid)


def test_payment_proposal_has_no_effect_and_card_shows_exact_manual_confirmation():
    store, session = _store()
    ledger = FakeLedger()
    try:
        proposal = propose_business_action(
            store,
            settings=_settings(),
            principal=_owner(),
            private_owner_chat=True,
            source_ref="tg:update:1",
            kind="business.record_payment",
            parameters=_payment_parameters(),
            ledger=ledger,
            now=datetime(2026, 10, 12, 9, tzinfo=UTC),
        )
        assert ledger.writes == []
        row = store.get_approval_by_approval_id(proposal.approval_id)
        envelope = read_owner_action(row)
        assert envelope is not None
        assert envelope["parameters"] == _payment_parameters()
        card = render_owner_proposal_card(envelope)
        assert CHARGE in card
        assert envelope["parameters"]["request_id"] in card
        assert "2500" in card and "אישור מאשר ידנית" in card
        assert "script_hash" not in card and "key_hash" not in card
        assert row.expires_at == "2026-10-13T09:00:00+00:00"
    finally:
        session.close()


def test_schedule_proposal_card_shows_exact_total_dates_amounts_and_ids():
    store, session = _store()
    ledger = FakeLedger()
    ledger.data["charges"] = []
    ledger.data["counts"]["charges"] = 0
    ledger.data["deals"][0]["payment_plan"] = "installments"
    try:
        request_id = stable_request_id(_owner(), "tg:schedule", "save_schedule")
        proposal = propose_business_action(
            store,
            settings=_settings(),
            principal=_owner(),
            private_owner_chat=True,
            source_ref="tg:schedule",
            kind="business.save_schedule",
            parameters={
                "request_id": request_id,
                "expected_revision": 2,
                "deal_id": DEAL,
                "number_of_charges": 3,
                "first_due_date": "2026-10-31",
                "charge_ids": [
                    stable_record_id(request_id, f"charge:{index}") for index in range(3)
                ],
            },
            ledger=ledger,
        )
        envelope = read_owner_action(store.get_approval_by_approval_id(proposal.approval_id))
        assert envelope is not None
        exact = envelope["target"]["prospective_charges"]
        assert [row["amount_minor"] for row in exact] == [3334, 3333, 3333]
        assert [row["due_date"] for row in exact] == [
            "2026-10-31",
            "2026-11-30",
            "2026-12-31",
        ]
        card = render_owner_proposal_card(envelope)
        assert all(row["id"] in card for row in exact)
        assert "3334" in card and "2026-11-30" in card
        assert ledger.writes == []
    finally:
        session.close()


@pytest.mark.parametrize(
    ("kind", "record_key", "record", "expected"),
    [
        (
            "business.save_deal",
            "deal",
            {
                "id": DEAL,
                "title": "Deal",
                "amount_minor": 10000,
                "currency": "ILS",
                "payment_plan": "full",
                "start_date": "2026-10-01",
                "status": "active",
                "project_id": None,
                "notes": "",
            },
            ("מזהה פרויקט", "ללא", "הערות", "ריק"),
        ),
        (
            "business.record_expense",
            "expense",
            {
                "id": str(uuid4()),
                "project_id": None,
                "amount_minor": 500,
                "currency": "ILS",
                "occurred_on": "2026-10-12",
                "notes": "",
            },
            ("מזהה פרויקט", "ללא", "הערות", "ריק"),
        ),
        (
            "business.save_project",
            "project",
            {
                "id": PROJECT,
                "title": "Project",
                "status": "completed",
                "deadline": None,
                "notes": "",
            },
            ("תאריך יעד", "ללא", "הערות", "ריק"),
        ),
    ],
)
def test_finance_cards_render_present_none_and_empty_clear_values(
    kind, record_key, record, expected
):
    parameters = {
        "request_id": str(uuid4()),
        "expected_revision": 1,
        record_key: record,
    }
    card = render_owner_proposal_card(
        {
            "kind": kind,
            "parameters": parameters,
            "target": {
                "record_id": record["id"],
                "expected_revision": 1,
                "source": "google_sheets",
                "mode": "TEST",
            },
            "version": 1,
        }
    )
    assert all(value in card for value in expected)


def test_payment_card_renders_explicit_manual_confirmation_field():
    parameters = _payment_parameters("tg:card-confirmed")
    card = render_owner_proposal_card(
        {
            "kind": "business.record_payment",
            "parameters": parameters,
            "target": {
                "record_id": parameters["payment"]["id"],
                "expected_revision": 1,
                "source": "google_sheets",
                "mode": "TEST",
            },
            "version": 1,
        }
    )
    assert "אישור שהכסף התקבל" in card
    assert "כן" in card


def test_overview_preserves_unknown_totals_instead_of_zero(monkeypatch):
    store, session = _store()
    ledger = FakeLedger()
    ledger.data["coverage"] = "unknown"
    ledger.data["reason_codes"] = ["financial_coverage_incomplete"]
    ledger.data["totals"] = {key: None for key in ledger.data["totals"]}
    ctx = ToolContext(
        store=store,
        brain=None,
        settings=_settings(),
        principal=_owner(),
        embedding_port=None,
        private_owner_chat=True,
        source_ref="tg:overview",
    )
    monkeypatch.setattr("app.tools.owner.business_ledger.make_ledger", lambda *_a: ledger)
    try:
        result = _business_overview(ctx, {})
        assert result.ok
        assert '"coverage":"unknown"' in result.text
        assert '"received_minor":null' in result.text
        assert '"period":{"as_of":"2026-10-12T09:00:00.000Z"}' in result.text
        outstanding = _business_outstanding(ctx, {"limit": 10})
        assert outstanding.ok
        assert '"outstanding_minor":null' in outstanding.text
        assert '"received_minor":null' in outstanding.text
    finally:
        session.close()


def test_long_escaped_rows_are_budgeted_as_whole_valid_json(monkeypatch):
    store, session = _store()
    ledger = FakeLedger()
    long_notes = '"\\' * 500
    ledger.data["deals"] = [
        {
            **ledger.data["deals"][0],
            "id": f"{index + 1:08x}-1111-4111-8111-{index + 1:012x}",
            "notes": long_notes,
        }
        for index in range(10)
    ]
    ledger.data["counts"]["deals"] = 10
    ctx = ToolContext(
        store=store,
        brain=None,
        settings=_settings(),
        principal=_owner(),
        embedding_port=None,
        private_owner_chat=True,
        source_ref="tg:long-read",
    )
    monkeypatch.setattr("app.tools.owner.business_ledger.make_ledger", lambda *_a: ledger)
    try:
        result = _business_deals(ctx, {"limit": 10})
        assert result.ok
        payload_text = result.payload()["result"]
        payload = json.loads(payload_text)
        assert len(payload_text) <= result.max_chars
        assert 0 < payload["returned"] < 10
        assert payload["total"] == 10
        assert payload["has_more"] is True
        assert payload["partial"] is True
        assert payload["result_reason_codes"] == ["result_budget_limited"]
        assert all(row["notes"] == long_notes for row in payload["items"])
    finally:
        session.close()


@pytest.mark.parametrize("swap", ["workspace", "script", "key"])
def test_approved_action_rejects_swapped_internal_binding(monkeypatch, swap):
    store, session = _store()
    ledger = FakeLedger()
    settings = _settings()
    try:
        proposal = propose_business_action(
            store,
            settings=settings,
            principal=_owner(),
            private_owner_chat=True,
            source_ref=f"tg:swap:{swap}",
            kind="business.record_payment",
            parameters=_payment_parameters(f"tg:swap:{swap}"),
            ledger=ledger,
        )
        decide_owner_action(
            store,
            principal=_owner(),
            approval_id=proposal.approval_id,
            decision=DECISION_APPROVED,
        )
        if swap == "workspace":
            settings.business_ledger_workspace_id = "workspace-other"
            settings.owner_os_workspace_id = "workspace-other"
        elif swap == "script":
            settings.business_ledger_script_url = (
                "https://script.google.com/macros/s/" + "y" * 24 + "/exec"
            )
        else:
            settings.business_ledger_hmac_secret = "different-test-secret-at-least-32-bytes"
        monkeypatch.setattr("app.services.business_ledger_actions.make_ledger", lambda *_a: ledger)
        result = execute_business_action(
            store, settings=settings, principal=_owner(), proposal_id=proposal.proposal_id
        )
        assert result.status == "target_changed"
        assert ledger.writes == []
    finally:
        session.close()


def test_stale_and_overpayment_rejected_before_proposal():
    store, session = _store()
    ledger = FakeLedger()
    try:
        before = len(store.list_all_pending_approvals())
        stale = _payment_parameters("tg:stale")
        stale["expected_charge_revision"] = 2
        with pytest.raises(ValueError, match="stale_or_overpaid"):
            propose_business_action(
                store,
                settings=_settings(),
                principal=_owner(),
                private_owner_chat=True,
                source_ref="tg:stale",
                kind="business.record_payment",
                parameters=stale,
                ledger=ledger,
            )
        over = _payment_parameters("tg:over")
        over["payment"]["amount_minor"] = 10001
        with pytest.raises(ValueError, match="stale_or_overpaid"):
            propose_business_action(
                store,
                settings=_settings(),
                principal=_owner(),
                private_owner_chat=True,
                source_ref="tg:over",
                kind="business.record_payment",
                parameters=over,
                ledger=ledger,
            )
        assert len(store.list_all_pending_approvals()) == before
    finally:
        session.close()


def test_approved_payment_executes_once_and_returns_real_receipt(monkeypatch):
    store, session = _store()
    ledger = FakeLedger()
    parameters = _payment_parameters("tg:execute")
    try:
        proposal = propose_business_action(
            store,
            settings=_settings(),
            principal=_owner(),
            private_owner_chat=True,
            source_ref="tg:execute",
            kind="business.record_payment",
            parameters=parameters,
            ledger=ledger,
        )
        decide_owner_action(
            store, principal=_owner(), approval_id=proposal.approval_id, decision=DECISION_APPROVED
        )
        monkeypatch.setattr("app.services.business_ledger_actions.make_ledger", lambda *_a: ledger)
        first = execute_business_action(
            store, settings=_settings(), principal=_owner(), proposal_id=proposal.proposal_id
        )
        second = execute_business_action(
            store, settings=_settings(), principal=_owner(), proposal_id=proposal.proposal_id
        )
        assert first.status == "executed"
        assert parameters["payment"]["id"] in first.text
        assert "44444444-4444-4444-8444-444444444444" in first.text
        assert second.status == "already_handled"
        assert len(ledger.writes) == 1
    finally:
        session.close()


def test_generic_owner_executor_dispatches_finance_to_scoped_adapter(monkeypatch):
    store, session = _store()
    ledger = FakeLedger()
    settings = _settings()
    try:
        proposal = propose_business_action(
            store,
            settings=settings,
            principal=_owner(),
            private_owner_chat=True,
            source_ref="tg:dispatch",
            kind="business.record_payment",
            parameters=_payment_parameters("tg:dispatch"),
            ledger=ledger,
        )
        decide_owner_action(
            store,
            principal=_owner(),
            approval_id=proposal.approval_id,
            decision=DECISION_APPROVED,
        )
        seen = []

        def scoped(*args, **kwargs):
            seen.append((args, kwargs))
            return OwnerActionExecution("executed", "scoped")

        monkeypatch.setattr("app.services.business_ledger_actions.execute_business_action", scoped)
        result = execute_approved_owner_action_with_adapters(
            store,
            settings=settings,
            principal=_owner(),
            proposal_id=proposal.proposal_id,
        )
        assert result == OwnerActionExecution("executed", "scoped")
        assert len(seen) == 1
    finally:
        session.close()


def test_mismatched_write_receipt_is_unknown_and_cannot_retry(monkeypatch):
    store, session = _store()
    ledger = FakeLedger()
    ledger.bad_receipt = True
    parameters = _payment_parameters("tg:bad-receipt")
    settings = _settings()
    try:
        proposal = propose_business_action(
            store,
            settings=settings,
            principal=_owner(),
            private_owner_chat=True,
            source_ref="tg:bad-receipt",
            kind="business.record_payment",
            parameters=parameters,
            ledger=ledger,
        )
        decide_owner_action(
            store,
            principal=_owner(),
            approval_id=proposal.approval_id,
            decision=DECISION_APPROVED,
        )
        monkeypatch.setattr("app.services.business_ledger_actions.make_ledger", lambda *_a: ledger)
        first = execute_business_action(
            store, settings=settings, principal=_owner(), proposal_id=proposal.proposal_id
        )
        second = execute_business_action(
            store, settings=settings, principal=_owner(), proposal_id=proposal.proposal_id
        )
        assert first.status == "unknown"
        assert second.status == "already_handled"
        assert len(ledger.writes) == 1
    finally:
        session.close()


def test_lost_response_never_reposts_and_approved_recovery_only_reads_receipt(monkeypatch):
    store, session = _store()
    ledger = FakeLedger()
    ledger.fail_after_commit = True
    parameters = _payment_parameters("tg:lost")
    settings = _settings()
    try:
        proposal = propose_business_action(
            store,
            settings=settings,
            principal=_owner(),
            private_owner_chat=True,
            source_ref="tg:lost",
            kind="business.record_payment",
            parameters=parameters,
            ledger=ledger,
        )
        decide_owner_action(
            store, principal=_owner(), approval_id=proposal.approval_id, decision=DECISION_APPROVED
        )
        monkeypatch.setattr("app.services.business_ledger_actions.make_ledger", lambda *_a: ledger)
        first = execute_business_action(
            store, settings=settings, principal=_owner(), proposal_id=proposal.proposal_id
        )
        retry = execute_business_action(
            store, settings=settings, principal=_owner(), proposal_id=proposal.proposal_id
        )
        assert first.status == "unknown" and retry.status == "already_handled"
        assert len(ledger.writes) == 1

        recovery_parameters = {
            "request_id": parameters["request_id"],
            "expected_id": parameters["payment"]["id"],
            "expected_revision": 1,
        }
        recovery = propose_business_action(
            store,
            settings=settings,
            principal=_owner(),
            private_owner_chat=True,
            source_ref="tg:recover",
            kind="business.recover_receipt",
            parameters=recovery_parameters,
            ledger=ledger,
        )
        decide_owner_action(
            store, principal=_owner(), approval_id=recovery.approval_id, decision=DECISION_APPROVED
        )
        recovered = execute_business_action(
            store, settings=settings, principal=_owner(), proposal_id=recovery.proposal_id
        )
        assert recovered.status == "executed"
        assert "44444444-4444-4444-8444-444444444444" in recovered.text
        assert len(ledger.writes) == 1
    finally:
        session.close()


def test_missing_receipt_recovery_stays_unknown_without_write(monkeypatch):
    store, session = _store()
    ledger = FakeLedger()
    request_id = str(uuid4())
    settings = _settings()
    try:
        proposal = propose_business_action(
            store,
            settings=settings,
            principal=_owner(),
            private_owner_chat=True,
            source_ref="tg:missing",
            kind="business.recover_receipt",
            parameters={
                "request_id": request_id,
                "expected_id": str(uuid4()),
                "expected_revision": 1,
            },
            ledger=ledger,
        )
        decide_owner_action(
            store, principal=_owner(), approval_id=proposal.approval_id, decision=DECISION_APPROVED
        )
        monkeypatch.setattr("app.services.business_ledger_actions.make_ledger", lambda *_a: ledger)
        result = execute_business_action(
            store, settings=settings, principal=_owner(), proposal_id=proposal.proposal_id
        )
        assert result.status == "unknown"
        assert ledger.writes == []
    finally:
        session.close()
