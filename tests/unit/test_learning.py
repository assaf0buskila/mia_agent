import importlib
import inspect

import pytest
from app.api.inbound import process_inbound_texts
from app.core.capabilities import CapabilityId, require_alive
from app.db.models import OwnerInstructionRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.learning import (
    InstructionKind,
    classify_instruction_kind,
    propose_owner_instruction,
)
from app.domain.owner_tasks import OwnerTaskType, ack_for_owner_task, classify_owner_task
from app.graph.orchestrator import build_graph
from app.integrations.base import RecordingMessagePort
from app.integrations.linkedin import DisabledLinkedInPort
from app.integrations.research import DisabledResearchPort
from app.integrations.sheets import FakeSheetsPort
from sqlalchemy import delete, func, select

from tests.unit.sales_copy import assert_discovery_reply

OWNER_PREFERENCE_PHONE = "972509991001"
OWNER_REMEMBER_LINKEDIN_PHONE = "972509991002"
OWNER_PREFERENCE_DUP_PHONE = "972509991003"
OWNER_PREFERENCE_KILL_PHONE = "972509991004"
PROSPECT_COLD_PHONE = "972509991005"
OWNER_CORRECTION_PHONE = "972509991006"
OWNER_BEHAVIOR_RULE_PHONE = "972509991007"


def _delete_owner_tasks(db, *, event_ids: tuple[str, ...]) -> None:
    from app.db.models import OwnerTaskRow

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
        db.flush()
        db.commit()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("from now on be concise", InstructionKind.PREFERENCE),
        ("that's wrong about pricing", InstructionKind.CORRECTION),
        ("never say we guarantee results", InstructionKind.BEHAVIOR_RULE),
        ("זה לא נכון מה שכתבת", InstructionKind.CORRECTION),
        ("אל תגידי שאנחנו הכי זולים", InstructionKind.BEHAVIOR_RULE),
        ("from now on never say hi", InstructionKind.BEHAVIOR_RULE),
    ],
)
def test_classify_instruction_kind(text: str, expected: InstructionKind) -> None:
    assert classify_instruction_kind(text) == expected


def test_classify_instruction_kind_correction_keyword() -> None:
    assert classify_instruction_kind("correction on tone") == InstructionKind.CORRECTION


def test_classify_instruction_kind_always_say() -> None:
    assert classify_instruction_kind("always say thanks first") == InstructionKind.BEHAVIOR_RULE


def test_classify_instruction_kind_prefer_remember() -> None:
    assert classify_instruction_kind("remember my style") == InstructionKind.PREFERENCE


def test_classify_instruction_kind_curly_apostrophe_correction() -> None:
    assert classify_instruction_kind("that’s wrong about pricing") == (
        InstructionKind.CORRECTION
    )


def test_classify_preference_from_now_on() -> None:
    decision = classify_owner_task("from now on talk shorter")
    assert decision.task_type == OwnerTaskType.PREFERENCE
    assert decision.needs_clarification is False


def test_classify_preference_ack_not_active() -> None:
    decision = classify_owner_task("from now on be more concise")
    ack = ack_for_owner_task(decision, text="from now on be more concise")
    assert "נשמר כהצעת העדפה" in ack
    assert "לא פעיל" in ack
    assert "פרומפטים בפרודקשן" in ack


def test_ack_correction_kind() -> None:
    decision = classify_owner_task("that's wrong about the offer")
    ack = ack_for_owner_task(decision, text="that's wrong about the offer")
    assert "נשמר כהצעת תיקון" in ack
    assert "לא פעיל" in ack
    assert "פרומפטים בפרודקשן" in ack


def test_ack_behavior_rule_kind() -> None:
    decision = classify_owner_task("never say guaranteed")
    ack = ack_for_owner_task(decision, text="never say guaranteed")
    assert "נשמר כהצעת כלל" in ack
    assert "לא פעיל" in ack
    assert "פרומפטים בפרודקשן" in ack


