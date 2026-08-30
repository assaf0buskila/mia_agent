import hashlib
import json

import pytest
from app.agents.client.graph import compile_client_graph
from app.api.inbound import process_inbound_texts
from app.api.website import process_website_message
from app.capabilities.types import Principal
from app.channels.website import message_to_client_state
from app.core.config import get_settings
from app.db.models import AiRunRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.ai_runs import (
    GRAPH_VERSION,
    MODEL_CANNED,
    PROMPT_VERSION,
    persist_ai_run,
    sales_model_label,
    sanitize_decision_confidence,
    sanitize_prompt_version,
)
from app.domain.events import Channel, persist_tool_outcome
from app.domain.policies import POLICY_VERSION
from app.domain.sales import NextAction
from app.domain.tools import ToolOutcome
from app.integrations.base import RecordingMessagePort
from app.integrations.calendar import DisabledCalendarPort
from app.integrations.calendar_booking import DisabledCalendarBookingPort
from app.integrations.sales_reply import (
    _SYSTEM_PROMPT,
    FakeSalesReplyPort,
    ReplyContext,
    build_user_content,
)
from app.integrations.sales_reply import (
    PROMPT_VERSION as SALES_REPLY_PROMPT_VERSION,
)
from app.integrations.sheets import DisabledSheetsPort
from app.main import app
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

PROSPECT_PHONE = "972509994901"
VISITOR_TEXT = "hi"
# Bumped with PROMPT_VERSION sales_reply_v9: the merge of the two v8 prompts. The reply
# shape is answer-then-ask over PUBLISHED ASSAFWEB FACTS (ADR-028) *and* the prompt now
# carries the PROSPECT TONE delivery contract, so neither v8 hash is valid any more.
_FROZEN_SYSTEM_PROMPT_SHA256 = (
    "f4c6ee0db000a09f8888e3cc177484eae548253ed9f65c6ffb5c5f99c1b98feb"
)


def _all_ai_run_values(row: AiRunRow) -> str:
    return json.dumps(
        {
            "run_id": row.run_id,
            "lead_id": row.lead_id,
            "channel": row.channel,
            "graph_version": row.graph_version,
            "model": row.model,
            "tokens_in": row.tokens_in,
            "tokens_out": row.tokens_out,
            "cost_usd": row.cost_usd,
            "next_action": row.next_action,
            "kill_switch": row.kill_switch,
            "policy_version": row.policy_version,
            "latency_ms": row.latency_ms,
            "automation_mode": row.automation_mode,
            "prompt_version": row.prompt_version,
            "decision_confidence": row.decision_confidence,
        }
    )


def test_website_first_message_persists_ai_run() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": VISITOR_TEXT},
        )
        assert response.status_code == 200
        lead_id = response.json()["lead_id"]
        assert response.json()["next_action"] == NextAction.UNDERSTAND_WORKFLOW.value
    db = get_session_factory()()
    try:
        rows = list(db.scalars(select(AiRunRow).where(AiRunRow.lead_id == lead_id)).all())
        assert len(rows) == 1
        row = rows[0]
        assert row.run_id.startswith("run_")
        assert row.channel == Channel.WEBSITE.value
        assert row.graph_version == GRAPH_VERSION
        assert row.model == MODEL_CANNED
        assert row.next_action == NextAction.UNDERSTAND_WORKFLOW.value
        assert row.tokens_in == 0
        assert row.tokens_out == 0
        assert row.cost_usd == 0
        assert row.latency_ms >= 0
        assert row.kill_switch is False
        assert row.policy_version == POLICY_VERSION
        assert row.prompt_version == PROMPT_VERSION
        assert row.decision_confidence == "1.0"
        assert row.automation_mode == "auto_approved"
        assert VISITOR_TEXT not in row.model
        assert VISITOR_TEXT not in row.graph_version
        assert VISITOR_TEXT not in row.next_action
        assert VISITOR_TEXT not in row.policy_version
        assert VISITOR_TEXT not in row.prompt_version
        assert VISITOR_TEXT not in _all_ai_run_values(row)
    finally:
        db.close()


def test_website_second_message_persists_second_ai_run() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        first = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": VISITOR_TEXT},
        )
        client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "We run a clinic and miss calls all day."},
        )
        lead_id = first.json()["lead_id"]
    db = get_session_factory()()
    try:
        rows = list(
            db.scalars(
                select(AiRunRow).where(AiRunRow.lead_id == lead_id).order_by(AiRunRow.id)
            ).all()
        )
        assert len(rows) == 2
        assert rows[0].run_id != rows[1].run_id
        assert rows[0].run_id.startswith("run_")
        assert rows[1].run_id.startswith("run_")
    finally:
        db.close()


