import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from app.api.inbound import process_inbound_texts
from app.core.risk import RiskLevel
from app.core.write_flags import named_write_may_auto
from app.db.models import ApprovalRow, CanonicalEventRow, IdempotencyRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import (
    ACTION_CAMPAIGN_WRITE,
    ACTION_PROPOSAL_HANDOFF,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    RESOURCE_CAMPAIGN,
    RESOURCE_LEAD,
    RISK_R3,
    RISK_R4,
    OwnerApprovalResult,
    ack_for_approval_result,
    apply_approval_policy,
    apply_campaign_write_approval_policy,
    apply_owner_approval_decision,
    approval_expires_at,
    is_approval_expired,
    payload_hash,
    proposed_parameters_json,
    resource_hash_matches,
)
from app.domain.events import Channel, EventType
from app.domain.sales import NextAction, SalesState
from app.integrations.base import RecordingMessagePort
from app.integrations.calendar import DisabledCalendarPort
from app.integrations.sheets import DisabledSheetsPort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import func, select

PROSPECT_PHONE = "972509995011"
PROSPECT_PHONE_2 = "972509995012"
PROSPECT_PHONE_APPROVAL = "972509995013"
OWNER_PHONE_APPROVAL = "972509990050"
OWNER_PHONE_CAMPAIGN = "972509998101"
CAMPAIGN_PERSIST = "120339980001"
CAMPAIGN_DUP = "120339980002"
CAMPAIGN_APPROVE = "120339980003"
CAMPAIGN_VS_LEAD = "120339980004"
CAMPAIGN_EXPIRED = "120339980005"
CAMPAIGN_TAMPER = "120339980006"
CAMPAIGN_AMBIG_A = "120339980007"
CAMPAIGN_AMBIG_B = "120339980008"
CAMPAIGN_OBJECT_A = "120339980401"
CAMPAIGN_OBJECT_B = "120339980402"
CAMPAIGN_OBJECT_C = "120339980403"
CAMPAIGN_CLAIM_A = "120339980501"
CAMPAIGN_CLAIM_B = "120339980502"

_APPROVAL_PAYLOAD_KEYS = frozenset({"action", "risk", "decision"})
_IDENTITY_PAYLOAD_KEYS = frozenset(
    {"action", "channel", "resource_id", "resource_type", "risk"}
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_APR_ID = re.compile(r"^apr_[0-9a-f]{12}$")


def _expected_proposed_parameters(
    *,
    action: str,
    risk: str,
    channel: str,
    resource_type: str,
    resource_id: str,
) -> str:
    return proposed_parameters_json(
        action=action,
        risk=risk,
        channel=channel,
        resource_type=resource_type,
        resource_id=resource_id,
    )


def _assert_execute_fields_empty(row: ApprovalRow) -> None:
    assert row.executed_at == ""
    assert row.execution_operation_id == ""
    assert row.result == ""


def _assert_reserved_identity_empty(row: ApprovalRow) -> None:
    assert row.business_id == ""
    assert row.actor_id == ""


def _run_clinic_funnel_to_meeting(client: TestClient, session_id: str) -> str:
    messages = [
        "We run a clinic and miss calls all day.",
        "ok that's right",
        "I decide this quarter",
        "let's book a meeting",
    ]
    lead_id = ""
    for text in messages:
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": text},
        )
        assert response.status_code == 200
        body = response.json()
        lead_id = body["lead_id"]
    assert body["next_action"] == "offer_meeting"
    return lead_id


