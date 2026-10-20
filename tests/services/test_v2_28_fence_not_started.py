"""V2-28 hand-offs from the V2-17 review (#176).

(a) A ``LedgerError`` from the backend factory is handled explicitly, with a reason code, and never
    escapes the Sheets card path.
(b) A Telegram approve or reject that the V2-17 Sheets fence refuses never dispatched anything,
    so it ends as a definite ``not_started`` (V2_PG_CONTRACTS.md section 5.2 write_outcome). The
    approval claim is released, never parked as ``pending_review``. A failure after dispatch stays
    unknown.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from app.capabilities.types import Principal
from app.core.config import MiaEnv, Settings
from app.core.owner_write import current_write_dispatch
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import DECISION_APPROVED, DECISION_REJECTED
from app.services import assaf_os as assaf_os_module
from app.services.assaf_os import AssafOsError
from app.services.os_ledger import api as ledger_api
from app.services.owner_actions import (
    decide_owner_action,
    execute_approved_owner_action_with_adapters,
    reject_assaf_os_owner_action,
)
from app.tools.owner.assaf_os import _assaf_os_propose
from app.tools.owner.types import ToolContext

OWNER = "123456"
REQUEST = "12345678-1234-4234-8234-123456789012"
APPROVAL_REQUEST = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REJECTION_REQUEST = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
REAL_PORT = assaf_os_module.AssafOsPort


def settings(**changes):
    values = dict(
        _env_file=None,
        env=MiaEnv.TEST,
        telegram_owner_user_ids=OWNER,
        owner_os_enabled=True,
        assaf_os_enabled=True,
        assaf_os_script_url="https://script.google.com/macros/s/" + "x" * 24 + "/exec",
        assaf_os_workbook_id="workbook_12345678901234567890",
        assaf_os_workspace_id="workspace-test",
        assaf_os_hmac_secret="assaf-os-test-secret-at-least-32-bytes",
        assaf_os_mode="TEST",
    )
    values.update(changes)
    return Settings(**values)


def pending():
    return {
        "api_version": "assaf-os.v1",
        "source": "google_sheets",
        "actor_source": "MIA",
        "mode": "TEST",
        "workbook_id": "workbook_12345678901234567890",
        "workspace_id": "workspace-test",
        "approval_guard_version": 1,
        "status": "pending",
        "proposal_id": "PR_12345678901234567890",
        "request_id": REQUEST,
        "approval_request_id": APPROVAL_REQUEST,
        "rejection_request_id": REJECTION_REQUEST,
        "expires_at": "2026-10-12T09:10:00+00:00",
        "entity": "clients",
        "record_id": None,
        "expected_revision": 0,
        "input": {"name": "Dana", "email": "dana@example.com", "status": "פעיל"},
        "before": None,
        "after": {"person_id": "P_123", "name": "Dana"},
    }


@pytest.fixture
def store():
    init_db()
    session = get_session_factory()()
    try:
        yield LeadStore(session)
    finally:
        session.rollback()
        session.close()


def _approved_card(store, monkeypatch, decide, *, tag, decision=DECISION_APPROVED):
    """A Sheets card proposed by Mia and decided by the owner; ``decide`` is the port's decide."""

    class Port:
        def __init__(self, *_args, **kwargs):
            self.source = kwargs["actor_source"]

        def propose(self, _request):
            return pending()

        def proposal(self, _proposal_id):
            return pending()

        def decide(self, operation, proposal_id, request):
            return decide(self.source, operation, proposal_id, request)

    monkeypatch.setattr("app.services.assaf_os.AssafOsPort", Port)
    monkeypatch.setattr(
        "app.services.owner_actions._aware",
        lambda value: value or datetime(2026, 10, 12, 9, 2, tzinfo=UTC),
    )
    actor = Principal.owner(source="telegram", actor_id=OWNER)
    proposed = _assaf_os_propose(
        ToolContext(
            store=store,
            brain=None,
            settings=settings(),
            embedding_port=None,
            principal=actor,
            private_owner_chat=True,
            source_ref=f"tg:v2-28-fence:{tag}",
            owner_text="create Dana dana@example.com",
            now=datetime(2026, 10, 12, 9, tzinfo=UTC),
        ),
        {
            "request_id": REQUEST,
            "entity": "clients",
            "record_id": None,
            "expected_revision": 0,
            "input_json": '{"name":"Dana","email":"dana@example.com","status":"פעיל"}',
        },
    )
    decided = decide_owner_action(
        store,
        principal=actor,
        approval_id=proposed.approval_id,
        decision=decision,
        now=datetime(2026, 10, 12, 9, 1, tzinfo=UTC),
    )
    return actor, decided.proposal_id