def test_kill_switch_stops_website_chat_without_ai_run() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_kill_switch"
        )
        db.commit()
        settings = get_settings().model_copy(update={"kill_switch": True})
        with pytest.raises(HTTPException) as exc:
            process_website_message(
                store,
                session_id="web_kill_switch",
                text=VISITOR_TEXT,
                settings=settings,
                calendar=DisabledCalendarPort(),
                calendar_booking=DisabledCalendarBookingPort(),
                sheets=DisabledSheetsPort(),
            )
        assert exc.value.status_code == 503
        db.commit()
        rows = list(db.scalars(select(AiRunRow).where(AiRunRow.lead_id == lead_id)))
        assert rows == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_whatsapp_inbound_persists_ai_run() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "wamid.ai_run.1",
                    "from": PROSPECT_PHONE,
                    "text": VISITOR_TEXT,
                }
            ],
            store=store,
            port=RecordingMessagePort(),
            kill_switch=False,
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE
        )
        row = db.scalars(select(AiRunRow).where(AiRunRow.lead_id == lead_id)).one()
        assert row.run_id.startswith("run_")
        assert row.next_action == NextAction.UNDERSTAND_WORKFLOW.value
        assert row.model == MODEL_CANNED
        assert row.prompt_version == PROMPT_VERSION
        assert row.decision_confidence == "1.0"
        assert row.automation_mode == "auto_approved"
        assert VISITOR_TEXT not in _all_ai_run_values(row)
    finally:
        db.close()


def test_persist_ai_run_duplicate_run_id_writes_once() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.WEBSITE, external_id="web_dup")
        db.commit()
        persist_ai_run(
            store,
            run_id="run_dup123456",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
        )
        persist_ai_run(
            store,
            run_id="run_dup123456",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.DEEPEN_PAIN.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
        )
        db.commit()
        count = db.scalar(
            select(func.count()).select_from(AiRunRow).where(AiRunRow.run_id == "run_dup123456")
        )
        assert count == 1
        row = store.get_ai_run("run_dup123456")
        assert row is not None
        assert row.next_action == NextAction.UNDERSTAND_WORKFLOW.value
        assert row.policy_version == POLICY_VERSION
        assert row.prompt_version == PROMPT_VERSION
        assert row.decision_confidence == "1.0"
    finally:
        db.close()


def test_persist_ai_run_writes_decision_confidence() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_decision_conf"
        )
        db.commit()
        persist_ai_run(
            store,
            run_id="run_decision_conf_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
        )
        db.commit()
        row = store.get_ai_run("run_decision_conf_1")
        assert row is not None
        assert row.decision_confidence == "1.0"
    finally:
        db.close()


def test_persist_ai_run_decision_confidence_first_write_wins() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_decision_fww"
        )
        db.commit()
        persist_ai_run(
            store,
            run_id="run_decision_fww_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
        )
        db.commit()
        first = store.get_ai_run("run_decision_fww_1")
        assert first is not None
        assert first.decision_confidence == "1.0"
        store.save_ai_run(
            run_id="run_decision_fww_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            graph_version=GRAPH_VERSION,
            model=MODEL_CANNED,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0,
            next_action=NextAction.DEEPEN_PAIN.value,
            kill_switch=False,
            policy_version=POLICY_VERSION,
            decision_confidence="0.4",
        )
        db.commit()
        row = store.get_ai_run("run_decision_fww_1")
        assert row is not None
        assert row.decision_confidence == "1.0"
    finally:
        db.close()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, "1.0"),
        (1, "1.0"),
        ("1.0", "1.0"),
        ("1", "1.0"),
        (0, "0"),
        (0.0, "0"),
        (0.5, "0.5"),
        (True, ""),
        (None, ""),
        ("x", ""),
        (1.1, ""),
        (-0.1, ""),
    ],
)
def test_sanitize_decision_confidence(value: object, expected: str) -> None:
    assert sanitize_decision_confidence(value) == expected


def test_persist_ai_run_writes_prompt_version() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_prompt_ver"
        )
        db.commit()
        persist_ai_run(
            store,
            run_id="run_prompt_ver_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
        )
        db.commit()
        row = store.get_ai_run("run_prompt_ver_1")
        assert row is not None
        assert row.prompt_version == PROMPT_VERSION
        assert row.prompt_version == "sales_reply_v9"
    finally:
        db.close()