def test_website_proposal_handoff_creates_pending_approval() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "Please send me a proposal"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["next_action"] == "handoff"
        lead_id = body["lead_id"]
    db = get_session_factory()()
    try:
        row = db.scalars(
            select(ApprovalRow).where(ApprovalRow.lead_id == lead_id)
        ).one()
        assert row.action == ACTION_PROPOSAL_HANDOFF
        assert row.risk == RISK_R3
        assert row.decision == DECISION_PENDING
        assert row.approver == ""
        assert row.channel == Channel.WEBSITE.value
        assert row.resource_type == RESOURCE_LEAD
        assert row.resource_id == lead_id
        assert row.expires_at
        assert not is_approval_expired(row, now=datetime.now(UTC))
        assert _HEX64.match(row.payload_hash)
        expected_hash = payload_hash(
            action=ACTION_PROPOSAL_HANDOFF,
            risk=RISK_R3,
            channel=Channel.WEBSITE.value,
            resource_type=RESOURCE_LEAD,
            resource_id=lead_id,
        )
        assert row.payload_hash == expected_hash
        assert resource_hash_matches(row)
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == EventType.APPROVAL_REQUIRED.value,
                )
            )
        )
        assert len(events) == 1
        assert events[0].provider_event_id == f"{lead_id}:approval:proposal_handoff"
        event_payload = json.loads(events[0].payload_json)
        assert set(event_payload.keys()) == _APPROVAL_PAYLOAD_KEYS
        assert event_payload == {
            "action": ACTION_PROPOSAL_HANDOFF,
            "risk": RISK_R3,
            "decision": DECISION_PENDING,
        }
        assert "@" not in events[0].payload_json
    finally:
        db.close()


def test_second_proposal_message_still_one_approval_row() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        first = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "Please send me a proposal"},
        )
        assert first.json()["next_action"] == "handoff"
        lead_id = first.json()["lead_id"]
        again = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "Please send me a proposal"},
        )
        assert again.json()["next_action"] == "handoff"
    db = get_session_factory()()
    try:
        rows = list(
            db.scalars(select(ApprovalRow).where(ApprovalRow.lead_id == lead_id))
        )
        assert len(rows) == 1
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == EventType.APPROVAL_REQUIRED.value,
                )
            )
        )
        assert len(events) == 1
    finally:
        db.close()


def test_kill_switch_skips_approval_create(monkeypatch) -> None:
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_kill_approval_1"
        )
        sales = SalesState(
            lead_id=lead_id,
            workflow_known=True,
            owner_required=True,
        )
        store.save_sales(sales)
        db.commit()
        apply_approval_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.HANDOFF.value,
            sales=sales,
            kill_switch=True,
        )
        db.commit()
        assert store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF) is None
    finally:
        db.close()


def test_upsert_approval_rejects_unknown_action() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_approval_guard_1"
        )
        store.upsert_approval(
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            action="meta_budget",
            risk=RISK_R3,
            payload_hash="a" * 64,
            decision=DECISION_PENDING,
            resource_type=RESOURCE_LEAD,
            resource_id=lead_id,
            expires_at=approval_expires_at(now=datetime.now(UTC)),
        )
        db.commit()
        assert store.get_approval(lead_id, "meta_budget") is None
        assert store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF) is None
    finally:
        db.close()


def test_apply_approval_policy_wrong_action_or_no_owner_required() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_approval_skip_1"
        )
        sales = SalesState(lead_id=lead_id, workflow_known=True, owner_required=True)
        store.save_sales(sales)
        apply_approval_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.QUALIFY.value,
            sales=sales,
            kill_switch=False,
        )
        assert store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF) is None
        sales_no_owner = SalesState(lead_id=lead_id, workflow_known=True)
        apply_approval_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.HANDOFF.value,
            sales=sales_no_owner,
            kill_switch=False,
        )
        assert store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF) is None
    finally:
        db.close()


def test_apply_approval_policy_never_calls_message_port() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE
        )
        sales = SalesState(
            lead_id=lead_id,
            workflow_known=True,
            owner_required=True,
        )
        store.save_sales(sales)
        port = RecordingMessagePort()
        apply_approval_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WHATSAPP,
            action=NextAction.HANDOFF.value,
            sales=sales,
            kill_switch=False,
        )
        db.commit()
        assert port.sent == []
        assert store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF) is not None
    finally:
        db.close()


