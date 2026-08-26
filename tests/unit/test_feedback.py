import importlib
import inspect

import pytest
from app.api.inbound import process_inbound_texts
from app.core.capabilities import CapabilityId, require_alive
from app.core.risk import RiskLevel
from app.db.models import OwnerCorrectionRow, OwnerInstructionRow, OwnerTaskRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.feedback import (
    CorrectionScope,
    classify_correction_scope,
    persist_owner_correction,
)
from app.domain.learning import MAX_INSTRUCTION_BODY
from app.domain.policies.execution_policy import ExecutionMode, policy_for
from app.integrations.base import RecordingMessagePort
from app.integrations.linkedin import DisabledLinkedInPort
from app.integrations.research import DisabledResearchPort
from app.integrations.sheets import FakeSheetsPort
from sqlalchemy import delete

OWNER_CORRECTION_PHONE = "972509991108"
OWNER_REMEMBER_CORRECTION_PHONE = "972509991109"
OWNER_CORRECTION_DUP_PHONE = "972509991110"
PROSPECT_CORRECTION_PHONE = "972509991111"


def _delete_feedback_rows(db, *, event_ids: tuple[str, ...]) -> None:
    for event_id in event_ids:
        db.execute(
            delete(OwnerTaskRow).where(
                OwnerTaskRow.provider == "whatsapp",
                OwnerTaskRow.provider_event_id == event_id,
            )
        )
        db.execute(
            delete(OwnerInstructionRow).where(
                OwnerInstructionRow.provider == "whatsapp",
                OwnerInstructionRow.provider_event_id == event_id,
            )
        )
        db.execute(
            delete(OwnerCorrectionRow).where(
                OwnerCorrectionRow.provider == "whatsapp",
                OwnerCorrectionRow.provider_event_id == event_id,
            )
        )
        db.flush()
        db.commit()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("remember this", CorrectionScope.REMEMBER),
        ("לזכור", CorrectionScope.REMEMBER),
        ("that's wrong", CorrectionScope.THIS_TURN),
    ],
)
def test_classify_correction_scope(text: str, expected: CorrectionScope) -> None:
    assert classify_correction_scope(text) == expected


@pytest.mark.asyncio
async def test_owner_correction_persists_logged_and_proposed() -> None:
    init_db()
    db = get_session_factory()()
    event_id = "wamid.fdbk.correction.1"
    long_tail = "x" * 2500
    body = f"that's wrong, don't pitch ROI {long_tail}"
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": event_id,
                    "from": OWNER_CORRECTION_PHONE,
                    "text": body,
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_CORRECTION_PHONE},
            sheets=sheets,
            research=DisabledResearchPort(),
            linkedin=DisabledLinkedInPort(),
        )
        db.commit()
        correction = store.get_owner_correction(
            provider="whatsapp", provider_event_id=event_id
        )
        assert correction is not None
        assert correction.status == "logged"
        assert correction.scope == CorrectionScope.THIS_TURN.value
        assert len(correction.body) == MAX_INSTRUCTION_BODY
        assert "don't pitch ROI" in correction.body
        instruction = store.get_proposed_instruction(
            provider="whatsapp", provider_event_id=event_id
        )
        assert instruction is not None
        assert instruction.kind == "correction"
        assert instruction.status == "proposed"
        assert store.list_active_instructions() == []
        assert len(port.sent) == 1
        sent = port.sent[0].text
        assert "נשמר כהצעת תיקון" in sent
        assert "לא פעיל" in sent
    finally:
        _delete_feedback_rows(db, event_ids=(event_id,))
        db.close()


@pytest.mark.asyncio
async def test_owner_correction_remember_scope() -> None:
    init_db()
    db = get_session_factory()()
    event_id = "wamid.fdbk.remember.1"
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": event_id,
                    "from": OWNER_REMEMBER_CORRECTION_PHONE,
                    "text": "that's wrong, remember this",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_REMEMBER_CORRECTION_PHONE},
            sheets=FakeSheetsPort(),
            research=DisabledResearchPort(),
            linkedin=DisabledLinkedInPort(),
        )
        db.commit()
        correction = store.get_owner_correction(
            provider="whatsapp", provider_event_id=event_id
        )
        assert correction is not None
        assert correction.scope == CorrectionScope.REMEMBER.value
    finally:
        _delete_feedback_rows(db, event_ids=(event_id,))
        db.close()


def test_duplicate_provider_event_id_writes_correction_once() -> None:
    init_db()
    db = get_session_factory()()
    event_id = "wamid.fdbk.dup.1"
    try:
        store = LeadStore(db)
        first = persist_owner_correction(
            store=store,
            provider="whatsapp",
            provider_event_id=event_id,
            body="that's wrong about tone",
            kill_switch=False,
        )
        second = persist_owner_correction(
            store=store,
            provider="whatsapp",
            provider_event_id=event_id,
            body="that's wrong again",
            kill_switch=False,
        )
        db.commit()
        assert first is True
        assert second is False
        correction = store.get_owner_correction(
            provider="whatsapp", provider_event_id=event_id
        )
        assert correction is not None
        assert correction.body == "that's wrong about tone"
    finally:
        _delete_feedback_rows(db, event_ids=(event_id,))
        db.close()


def test_persist_owner_correction_returns_false_on_kill_switch() -> None:
    init_db()
    db = get_session_factory()()
    event_id = "wamid.fdbk.kill.1"
    try:
        store = LeadStore(db)
        written = persist_owner_correction(
            store=store,
            provider="whatsapp",
            provider_event_id=event_id,
            body="that's wrong about pricing",
            kill_switch=True,
        )
        db.commit()
        assert written is False
        assert store.get_owner_correction(
            provider="whatsapp", provider_event_id=event_id
        ) is None
    finally:
        db.close()


def test_feedback_module_no_forbidden_imports() -> None:
    module = importlib.import_module("app.domain.feedback")
    source = inspect.getsource(module)
    forbidden = (
        "app.graph",
        "MessagePort",
        "select_next_action",
        "list_active_instructions",
    )
    for token in forbidden:
        assert token not in source


def test_require_alive_fde_feedback() -> None:
    require_alive(CapabilityId.FDE_FEEDBACK)


def test_fde_feedback_policy_is_deterministic_r1() -> None:
    policy = policy_for(CapabilityId.FDE_FEEDBACK)
    assert policy.execution_mode == ExecutionMode.DETERMINISTIC
    assert policy.risk == RiskLevel.R1_LOW_WRITE


@pytest.mark.asyncio
async def test_prospect_correction_phrasing_does_not_write_owner_corrections() -> None:
    init_db()
    db = get_session_factory()()
    event_id = "wamid.fdbk.prospect.1"
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": event_id,
                    "from": PROSPECT_CORRECTION_PHONE,
                    "text": "that's wrong about your offer",
                    "source": "text",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids=set(),
            sheets=FakeSheetsPort(),
            research=DisabledResearchPort(),
            linkedin=DisabledLinkedInPort(),
        )
        db.commit()
        assert store.get_owner_correction(
            provider="whatsapp", provider_event_id=event_id
        ) is None
        assert store.get_proposed_instruction(
            provider="whatsapp", provider_event_id=event_id
        ) is None
    finally:
        _delete_feedback_rows(db, event_ids=(event_id,))
        db.close()