def test_persist_ai_run_prompt_version_first_write_wins() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_prompt_fww"
        )
        db.commit()
        persist_ai_run(
            store,
            run_id="run_prompt_fww_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
        )
        db.commit()
        first = store.get_ai_run("run_prompt_fww_1")
        assert first is not None
        assert first.prompt_version == PROMPT_VERSION
        store.save_ai_run(
            run_id="run_prompt_fww_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            graph_version=GRAPH_VERSION,
            model=MODEL_CANNED,
            tokens_in=0,
            tokens_out=0,
            cost_usd=0,
            next_action=NextAction.DEEPEN_PAIN.value,
            kill_switch=False,
            policy_version=POLICY_VERSION,
            prompt_version="other_prompt_v9",
        )
        db.commit()
        row = store.get_ai_run("run_prompt_fww_1")
        assert row is not None
        assert row.prompt_version == PROMPT_VERSION
        persist_ai_run(
            store,
            run_id="run_prompt_fww_2",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
        )
        persist_ai_run(
            store,
            run_id="run_prompt_fww_2",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.DEEPEN_PAIN.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
        )
        db.commit()
        second = store.get_ai_run("run_prompt_fww_2")
        assert second is not None
        assert second.prompt_version == PROMPT_VERSION
    finally:
        db.close()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        ("sales_reply_v2", "sales_reply_v2"),
        ("bogus prompt", ""),
        ("../etc/passwd", ""),
        ("a" * 33, ""),
    ],
)
def test_sanitize_prompt_version(value: str, expected: str) -> None:
    assert sanitize_prompt_version(value) == expected


def test_prompt_version_constant_from_sales_reply() -> None:
    assert PROMPT_VERSION == SALES_REPLY_PROMPT_VERSION == "sales_reply_v9"


def test_sales_reply_system_prompt_frozen_hash() -> None:
    # If this fails, bump PROMPT_VERSION in sales_reply.py and ai_runs.py and update the hash.
    digest = hashlib.sha256(_SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    assert digest == _FROZEN_SYSTEM_PROMPT_SHA256


def test_v9_system_prompt_carries_both_contracts() -> None:
    """ADR-040: one version, two contracts. Losing either half is the merge bug."""
    # Answer-then-ask (ADR-028), the shipped production contract.
    assert "16. Answer then ask." in _SYSTEM_PROMPT
    assert "PUBLISHED ASSAFWEB FACTS covers it" in _SYSTEM_PROMPT
    # Prospect tone (ADR-040), delivery only.
    assert "5b. PROSPECT TONE" in _SYSTEM_PROMPT
    assert "instruction about delivery and nothing else" in _SYSTEM_PROMPT
    # Tone loses every conflict with answer-then-ask, and buys no silence.
    assert "A listed PROSPECT TONE buys no exemption here" in _SYSTEM_PROMPT
    # No manufactured empathy when the detector found nothing.
    assert "When no PROSPECT TONE is listed, do not invent a feeling" in _SYSTEM_PROMPT


def test_v9_user_content_omits_tone_block_entirely_without_cues() -> None:
    """A neutral visitor gets no PROSPECT TONE section at all, not an empty header."""
    content = build_user_content(
        action=NextAction.UNDERSTAND_WORKFLOW,
        canned="fallback",
        latest_message="we run a bakery in haifa",
        channel="website",
        context=ReplyContext(
            knowledge=("- [published] Every launch includes a month of guidance.",)
        ),
    )
    assert "PROSPECT TONE" not in content
    assert "PUBLISHED ASSAFWEB FACTS" in content


def test_v9_user_content_renders_knowledge_and_tone_together() -> None:
    """Both blocks reach the model, and tone reaches it as an instruction, not a label."""
    content = build_user_content(
        action=NextAction.HANDLE_OBJECTION,
        canned="fallback",
        latest_message="How long does a launch take? This is getting ridiculous.",
        channel="website",
        context=ReplyContext(
            knowledge=("- [published] Every launch includes a month of guidance.",),
            emotional_cues=("frustrated", "overwhelmed"),
        ),
    )
    assert "PUBLISHED ASSAFWEB FACTS" in content
    assert "PROSPECT TONE" in content
    assert "frustrated, overwhelmed" in content
    assert "This is how to deliver, not what to say" in content
    assert "Tone never decides whether you answer their question." in content


def test_v9_client_graph_passes_structured_knowledge_and_tone_to_reply_port(monkeypatch) -> None:
    """ADR-040/038: one ClientGraph retrieval supplies facts and sales compose supplies tone."""
    def fake_execute(*args: object, **kwargs: object) -> dict[str, object]:
        return {"hits": [{"id": "faq-1", "label": "FAQ", "text": "Published fact."}]}

    monkeypatch.setattr("app.agents.client.graph.execute_capability", fake_execute)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_v9_wiring"
        )
        db.commit()
        port = FakeSalesReplyPort()
        graph = compile_client_graph(
            store,
            reply_port=port,
            principal=Principal.client(source="website", actor_id="web_v9_wiring"),
        )
        graph.invoke(
            message_to_client_state(
                run_id="run_v9_wiring",
                session_id="web_v9_wiring",
                lead_id=lead_id,
                text="This is ridiculous, what do you offer?",
            )
        )
        call = port.calls[0]
        assert call["knowledge_hits"] == [
            {"id": "faq-1", "label": "FAQ", "text": "Published fact."}
        ]
        assert call["context"].emotional_cues == ("frustrated",)
    finally:
        db.close()