def _real_fenced_decide(monkeypatch):
    """The real AssafOsPort.decide behind an active ledger authority. Nothing may be posted."""

    def no_post(*_args, **_kwargs):
        raise AssertionError("the fence must refuse before anything is posted")

    monkeypatch.setattr(assaf_os_module, "_post", no_post)
    monkeypatch.setattr(ledger_api, "core_authority_state", lambda _settings: "active")
    calls = []

    def decide(source, operation, proposal_id, request):
        calls.append((source, operation))
        real = REAL_PORT(settings(), OWNER, actor_source=source)
        return real.decide(operation, proposal_id, request)

    return decide, calls


def _claim(store, proposal_id, suffix):
    return store.get_provider_write_status(scope="approval", key=f"{proposal_id}:{suffix}")


def test_the_real_fence_refuses_before_the_dispatch_counter_moves(monkeypatch):
    # The premise of (b): the V2-17 fence raises before WriteDispatch.dispatched moves.
    from app.core.owner_write import WriteDispatch
    from app.services.assaf_os import DecisionRequest

    decide, _ = _real_fenced_decide(monkeypatch)
    with WriteDispatch("approve").activate() as dispatch:
        with pytest.raises(AssafOsError) as refused:
            decide("TELEGRAM_APPROVAL", "approve", pending()["proposal_id"],
                   DecisionRequest(request_id=APPROVAL_REQUEST))
    assert (refused.value.code, refused.value.status) == ("authority_moved", 409)
    assert dispatch.dispatched == 0 and dispatch.not_started


def test_a_fenced_telegram_approve_is_not_started_and_releases_its_claim(
    store, monkeypatch, caplog
):
    decide, calls = _real_fenced_decide(monkeypatch)
    actor, proposal_id = _approved_card(store, monkeypatch, decide, tag="approve")
    caplog.set_level(logging.INFO, logger="mia.owner_actions")
    first = execute_approved_owner_action_with_adapters(
        store, settings=settings(), principal=actor, proposal_id=proposal_id
    )
    assert first.status == "not_started"
    assert _claim(store, proposal_id, "execute") == ""  # released, never pending_review
    row = store.get_approval_by_resource("owner_proposal", proposal_id, "owner_external_write")
    assert not row.executed_at
    # A later tap is decided afresh (still fenced here), never a sticky unknown.
    second = execute_approved_owner_action_with_adapters(
        store, settings=settings(), principal=actor, proposal_id=proposal_id
    )
    assert second.status == "not_started"
    assert calls == [("TELEGRAM_APPROVAL", "approve")] * 2
    lines = [r.getMessage() for r in caplog.records if "not_started" in r.getMessage()]
    assert lines and all("reason=authority_moved" in line for line in lines)
    assert not any(pending()["proposal_id"] in r.getMessage() for r in caplog.records)


def test_a_fenced_telegram_reject_is_not_started_and_releases_its_claim(store, monkeypatch):
    decide, calls = _real_fenced_decide(monkeypatch)
    actor, proposal_id = _approved_card(
        store, monkeypatch, decide, tag="reject", decision=DECISION_REJECTED
    )
    outcome = reject_assaf_os_owner_action(
        store, settings=settings(), principal=actor, proposal_id=proposal_id
    )
    assert outcome.status == "not_started"
    assert _claim(store, proposal_id, "reject") == ""
    again = reject_assaf_os_owner_action(
        store, settings=settings(), principal=actor, proposal_id=proposal_id
    )
    assert again.status == "not_started"
    assert calls == [("TELEGRAM_APPROVAL", "reject")] * 2