def test_clinic_funnel_to_meeting_no_approval_row() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        lead_id = _run_clinic_funnel_to_meeting(client, session_id)
    db = get_session_factory()()
    try:
        row = db.scalars(
            select(ApprovalRow).where(ApprovalRow.lead_id == lead_id)
        ).one_or_none()
        assert row is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_whatsapp_proposal_creates_approval() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": "wamid.approval.1",
                "from": PROSPECT_PHONE_2,
                "text": "send me a proposal",
            }],
            store=store,
            port=port,
            kill_switch=False,
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE_2
        )
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        assert row.decision == DECISION_PENDING
        assert row.channel == Channel.WHATSAPP.value
    finally:
        db.close()


def _seed_pending_approval(
    store: LeadStore, *, external_id: str
) -> tuple[str, str]:
    _, lead_id = store.open_channel_lead(
        channel=Channel.WHATSAPP, external_id=external_id
    )
    sales = SalesState(
        lead_id=lead_id,
        workflow_known=True,
        owner_required=True,
    )
    store.save_sales(sales)
    apply_approval_policy(
        store,
        lead_id=lead_id,
        channel=Channel.WHATSAPP,
        action=NextAction.HANDOFF.value,
        sales=sales,
        kill_switch=False,
    )
    return external_id, lead_id


def test_list_pending_approvals_rejects_unknown_action() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _seed_pending_approval(store, external_id="972509995024")
        db.commit()
        assert store.list_pending_approvals(action="meta_budget") == []
        assert store.decide_approval(
            lead_id="lead_deadbeefdead",
            action="meta_budget",
            decision=DECISION_APPROVED,
        ) is False
    finally:
        db.close()