def test_persist_ai_run_writes_policy_version() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_policy_ver"
        )
        db.commit()
        persist_ai_run(
            store,
            run_id="run_policy_ver_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
        )
        db.commit()
        row = store.get_ai_run("run_policy_ver_1")
        assert row is not None
        assert row.policy_version == POLICY_VERSION
        assert row.policy_version == "fde_v1"
    finally:
        db.close()


def test_policy_version_constant_from_policies_package() -> None:
    from app.domain.policies import POLICY_VERSION as exported_version
    from app.domain.policies.execution_policy import POLICY_VERSION as registry_version

    assert exported_version == registry_version == "fde_v1"


@pytest.mark.parametrize(
    ("kill_switch", "sales_model", "openai_api_key"),
    [
        (True, "gpt-test-x", "test-key"),
        (False, "", "test-key"),
        (False, "gpt-test-x", ""),
    ],
)
def test_sales_model_label_returns_canned_when_unconfigured(
    kill_switch: bool, sales_model: str, openai_api_key: str
) -> None:
    assert (
        sales_model_label(
            sales_model=sales_model,
            openai_api_key=openai_api_key,
            kill_switch=kill_switch,
        )
        == MODEL_CANNED
    )


def test_sales_model_label_returns_model_when_configured() -> None:
    assert (
        sales_model_label(
            sales_model="gpt-test-x",
            openai_api_key="test-key-not-real",
            kill_switch=False,
        )
        == "gpt-test-x"
    )


def test_sales_model_label_reports_gemini_when_it_is_the_only_model() -> None:
    """A Gemini-only deployment paraphrases, so the audit row must not say canned."""
    assert (
        sales_model_label(
            sales_model="",
            openai_api_key="",
            kill_switch=False,
            gemini_api_key="gemini-key-not-real",
            sales_gemini_model="gemini-test-x",
        )
        == "gemini-test-x"
    )


def test_sales_model_label_prefers_openai_over_gemini() -> None:
    assert (
        sales_model_label(
            sales_model="gpt-test-x",
            openai_api_key="test-key-not-real",
            kill_switch=False,
            gemini_api_key="gemini-key-not-real",
            sales_gemini_model="gemini-test-x",
        )
        == "gpt-test-x"
    )


def test_sales_model_label_is_canned_when_gemini_key_has_no_model() -> None:
    assert (
        sales_model_label(
            sales_model="",
            openai_api_key="",
            kill_switch=False,
            gemini_api_key="gemini-key-not-real",
            sales_gemini_model="",
        )
        == MODEL_CANNED
    )


def test_sales_model_label_is_canned_when_killed_even_with_gemini() -> None:
    assert (
        sales_model_label(
            sales_model="",
            openai_api_key="",
            kill_switch=True,
            gemini_api_key="gemini-key-not-real",
            sales_gemini_model="gemini-test-x",
        )
        == MODEL_CANNED
    )


def test_sales_model_label_uses_fallback_when_primary_empty() -> None:
    assert (
        sales_model_label(
            sales_model="",
            openai_api_key="test-key-not-real",
            kill_switch=False,
            sales_fallback_model="gpt-fallback-x",
        )
        == "gpt-fallback-x"
    )


def test_persist_ai_run_skips_invalid_next_action() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.WEBSITE, external_id="web_bad")
        db.commit()
        persist_ai_run(
            store,
            run_id="run_invalid_action",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action="not_a_real_action",
            kill_switch=False,
            sales_model="",
            openai_api_key="",
        )
        db.commit()
        assert store.get_ai_run("run_invalid_action") is None
    finally:
        db.close()


