"""Real CRM/approval regression tests, without provider adapters."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    ApprovalRow,
    CrmActivityRow,
    CrmContactRow,
    CrmIssueContactRow,
    CrmIssueRow,
    CrmOutboxRow,
    IdempotencyRow,
)
from app.db.store import LeadStore
from app.services.crm_v2 import WRITER_OWNER, CrmService
from app.services.owner_actions import propose_owner_action
from app.services.quick_leads import QuickLeadInput, QuickLeadService
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

OWNER = Principal.owner(source="telegram", actor_id="12345")


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'service.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(engine)
    engine.dispose()


def settings(**changes):
    return Settings(_env_file=None, telegram_owner_user_ids="12345", **changes)


def lead(**changes):
    payload = {
        "request_id": str(uuid4()), "name": "דנה", "phone": "0501234567",
        "need": "אתר", "source": "referral",
    }
    payload.update(changes)
    return QuickLeadInput.model_validate(payload)


def call(factory, method, value, *, principal=OWNER, configuration=None):
    with factory() as db:
        result = getattr(QuickLeadService(
            db, settings=configuration or settings(), principal=principal,
        ), method)(value)
        db.commit()
        return result


def seed(factory, **fields):
    with factory() as db:
        result = CrmService(db).capture(
            fields, writer=WRITER_OWNER, source_ref="seed:" + uuid4().hex,
        )
        db.commit()
        return result.contact.id


def count(factory, model):
    with factory() as db:
        return db.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize("phone", ["0501234567", "+972501234567", "972501234567", "00972501234567"])
def test_all_legacy_phone_aliases_detect_duplicate_and_preserve_customer(factory, phone):
    contact_id = seed(
        factory, phone=phone, name="מקורי", status="paying_customer", source="website",
    )
    result = call(factory, "propose", lead())
    assert result["status"] == "duplicate"
    assert result["contact_id"] == contact_id and result["revision"] == 1
    assert result["preview"]["source"] == "referral"
    assert count(factory, ApprovalRow) == 0
    assert count(factory, CrmActivityRow) == 0
    with factory() as db:
        fields = json.loads(db.get(CrmContactRow, contact_id).fields_json)
        assert fields["name"] == "מקורי"
        assert fields["status"] == "paying_customer"
        assert fields["source"] == "website"


@pytest.mark.parametrize("alias", ["0501234567", "972501234567", "00972501234567"])
def test_legacy_alias_created_after_proposal_invalidates_approval(factory, alias):
    proposal = call(factory, "propose", lead())
    seed(factory, phone=alias)
    assert call(factory, "approve", proposal["approval_id"])["status"] == "target_changed"
    assert count(factory, CrmContactRow) == 1
    assert count(factory, CrmActivityRow) == 0


@pytest.mark.parametrize("aliases", [False, True])
def test_ambiguous_identities_preserve_all_contacts_and_issue_links(factory, aliases):
    first = seed(factory, phone="0501234567", name="ראשון")
    second = seed(factory, **(
        {"phone": "+972501234567"} if aliases else {"email": "dana@example.test"}
    ))
    data = lead() if aliases else lead(email="dana@example.test")
    result = call(factory, "propose", data)
    assert result["status"] == "identity_conflict"
    assert set(result["contact_ids"]) == {first, second}
    assert count(factory, CrmContactRow) == 2
    assert count(factory, CrmActivityRow) == 0
    assert count(factory, ApprovalRow) == 0
    assert count(factory, CrmIssueRow) == 1
    assert count(factory, CrmIssueContactRow) == 2
    with factory() as db:
        issue = db.get(CrmIssueRow, result["issue_ids"][0])
        assert json.loads(issue.details_json)["manual_lead"] == data.model_dump(mode="json")
        receipt = db.scalar(select(IdempotencyRow).where(IdempotencyRow.scope == "quick_lead"))
        assert json.loads(receipt.result_json)["lead"]["need"] == "אתר"
    assert call(factory, "propose", data) == result
    assert count(factory, CrmIssueRow) == 1


def test_repeated_request_payload_is_immutable(factory):
    data = lead()
    first = call(factory, "propose", data)
    assert call(factory, "propose", data) == first
    changed = lead(request_id=data.request_id, name="שם שונה")
    assert call(factory, "propose", changed)["status"] == "idempotency_conflict"
    assert count(factory, ApprovalRow) == 1


def test_oversized_phone_alias_never_falls_back_to_fuzzy_contact_search(factory):
    seed(factory, phone="0521234567", want="00123456789012345 appears in prose")
    result = call(factory, "propose", lead(phone="+123456789012345"))
    assert result["status"] == "pending_approval"


def test_expired_and_tampered_approvals_cannot_create(factory):
    first = call(factory, "propose", lead())
    with factory() as db:
        row = LeadStore(db).get_approval_by_approval_id(first["approval_id"])
        row.expires_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        db.commit()
    assert call(factory, "approve", first["approval_id"])["status"] == "expired"
    second = call(factory, "propose", lead(phone="0521234567"))
    with factory() as db:
        row = LeadStore(db).get_approval_by_approval_id(second["approval_id"])
        envelope = json.loads(row.proposed_parameters)
        envelope["parameters"]["lead"]["phone"] = "+972549999999"
        row.proposed_parameters = json.dumps(envelope)
        db.commit()
    assert call(factory, "approve", second["approval_id"])["status"] == "unbound"
    assert count(factory, CrmContactRow) == 0


def test_foreign_action_cannot_execute_through_quick_lead(factory):
    with factory() as db:
        proposal = propose_owner_action(
            LeadStore(db), principal=OWNER, source_ref="other-action", kind="gmail.create_draft",
            parameters={"to": "customer@example.test", "body": "forbidden"}, target={},
        )
        db.commit()
    assert call(factory, "approve", proposal.approval_id)["status"] == "unbound"
    assert count(factory, CrmContactRow) == 0


@pytest.mark.parametrize("principal,configuration", [
    (Principal.client(source="website", actor_id="12345"), settings()),
    (Principal.owner(source="website", actor_id="12345"), settings()),
    (Principal.owner(source="telegram", actor_id="99999"), settings()),
    (OWNER, settings(kill_switch=True)),
])
def test_owner_write_boundary_is_checked_in_service(factory, principal, configuration):
    with pytest.raises(PermissionError):
        call(factory, "propose", lead(), principal=principal, configuration=configuration)
    assert count(factory, ApprovalRow) == 0


def test_unknown_failure_rolls_back_partial_capture_and_never_retries(factory, monkeypatch):
    proposal = call(factory, "propose", lead())

    def fail_activity(*_args, **_kwargs):
        raise RuntimeError("synthetic failure after contact was flushed")

    monkeypatch.setattr(CrmService, "_record_activity", fail_activity)
    result = call(factory, "approve", proposal["approval_id"])
    assert result["status"] == "unknown"
    assert count(factory, CrmContactRow) == 0
    assert count(factory, CrmActivityRow) == 0
    assert count(factory, CrmOutboxRow) == 0
    with factory() as db:
        assert db.scalar(select(IdempotencyRow.status).where(
            IdempotencyRow.scope == "approval"
        )) == "pending_review"
    assert call(factory, "approve", proposal["approval_id"])["status"] == "already_handled"


def test_service_does_not_commit_and_missing_business_stays_empty(factory):
    proposal = call(factory, "propose", lead(email="DANA@EXAMPLE.TEST", phone=""))
    with factory() as db:
        result = QuickLeadService(db, settings=settings(), principal=OWNER).approve(
            proposal["approval_id"]
        )
        assert result["status"] == "created"
        fields = json.loads(db.get(CrmContactRow, result["contact_id"]).fields_json)
        assert fields["business"] == ""
        assert fields["email"] == "dana@example.test"
        assert fields["status"] == "lead"
        db.rollback()
    assert count(factory, CrmContactRow) == 0
    assert count(factory, CrmActivityRow) == 0


@pytest.mark.parametrize("phone", ["abc0501234567", "+9720501234567", "050", "050+1234567"])
def test_invalid_phone_is_rejected_even_with_valid_email(phone):
    with pytest.raises(ValidationError):
        lead(phone=phone, email="dana@example.test")