def test_apply_owner_approval_decision_approves_pending_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = _seed_pending_approval(store, external_id="972509995020")
        port = RecordingMessagePort()
        result = apply_owner_approval_decision(
            store,
            text=f"approve the proposal {lead_id}",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert result.status == "decided"
        assert result.decision == DECISION_APPROVED
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        assert row.decision == DECISION_APPROVED
        assert row.approver == ""
        assert port.sent == []
    finally:
        db.close()


def test_apply_owner_approval_decision_rejects_pending_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = _seed_pending_approval(store, external_id="972509995021")
        result = apply_owner_approval_decision(
            store,
            text=f"reject the proposal {lead_id}",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert result.status == "decided"
        assert result.decision == DECISION_REJECTED
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        assert row.decision == DECISION_REJECTED
        assert row.approver == ""
    finally:
        db.close()


def test_apply_owner_approval_decision_kill_switch_skipped_stays_pending() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = _seed_pending_approval(store, external_id="972509995022")
        result = apply_owner_approval_decision(
            store,
            text=f"approve the proposal {lead_id}",
            channel=Channel.WHATSAPP,
            kill_switch=True,
        )
        db.commit()
        assert result.status == "skipped"
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        assert row.decision == DECISION_PENDING
    finally:
        db.close()


def test_apply_owner_approval_decision_already_decided_unchanged() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = _seed_pending_approval(store, external_id="972509995023")
        first = apply_owner_approval_decision(
            store,
            text=f"approve the proposal {lead_id}",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert first.status == "decided"
        second = apply_owner_approval_decision(
            store,
            text=f"reject the proposal {lead_id}",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert second.status == "already_decided"
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        assert row.decision == DECISION_APPROVED
        assert ack_for_approval_result(second) == (
            "הבקשה כבר טופלה. לא שיניתי כלום."
        )
    finally:
        db.close()


def test_apply_approval_policy_sets_resource_binding_and_hash() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_appr_exp_1"
        )
        sales = SalesState(
            lead_id=lead_id,
            workflow_known=True,
            owner_required=True,
        )
        store.save_sales(sales)
        before = datetime.now(UTC)
        apply_approval_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.HANDOFF.value,
            sales=sales,
            kill_switch=False,
        )
        db.commit()
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        assert row.resource_type == RESOURCE_LEAD
        assert row.resource_id == lead_id
        assert row.expires_at
        expires = datetime.fromisoformat(row.expires_at)
        assert expires > before
        expected_hash = payload_hash(
            action=ACTION_PROPOSAL_HANDOFF,
            risk=RISK_R3,
            channel=Channel.WEBSITE.value,
            resource_type=RESOURCE_LEAD,
            resource_id=lead_id,
        )
        assert row.payload_hash == expected_hash
        assert resource_hash_matches(row)
    finally:
        db.close()


def test_owner_decide_after_expiry_stays_pending() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = _seed_pending_approval(store, external_id="972509995080")
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        row.expires_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        db.commit()
        result = apply_owner_approval_decision(
            store,
            text=f"approve the proposal {lead_id}",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert result.status == "expired"
        assert store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF).decision == DECISION_PENDING
    finally:
        db.close()


def test_owner_decide_tampered_payload_hash_unbound() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = _seed_pending_approval(store, external_id="972509995081")
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        row.payload_hash = "b" * 64
        db.commit()
        result = apply_owner_approval_decision(
            store,
            text=f"approve the proposal {lead_id}",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert result.status == "unbound"
        assert store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF).decision == DECISION_PENDING
    finally:
        db.close()


def test_owner_decide_tampered_resource_id_unbound() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_a = _seed_pending_approval(store, external_id="972509995082")
        _, lead_b = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id="972509995083"
        )
        row = store.get_approval(lead_a, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        row.resource_id = lead_b
        db.commit()
        result = apply_owner_approval_decision(
            store,
            text=f"approve the proposal {lead_a}",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert result.status == "unbound"
        assert store.get_approval(lead_a, ACTION_PROPOSAL_HANDOFF).decision == DECISION_PENDING
    finally:
        db.close()


def test_owner_decide_empty_expires_at_expired() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = _seed_pending_approval(store, external_id="web_appr_unb_1")
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        row.expires_at = ""
        db.commit()
        result = apply_owner_approval_decision(
            store,
            text=f"approve the proposal {lead_id}",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert result.status == "expired"
        assert store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF).decision == DECISION_PENDING
    finally:
        db.close()


def test_owner_decide_skips_expired_pending_with_lead_id() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_valid = _seed_pending_approval(store, external_id="972509995084")
        _, lead_expired = _seed_pending_approval(store, external_id="972509995085")
        expired_row = store.get_approval(lead_expired, ACTION_PROPOSAL_HANDOFF)
        assert expired_row is not None
        expired_row.expires_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        db.commit()
        expired_result = apply_owner_approval_decision(
            store,
            text=f"approve the proposal {lead_expired}",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert expired_result.status == "expired"
        valid_result = apply_owner_approval_decision(
            store,
            text=f"approve the proposal {lead_valid}",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert valid_result.status == "decided"
        assert valid_result.lead_id == lead_valid
        valid_row = store.get_approval(lead_valid, ACTION_PROPOSAL_HANDOFF)
        assert valid_row is not None
        assert valid_row.decision == DECISION_APPROVED
        expired_still_pending = store.get_approval(
            lead_expired, ACTION_PROPOSAL_HANDOFF
        )
        assert expired_still_pending is not None
        assert expired_still_pending.decision == DECISION_PENDING
    finally:
        db.close()


def test_ack_for_expired_and_unbound() -> None:
    expired = ack_for_approval_result(
        OwnerApprovalResult(status="expired", lead_id="lead_deadbeefdead")
    )
    unbound = ack_for_approval_result(
        OwnerApprovalResult(status="unbound", lead_id="lead_deadbeefdead")
    )
    assert expired == "הבקשה פגה. לא ביצעתי כלום."
    assert unbound == "האישור לא תואם למשאב. לא ביצעתי כלום."


@pytest.mark.asyncio
async def test_owner_inbound_approve_proposal_persist_only() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": "wamid.approval.prospect.1",
                "from": PROSPECT_PHONE_APPROVAL,
                "text": "send me a proposal",
            }],
            store=store,
            port=port,
            kill_switch=False,
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE_APPROVAL
        )
        pending = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert pending is not None
        assert pending.decision == DECISION_PENDING
        port.sent.clear()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": "wamid.approval.owner.1",
                "from": OWNER_PHONE_APPROVAL,
                "text": f"approve the proposal {lead_id}",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE_APPROVAL},
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        assert row.decision == DECISION_APPROVED
        assert row.approver == ""
        assert len(port.sent) == 1
        assert "לא שלחתי" in port.sent[0].text
        assert "@" not in port.sent[0].text
        assert lead_id not in port.sent[0].text
    finally:
        db.close()


def _campaign_approval_row(db, campaign_id: str) -> ApprovalRow | None:
    return db.scalars(
        select(ApprovalRow).where(
            ApprovalRow.resource_type == RESOURCE_CAMPAIGN,
            ApprovalRow.resource_id == campaign_id,
            ApprovalRow.action == ACTION_CAMPAIGN_WRITE,
        )
    ).one_or_none()


@pytest.mark.asyncio
async def test_campaign_pause_request_persists_pending_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": "wamid.camp.appr.1",
                "from": OWNER_PHONE_CAMPAIGN,
                "text": f"pause campaign {CAMPAIGN_PERSIST}",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE_CAMPAIGN},
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        row = _campaign_approval_row(db, CAMPAIGN_PERSIST)
        assert row is not None
        assert row.action == ACTION_CAMPAIGN_WRITE
        assert row.risk == RISK_R4
        assert row.decision == DECISION_PENDING
        assert row.lead_id is None
        assert row.resource_type == RESOURCE_CAMPAIGN
        assert row.resource_id == CAMPAIGN_PERSIST
        assert row.expires_at
        expected_hash = payload_hash(
            action=ACTION_CAMPAIGN_WRITE,
            risk=RISK_R4,
            channel=Channel.WHATSAPP.value,
            resource_type=RESOURCE_CAMPAIGN,
            resource_id=CAMPAIGN_PERSIST,
        )
        assert row.payload_hash == expected_hash
        assert len(port.sent) == 1
        assert "לא שיניתי מודעות במטא" in port.sent[0].text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_campaign_duplicate_request_first_write_wins() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        for event_id in ("wamid.camp.appr.2a", "wamid.camp.appr.2b"):
            await process_inbound_texts(
                provider="whatsapp",
                channel=Channel.WHATSAPP,
                items=[{
                    "id": event_id,
                    "from": OWNER_PHONE_CAMPAIGN,
                    "text": f"pause campaign {CAMPAIGN_DUP}",
                }],
                store=store,
                port=port,
                kill_switch=False,
                owner_ids={OWNER_PHONE_CAMPAIGN},
                calendar=DisabledCalendarPort(),
                sheets=DisabledSheetsPort(),
            )
        db.commit()
        rows = list(
            db.scalars(
                select(ApprovalRow).where(
                    ApprovalRow.resource_type == RESOURCE_CAMPAIGN,
                    ApprovalRow.resource_id == CAMPAIGN_DUP,
                )
            )
        )
        assert len(rows) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_campaign_approve_decides_without_meta_write(monkeypatch) -> None:
    monkeypatch.setenv("MIA_META_WRITE", "false")
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": "wamid.camp.appr.3a",
                "from": OWNER_PHONE_CAMPAIGN,
                "text": f"pause campaign {CAMPAIGN_APPROVE}",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE_CAMPAIGN},
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        port.sent.clear()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": "wamid.camp.appr.3b",
                "from": OWNER_PHONE_CAMPAIGN,
                "text": f"approve campaign {CAMPAIGN_APPROVE}",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE_CAMPAIGN},
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        row = _campaign_approval_row(db, CAMPAIGN_APPROVE)
        assert row is not None
        assert row.decision == DECISION_APPROVED
        assert named_write_may_auto(
            enabled=True, risk=RiskLevel.R4_FINANCIAL_MARKETING
        ) is False
        assert len(port.sent) == 1
        assert "לא שיניתי מודעות במטא" in port.sent[0].text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_lead_proposal_approve_does_not_decide_campaign_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        apply_campaign_write_approval_policy(
            store,
            campaign_id=CAMPAIGN_VS_LEAD,
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        _, lead_id = _seed_pending_approval(store, external_id="972509998102")
        db.commit()
        result = apply_owner_approval_decision(
            store,
            text=f"approve the proposal {lead_id}",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert result.status == "decided"
        assert result.lead_id == lead_id
        campaign_row = _campaign_approval_row(db, CAMPAIGN_VS_LEAD)
        assert campaign_row is not None
        assert campaign_row.decision == DECISION_PENDING
    finally:
        db.close()


def test_campaign_expired_row_stays_pending() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        apply_campaign_write_approval_policy(
            store,
            campaign_id=CAMPAIGN_EXPIRED,
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        row = store.get_approval_by_resource(
            RESOURCE_CAMPAIGN, CAMPAIGN_EXPIRED, ACTION_CAMPAIGN_WRITE
        )
        assert row is not None
        row.expires_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        db.commit()
        result = apply_owner_approval_decision(
            store,
            text=f"approve campaign {CAMPAIGN_EXPIRED}",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert result.status == "expired"
        assert _campaign_approval_row(db, CAMPAIGN_EXPIRED).decision == DECISION_PENDING
    finally:
        db.close()


def test_campaign_hash_tamper_unbound() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        apply_campaign_write_approval_policy(
            store,
            campaign_id=CAMPAIGN_TAMPER,
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        row = store.get_approval_by_resource(
            RESOURCE_CAMPAIGN, CAMPAIGN_TAMPER, ACTION_CAMPAIGN_WRITE
        )
        assert row is not None
        row.payload_hash = "c" * 64
        db.commit()
        result = apply_owner_approval_decision(
            store,
            text=f"approve campaign {CAMPAIGN_TAMPER}",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert result.status == "unbound"
        assert _campaign_approval_row(db, CAMPAIGN_TAMPER).decision == DECISION_PENDING
    finally:
        db.close()


@pytest.mark.asyncio
async def test_campaign_missing_id_clarification_no_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        before = db.scalar(
            select(func.count())
            .select_from(ApprovalRow)
            .where(ApprovalRow.resource_type == RESOURCE_CAMPAIGN)
        )
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": "wamid.camp.appr.4",
                "from": OWNER_PHONE_CAMPAIGN,
                "text": "pause campaign",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE_CAMPAIGN},
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        after = db.scalar(
            select(func.count())
            .select_from(ApprovalRow)
            .where(ApprovalRow.resource_type == RESOURCE_CAMPAIGN)
        )
        assert after == before
        assert len(port.sent) == 1
        assert "מה מזהה הקמפיין" in port.sent[0].text
    finally:
        db.close()


def test_campaign_two_ids_ambiguous_no_persist() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        result = apply_owner_approval_decision(
            store,
            text=f"pause campaign {CAMPAIGN_AMBIG_A} and {CAMPAIGN_AMBIG_B}",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert result.status == "ambiguous"
        assert _campaign_approval_row(db, CAMPAIGN_AMBIG_A) is None
        assert _campaign_approval_row(db, CAMPAIGN_AMBIG_B) is None
    finally:
        db.close()


def test_lead_pending_row_has_approval_object_fields() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = _seed_pending_approval(store, external_id="972509995401")
        db.commit()
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        assert _APR_ID.match(row.approval_id)
        assert len(row.approval_id) == 16
        _assert_reserved_identity_empty(row)
        assert row.approved_at == ""
        _assert_execute_fields_empty(row)
        expected_params = _expected_proposed_parameters(
            action=ACTION_PROPOSAL_HANDOFF,
            risk=RISK_R3,
            channel=Channel.WHATSAPP.value,
            resource_type=RESOURCE_LEAD,
            resource_id=lead_id,
        )
        assert row.proposed_parameters == expected_params
        parsed = json.loads(row.proposed_parameters)
        assert set(parsed.keys()) == _IDENTITY_PAYLOAD_KEYS
        assert row.payload_hash == payload_hash(**parsed)
    finally:
        db.close()


def test_lead_pending_reupsert_preserves_approval_id() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = _seed_pending_approval(store, external_id="972509995402")
        db.commit()
        first = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert first is not None
        first_id = first.approval_id
        sales = store.get_sales(lead_id)
        apply_approval_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.HANDOFF.value,
            sales=sales,
            kill_switch=False,
        )
        db.commit()
        second = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert second is not None
        assert second.approval_id == first_id
        expected_params = _expected_proposed_parameters(
            action=ACTION_PROPOSAL_HANDOFF,
            risk=RISK_R3,
            channel=Channel.WEBSITE.value,
            resource_type=RESOURCE_LEAD,
            resource_id=lead_id,
        )
        assert second.proposed_parameters == expected_params
    finally:
        db.close()


def test_lead_approve_stamps_approved_at_reject_leaves_empty() -> None:
    init_db()
    frozen = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_approve = _seed_pending_approval(store, external_id="972509995403")
        _, lead_reject = _seed_pending_approval(store, external_id="972509995404")
        db.commit()
        assert store.decide_approval(
            lead_id=lead_approve,
            action=ACTION_PROPOSAL_HANDOFF,
            decision=DECISION_APPROVED,
            now=frozen,
        )
        assert store.decide_approval(
            lead_id=lead_reject,
            action=ACTION_PROPOSAL_HANDOFF,
            decision=DECISION_REJECTED,
            now=frozen,
        )
        db.commit()
        approved_row = store.get_approval(lead_approve, ACTION_PROPOSAL_HANDOFF)
        rejected_row = store.get_approval(lead_reject, ACTION_PROPOSAL_HANDOFF)
        assert approved_row is not None
        assert rejected_row is not None
        assert approved_row.approved_at == frozen.isoformat()
        assert rejected_row.approved_at == ""
    finally:
        db.close()


def test_lead_approve_leaves_execute_fields_empty() -> None:
    init_db()
    frozen = datetime(2026, 8, 21, 12, 30, 0, tzinfo=UTC)
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = _seed_pending_approval(store, external_id="972509995405")
        db.commit()
        assert store.decide_approval(
            lead_id=lead_id,
            action=ACTION_PROPOSAL_HANDOFF,
            decision=DECISION_APPROVED,
            now=frozen,
        )
        db.commit()
        row = store.get_approval(lead_id, ACTION_PROPOSAL_HANDOFF)
        assert row is not None
        assert row.approved_at == frozen.isoformat()
        _assert_execute_fields_empty(row)
    finally:
        db.close()


def test_campaign_pending_row_has_approval_object_fields() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        apply_campaign_write_approval_policy(
            store,
            campaign_id=CAMPAIGN_OBJECT_A,
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        row = _campaign_approval_row(db, CAMPAIGN_OBJECT_A)
        assert row is not None
        assert _APR_ID.match(row.approval_id)
        assert len(row.approval_id) == 16
        _assert_reserved_identity_empty(row)
        assert row.approved_at == ""
        _assert_execute_fields_empty(row)
        expected_params = _expected_proposed_parameters(
            action=ACTION_CAMPAIGN_WRITE,
            risk=RISK_R4,
            channel=Channel.WHATSAPP.value,
            resource_type=RESOURCE_CAMPAIGN,
            resource_id=CAMPAIGN_OBJECT_A,
        )
        assert row.proposed_parameters == expected_params
        parsed = json.loads(row.proposed_parameters)
        assert set(parsed.keys()) == _IDENTITY_PAYLOAD_KEYS
        assert row.payload_hash == payload_hash(**parsed)
    finally:
        db.close()


def test_campaign_pending_reupsert_preserves_approval_id() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        apply_campaign_write_approval_policy(
            store,
            campaign_id=CAMPAIGN_OBJECT_B,
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        first = _campaign_approval_row(db, CAMPAIGN_OBJECT_B)
        assert first is not None
        first_id = first.approval_id
        apply_campaign_write_approval_policy(
            store,
            campaign_id=CAMPAIGN_OBJECT_B,
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        second = _campaign_approval_row(db, CAMPAIGN_OBJECT_B)
        assert second is not None
        assert second.approval_id == first_id
    finally:
        db.close()


def test_campaign_approve_stamps_approved_at_execute_stays_empty() -> None:
    init_db()
    frozen = datetime(2026, 8, 21, 13, 0, 0, tzinfo=UTC)
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        apply_campaign_write_approval_policy(
            store,
            campaign_id=CAMPAIGN_OBJECT_C,
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert store.decide_campaign_approval(
            resource_id=CAMPAIGN_OBJECT_C,
            decision=DECISION_APPROVED,
            now=frozen,
        )
        db.commit()
        row = _campaign_approval_row(db, CAMPAIGN_OBJECT_C)
        assert row is not None
        assert row.approved_at == frozen.isoformat()
        _assert_execute_fields_empty(row)
        _assert_reserved_identity_empty(row)
    finally:
        db.close()


def test_lead_approval_claim_first_persist_completes_idempotency() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_appr_claim_1"
        )
        sales = SalesState(
            lead_id=lead_id,
            workflow_known=True,
            owner_required=True,
        )
        store.save_sales(sales)
        apply_approval_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.HANDOFF.value,
            sales=sales,
            kill_switch=False,
        )
        db.commit()
        claim_key = f"{lead_id}:approval:{ACTION_PROPOSAL_HANDOFF}"
        idem_row = db.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == "approval",
                IdempotencyRow.key == claim_key,
            )
        ).one()
        assert idem_row.status == "completed"
    finally:
        db.close()


def test_lead_approval_duplicate_queue_one_canonical() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_appr_claim_2"
        )
        sales = SalesState(
            lead_id=lead_id,
            workflow_known=True,
            owner_required=True,
        )
        store.save_sales(sales)
        apply_approval_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.HANDOFF.value,
            sales=sales,
            kill_switch=False,
        )
        apply_approval_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.HANDOFF.value,
            sales=sales,
            kill_switch=False,
        )
        db.commit()
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == EventType.APPROVAL_REQUIRED.value,
                )
            )
        )
        assert len(events) == 1
    finally:
        db.close()


def test_campaign_approval_claim_first_persist_completes_idempotency() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        apply_campaign_write_approval_policy(
            store,
            campaign_id=CAMPAIGN_CLAIM_A,
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        claim_key = f"{CAMPAIGN_CLAIM_A}:approval:{ACTION_CAMPAIGN_WRITE}"
        idem_row = db.scalars(
            select(IdempotencyRow).where(
                IdempotencyRow.scope == "approval",
                IdempotencyRow.key == claim_key,
            )
        ).one()
        assert idem_row.status == "completed"
    finally:
        db.close()


def test_campaign_approval_duplicate_queue_one_canonical() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        apply_campaign_write_approval_policy(
            store,
            campaign_id=CAMPAIGN_CLAIM_B,
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        apply_campaign_write_approval_policy(
            store,
            campaign_id=CAMPAIGN_CLAIM_B,
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.provider_event_id
                    == f"{CAMPAIGN_CLAIM_B}:approval:campaign_write",
                    CanonicalEventRow.event_type == EventType.APPROVAL_REQUIRED.value,
                )
            )
        )
        assert len(events) == 1
    finally:
        db.close()