def test_persist_ai_run_stores_latency_ms() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_latency"
        )
        db.commit()
        persist_ai_run(
            store,
            run_id="run_latency_42",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
            latency_ms=42,
        )
        persist_ai_run(
            store,
            run_id="run_latency_neg",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
            latency_ms=-3,
        )
        db.commit()
        row_ok = store.get_ai_run("run_latency_42")
        row_neg = store.get_ai_run("run_latency_neg")
        assert row_ok is not None
        assert row_ok.latency_ms == 42
        assert row_neg is not None
        assert row_neg.latency_ms == 0
    finally:
        db.close()


def test_persist_ai_run_stores_automation_mode_shadow() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_auto_shadow"
        )
        db.commit()
        persist_ai_run(
            store,
            run_id="run_auto_shadow_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
            automation_mode="shadow",
        )
        db.commit()
        row = store.get_ai_run("run_auto_shadow_1")
        assert row is not None
        assert row.automation_mode == "shadow"
    finally:
        db.close()


def test_persist_ai_run_stores_empty_automation_mode_for_bogus() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_auto_bogus"
        )
        db.commit()
        persist_ai_run(
            store,
            run_id="run_auto_bogus_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
            automation_mode="bogus",
        )
        db.commit()
        row = store.get_ai_run("run_auto_bogus_1")
        assert row is not None
        assert row.automation_mode == ""
    finally:
        db.close()


def test_persist_ai_run_stores_automation_mode_hybrid() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_auto_hybrid"
        )
        db.commit()
        persist_ai_run(
            store,
            run_id="run_auto_hybrid_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
            automation_mode="hybrid",
        )
        db.commit()
        row = store.get_ai_run("run_auto_hybrid_1")
        assert row is not None
        assert row.automation_mode == "hybrid"
    finally:
        db.close()


def test_persist_ai_run_automation_mode_first_write_wins() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_auto_fww"
        )
        db.commit()
        persist_ai_run(
            store,
            run_id="run_auto_fww_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
            automation_mode="shadow",
        )
        persist_ai_run(
            store,
            run_id="run_auto_fww_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            kill_switch=False,
            sales_model="",
            openai_api_key="",
            automation_mode="auto_approved",
        )
        db.commit()
        row = store.get_ai_run("run_auto_fww_1")
        assert row is not None
        assert row.automation_mode == "shadow"
    finally:
        db.close()


def test_persist_tool_outcome_passes_latency_ms() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_tool_lat"
        )
        db.commit()
        outcome = ToolOutcome(tool="calendar_find_free_slots", status="ok", result_count=2)
        persist_tool_outcome(
            store,
            provider="website",
            channel=Channel.WEBSITE,
            inbound_provider_event_id="tool.lat.1",
            conversation_id="web_tool_lat",
            lead_id=lead_id,
            outcome=outcome,
            latency_ms=17,
        )
        persist_tool_outcome(
            store,
            provider="website",
            channel=Channel.WEBSITE,
            inbound_provider_event_id="tool.lat.2",
            conversation_id="web_tool_lat",
            lead_id=lead_id,
            outcome=ToolOutcome(tool="sheets_mirror", status="ok", result_count=1),
        )
        db.commit()
        row_with = store.get_tool_run("tool.lat.1:tool:calendar_find_free_slots")
        row_default = store.get_tool_run("tool.lat.2:tool:sheets_mirror")
        assert row_with is not None
        assert row_with.latency_ms == 17
        assert row_default is not None
        assert row_default.latency_ms == 0
        assert VISITOR_TEXT not in json.dumps(
            {
                "tool": row_with.tool,
                "status": row_with.status,
                "latency_ms": row_with.latency_ms,
            }
        )
    finally:
        db.close()


def test_persist_tool_outcome_uses_outcome_latency_ms() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_tool_lat_outcome"
        )
        db.commit()
        outcome = ToolOutcome(
            tool="calendar_find_free_slots", status="ok", result_count=1, latency_ms=9
        )
        persist_tool_outcome(
            store,
            provider="website",
            channel=Channel.WEBSITE,
            inbound_provider_event_id="tool.lat.outcome.1",
            conversation_id="web_tool_lat_outcome",
            lead_id=lead_id,
            outcome=outcome,
        )
        db.commit()
        row = store.get_tool_run("tool.lat.outcome.1:tool:calendar_find_free_slots")
        assert row is not None
        assert row.latency_ms == 9
    finally:
        db.close()