def test_remember_linkedin_needs_clarification_no_instruction_row() -> None:
    decision = classify_owner_task("remember to check my linkedin")
    assert decision.task_type == OwnerTaskType.NOTE
    assert decision.needs_clarification is True
    assert decision.matched_types == ["linkedin", "preference"]
    ack = ack_for_owner_task(decision)
    assert "מה שהבנתי" in ack
    assert "לינקדאין או העדפה" in ack
    assert "לא מבצעת" in ack


def test_preference_not_understanding_check() -> None:
    decision = classify_owner_task("from now on talk shorter")
    assert decision.task_type == OwnerTaskType.PREFERENCE
    assert decision.needs_clarification is False
    ack = ack_for_owner_task(decision)
    assert "מה שהבנתי" not in ack
    assert "נשמר כהצעת העדפה" in ack
    assert "לא פעיל" in ack


def test_require_alive_owner_learning_and_graph_lab_pass() -> None:
    require_alive(CapabilityId.OWNER_LEARNING)
    require_alive(CapabilityId.GRAPH_LAB)


def test_build_graph_does_not_import_learning() -> None:
    source = inspect.getsource(build_graph)
    assert "learning" not in source


def test_learning_module_no_forbidden_imports() -> None:
    module = importlib.import_module("app.domain.learning")
    source = inspect.getsource(module)
    assert "MessagePort" not in source
    assert "MetaAdsPort" not in source


def test_propose_owner_instruction_rejects_fact_kind() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        written = propose_owner_instruction(
            store=store,
            provider="whatsapp",
            provider_event_id="evt.owner.kind.fact.reject",
            body="our pricing is always X",
            kill_switch=False,
            kind=InstructionKind.FACT,
        )
        db.commit()
        assert written is False
        assert store.get_proposed_instruction(
            provider="whatsapp", provider_event_id="evt.owner.kind.fact.reject"
        ) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_preference_persists_proposed_not_active() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.preference.1",
                    "from": OWNER_PREFERENCE_PHONE,
                    "text": "from now on be more concise",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PREFERENCE_PHONE},
            sheets=sheets,
            research=DisabledResearchPort(),
            linkedin=DisabledLinkedInPort(),
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.preference.1"
        )
        assert task is not None
        assert task.task_type == "preference"
        assert task.status == "logged"
        instruction = store.get_proposed_instruction(
            provider="whatsapp", provider_event_id="evt.owner.preference.1"
        )
        assert instruction is not None
        assert instruction.kind == "preference"
        assert instruction.status == "proposed"
        assert "concise" in instruction.body
        assert store.list_active_instructions() == []
        assert sheets.rows == {}
        assert len(port.sent) == 1
        sent = port.sent[0].text
        assert "נשמר כהצעת העדפה" in sent
        assert "לא פעיל" in sent
        assert "how the business works" not in sent
        assert "יום רגיל בעסק" not in sent
    finally:
        _delete_owner_tasks(db, event_ids=("evt.owner.preference.1",))
        db.close()


@pytest.mark.asyncio
async def test_owner_correction_persists_proposed_not_active() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.kind.correction.1",
                    "from": OWNER_CORRECTION_PHONE,
                    "text": "that's wrong about our pricing",
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
        instruction = store.get_proposed_instruction(
            provider="whatsapp", provider_event_id="evt.owner.kind.correction.1"
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
        _delete_owner_tasks(db, event_ids=("evt.owner.kind.correction.1",))
        db.close()


@pytest.mark.asyncio
async def test_owner_behavior_rule_persists_proposed_not_active() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.kind.behavior.1",
                    "from": OWNER_BEHAVIOR_RULE_PHONE,
                    "text": "never say we guarantee results",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_BEHAVIOR_RULE_PHONE},
        )
        db.commit()
        instruction = store.get_proposed_instruction(
            provider="whatsapp", provider_event_id="evt.owner.kind.behavior.1"
        )
        assert instruction is not None
        assert instruction.kind == "behavior_rule"
        assert instruction.status == "proposed"
        assert store.list_active_instructions() == []
        assert len(port.sent) == 1
        assert "נשמר כהצעת כלל" in port.sent[0].text
        assert "לא פעיל" in port.sent[0].text
    finally:
        _delete_owner_tasks(db, event_ids=("evt.owner.kind.behavior.1",))
        db.close()