@pytest.mark.parametrize("operation", ["approve", "reject"])
@pytest.mark.parametrize(
    ("dispatched", "code"),
    [
        (1, "source_unavailable"),  # a failure after the counter moved is uncertain
        (1, "authority_moved"),  # even a fence code is not proof once anything was dispatched
        (0, "transport lost after commit"),  # no counter proof without a fence refusal code
    ],
)
def test_anything_but_a_pre_dispatch_fence_refusal_stays_unknown(
    store, monkeypatch, operation, dispatched, code
):
    def decide(_source, _operation, _proposal_id, _request):
        current_write_dispatch().dispatched += dispatched
        raise AssafOsError(code)

    actor, proposal_id = _approved_card(
        store, monkeypatch, decide, tag=f"after-{operation}-{dispatched}-{code}",
        decision=DECISION_APPROVED if operation == "approve" else DECISION_REJECTED,
    )
    if operation == "approve":
        outcome = execute_approved_owner_action_with_adapters(
            store, settings=settings(), principal=actor, proposal_id=proposal_id
        )
        key = "execute"
    else:
        outcome = reject_assaf_os_owner_action(
            store, settings=settings(), principal=actor, proposal_id=proposal_id
        )
        key = "reject"
    assert outcome.status == "unknown"
    assert _claim(store, proposal_id, key) == "pending_review"


def test_a_ledger_error_from_the_factory_is_not_connected_with_a_reason_code(
    store, monkeypatch, caplog
):
    # (a) The Sheets card's connection check handles a LedgerError explicitly.
    def decide(*_args):
        raise AssertionError("never reached: the card is not connected")

    actor, proposal_id = _approved_card(store, monkeypatch, decide, tag="ledger-error")

    def factory(*_args, **_kwargs):
        raise ledger_api.LedgerError("ledger_unavailable")

    monkeypatch.setattr("app.services.os_backend.get_os_backend", factory)
    caplog.set_level(logging.INFO, logger="mia.owner_actions")
    outcome = execute_approved_owner_action_with_adapters(
        store, settings=settings(), principal=actor, proposal_id=proposal_id
    )
    assert outcome.status == "not_connected"
    assert _claim(store, proposal_id, "execute") == ""
    assert any(
        "op=connect" in r.getMessage() and "reason=ledger_unavailable" in r.getMessage()
        for r in caplog.records
    )


def test_a_sheets_lead_recovery_under_postgres_refuses_without_reading_the_ledger(monkeypatch):
    # (a) Under postgres the backend is the ledger, which cannot prove an Apps Script no-write.
    # The recovery refuses explicitly instead of letting a LedgerError escape.
    from app.services.assaf_os_leads import (
        prepare_selected_crm_lead,
        recover_expired_approved_crm_lead,
    )

    from tests.unit import test_website_lead_approval as sheets_leads

    class Port(sheets_leads.FakeAssafOsPort):
        # Own class-level lists, so the shared fake's state never leaks into other modules.
        proposals: list[object] = []
        snapshot_rows: list[dict] = []
        decisions: list[object] = []
        current_proposal: dict = {}

    monkeypatch.setattr("app.services.assaf_os.AssafOsPort", Port)
    init_db()
    session = get_session_factory()()
    try:
        lead_store = LeadStore(session)
        sheets_leads._seed_source(lead_store)
        principal = Principal.owner(source="telegram", actor_id=sheets_leads.OWNER)
        first = prepare_selected_crm_lead(
            lead_store, settings=sheets_leads._settings(), principal=principal,
            contact_id="crm-contact-1", conversation_id="site-current", path="לימוד",
            summary="", next_step="",
        )
        old = lead_store.get_approval_by_approval_id(first.approval_id)
        old.decision = DECISION_APPROVED
        old.expires_at = "2020-01-01T00:00:00+00:00"

        def no_backend(*_args, **_kwargs):
            raise AssertionError("no backend is built for a Sheets card under postgres")

        monkeypatch.setattr("app.services.assaf_os_leads.get_os_backend", no_backend)
        with pytest.raises(ValueError, match="remote_no_write_unproven"):
            recover_expired_approved_crm_lead(
                lead_store, settings=sheets_leads._settings(assaf_os_backend="postgres"),
                principal=principal, approval_id=first.approval_id, path="לימוד",
                summary="fresh review", next_step="",
            )
        assert old.result in {None, ""}
    finally:
        session.rollback()
        session.close()