@pytest.mark.asyncio
async def test_save_proposed_instruction_idempotent() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        kwargs = {
            "provider": "whatsapp",
            "provider_event_id": "evt.owner.preference.dup",
            "kind": "preference",
            "body": "from now on shorter replies",
            "status": "proposed",
        }
        store.save_proposed_instruction(**kwargs)
        store.save_proposed_instruction(**kwargs)
        db.commit()
        count = db.scalar(
            select(func.count())
            .select_from(OwnerInstructionRow)
            .where(
                OwnerInstructionRow.provider == "whatsapp",
                OwnerInstructionRow.provider_event_id == "evt.owner.preference.dup",
            )
        )
        assert count == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_kill_switch_skips_instruction_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        store.save_owner_task(
            provider="whatsapp",
            provider_event_id="evt.owner.preference.kill",
            channel="whatsapp",
            external_id=OWNER_PREFERENCE_KILL_PHONE,
            task_type="preference",
            status="logged",
        )
        written = propose_owner_instruction(
            store=store,
            provider="whatsapp",
            provider_event_id="evt.owner.preference.kill",
            body="from now on be brief",
            kill_switch=True,
        )
        db.commit()
        assert written is False
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.preference.kill"
        )
        assert task is not None
        assert task.task_type == "preference"
        instruction = store.get_proposed_instruction(
            provider="whatsapp", provider_event_id="evt.owner.preference.kill"
        )
        assert instruction is None
        assert store.list_active_instructions() == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_remember_linkedin_inbound_no_instruction_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.remember.linkedin",
                    "from": OWNER_REMEMBER_LINKEDIN_PHONE,
                    "text": "remember to check my linkedin",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_REMEMBER_LINKEDIN_PHONE},
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.remember.linkedin"
        )
        assert task is not None
        assert task.status == "needs_clarification"
        assert store.get_proposed_instruction(
            provider="whatsapp", provider_event_id="evt.owner.remember.linkedin"
        ) is None
        assert len(port.sent) == 1
        assert "מה שהבנתי" in port.sent[0].text
        assert "לא מבצעת" in port.sent[0].text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_prospect_cold_lead_unchanged_no_learning() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.prospect.cold.learning",
                    "from": PROSPECT_COLD_PHONE,
                    "text": "hi there",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PREFERENCE_PHONE},
        )
        db.commit()
        assert store.get_proposed_instruction(
            provider="whatsapp", provider_event_id="evt.prospect.cold.learning"
        ) is None
        assert len(port.sent) == 1
        assert_discovery_reply(port.sent[0].text)
    finally:
        db.close()


def test_save_proposed_instruction_cannot_write_active() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        store.save_proposed_instruction(
            provider="whatsapp",
            provider_event_id="evt.owner.preference.active-attempt",
            kind="preference",
            body="from now on be brief",
            status="active",
        )
        db.commit()
        instruction = store.get_proposed_instruction(
            provider="whatsapp",
            provider_event_id="evt.owner.preference.active-attempt",
        )
        assert instruction is not None
        assert instruction.status == "proposed"
        assert store.list_active_instructions() == []
    finally:
        db.close()


def test_propose_owner_instruction_returns_false_on_kill_switch() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        written = propose_owner_instruction(
            store=store,
            provider="whatsapp",
            provider_event_id="evt.direct.propose.kill",
            body="prefer shorter answers",
            kill_switch=True,
        )
        db.commit()
        assert written is False
        assert store.get_proposed_instruction(
            provider="whatsapp", provider_event_id="evt.direct.propose.kill"
        ) is None
    finally:
        db.close()
