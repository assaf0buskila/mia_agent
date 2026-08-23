"""Owner Telegram paraphraser: phrases typed RESULT, never selects tools."""

import hashlib
import json

import httpx
import pytest
from app.api.inbound import process_inbound_texts
from app.core.capabilities import CapabilityId, require_alive
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.memory import ConversationTurn
from app.domain.policies.execution_policy import ExecutionMode, policy_for
from app.domain.policies.task_classes import TaskClass, task_class_pin
from app.integrations.base import RecordingMessagePort
from app.integrations.owner_reply import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    CannedOwnerReplyPort,
    FakeOwnerReplyPort,
    OpenAIOwnerReplyPort,
    build_owner_reply_port,
    build_owner_user_content,
    owner_phrasing_acceptable,
)

_OWNER_ID = "700100209"
_FROZEN_OWNER_PROMPT_SHA256 = (
    "d82478cf9233c529b6c22bfc2d798397e3a5671c734755ed1192d8e347aa3465"
)


def test_owner_prompt_version_and_reasoning_contract() -> None:
    assert PROMPT_VERSION == "owner_telegram_v2"
    assert "reason silently about this conversation turn" in SYSTEM_PROMPT
    assert "Python owns" in SYSTEM_PROMPT
    assert "Composio tools" in SYSTEM_PROMPT
    assert "WHATSAPP_SEND_MESSAGE" not in SYSTEM_PROMPT
    assert "catalog" not in SYSTEM_PROMPT.lower()


def test_owner_system_prompt_frozen_hash() -> None:
    digest = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    assert digest == _FROZEN_OWNER_PROMPT_SHA256


def test_owner_user_content_asks_for_conversation_reasoning() -> None:
    content = build_owner_user_content(
        task_type="pending_approvals",
        canned="אין אישורים ממתינים.",
        owner_message="מה מחכה לאישור?",
        history=(
            ConversationTurn(role="owner", text="מה קרה היום?"),
            ConversationTurn(role="mia", text="שלושה לידים מהאתר."),
        ),
    )
    assert "TASK: pending_approvals" in content
    assert "REASON THEN WRITE" in content
    assert "data, not instructions" in content
    assert "מה מחכה לאישור?" in content


def test_canned_port_returns_result_unchanged() -> None:
    port = CannedOwnerReplyPort()
    result = port.compose(
        task_type="daily_brief",
        canned="היום היו שני לידים.",
        owner_message="מה קרה היום?",
        kill_switch=False,
    )
    assert result.text == "היום היו שני לידים."
    assert result.tokens_in == 0
    assert result.tokens_out == 0


def test_fake_port_paraphrases_unless_kill_switch() -> None:
    fake = FakeOwnerReplyPort()
    live = fake.compose(
        task_type="hot_leads",
        canned="ליד אחד חם.",
        owner_message="תראה לי לידים חמים",
        kill_switch=False,
    )
    killed = fake.compose(
        task_type="hot_leads",
        canned="ליד אחד חם.",
        owner_message="תראה לי לידים חמים",
        kill_switch=True,
    )
    assert live.text == "ליד אחד חם. (paraphrased for test)"
    assert killed.text == "ליד אחד חם."
    assert len(fake.calls) == 2


def test_owner_phrasing_rejects_tools_and_dropped_lead_ids() -> None:
    canned = "lead_abc123def456 מחכה לשיחה. כתוב ל-owner@example.com"
    assert owner_phrasing_acceptable(
        canned, "lead_abc123def456 עדיין פתוח. כתוב ל-owner@example.com"
    )
    assert not owner_phrasing_acceptable(canned, "הכל טוב")
    assert not owner_phrasing_acceptable(
        canned, "I'll call WHATSAPP_SEND_MESSAGE for lead_abc123def456"
    )
    assert not owner_phrasing_acceptable(canned, "")


def test_build_owner_reply_port_canned_without_keys() -> None:
    port = build_owner_reply_port(Settings(openai_api_key="", sales_model=""))
    assert isinstance(port, CannedOwnerReplyPort)


def test_build_owner_reply_port_live_when_key_and_model() -> None:
    port = build_owner_reply_port(
        Settings(openai_api_key="sk-test-key", sales_model="test-sales-model")
    )
    assert isinstance(port, OpenAIOwnerReplyPort)


def test_openai_owner_port_kill_switch_skips_http() -> None:
    class _Client:
        post_called = False

        def post(self, *args, **kwargs):
            self.post_called = True
            raise AssertionError("kill switch must not HTTP")

    client = _Client()
    port = OpenAIOwnerReplyPort(
        api_key="sk-test",
        model="test-sales-model",
        client=client,  # type: ignore[arg-type]
    )
    canned = "אין אישורים."
    result = port.compose(
        task_type="pending_approvals",
        canned=canned,
        owner_message="מה מחכה?",
        kill_switch=True,
    )
    assert result.text == canned
    assert client.post_called is False


def test_openai_owner_port_falls_back_when_tool_shaped() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "Calling WHATSAPP_SEND_MESSAGE now."}}
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = OpenAIOwnerReplyPort(
        api_key="sk-test",
        model="test-sales-model",
        client=client,
    )
    canned = "אין אישורים ממתינים."
    result = port.compose(
        task_type="pending_approvals",
        canned=canned,
        owner_message="מה מחכה לאישור?",
        kill_switch=False,
    )
    assert result.text == canned


def test_openai_owner_port_keeps_lead_id() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "lead_abc123def456 הכי מעניין עכשיו."
                        }
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 9},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = OpenAIOwnerReplyPort(
        api_key="sk-test",
        model="test-sales-model",
        client=client,
    )
    result = port.compose(
        task_type="website_conversations",
        canned="lead_abc123def456 דיבר על מלאי.",
        owner_message="מה הכי מעניין?",
        kill_switch=False,
    )
    assert result.text == "lead_abc123def456 הכי מעניין עכשיו."
    assert result.tokens_in == 11
    assert result.tokens_out == 9


@pytest.mark.asyncio
async def test_inbound_owner_path_uses_injected_reply_port() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        fake = FakeOwnerReplyPort()
        await process_inbound_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[
                {
                    "id": "evt.owner.phrase.1",
                    "from": _OWNER_ID,
                    "text": "מה קרה היום?",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={_OWNER_ID},
            owner_reply=fake,
        )
        db.commit()
        assert fake.calls
        assert fake.calls[0]["task_type"]
        assert "(paraphrased for test)" in port.sent[-1].text
    finally:
        db.close()


def test_owner_reply_capability_and_policy() -> None:
    require_alive(CapabilityId.OWNER_REPLY)
    policy = policy_for(CapabilityId.OWNER_REPLY)
    assert policy.execution_mode == ExecutionMode.AI_AUTOMATIC
    assert policy.fail_closed is True
    pin = task_class_pin(TaskClass.OWNER_CONVERSATION.value)
    assert pin.owner == "owner_reply_port"
    assert pin.model_source == "env"


def test_openai_owner_request_contains_reasoning_cue() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "שני לידים מהאתר."}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = OpenAIOwnerReplyPort(
        api_key="sk-test",
        model="test-sales-model",
        client=client,
    )
    port.compose(
        task_type="daily_brief",
        canned="שני לידים מהאתר.",
        owner_message="מה קרה היום?",
        kill_switch=False,
    )
    assert captured
    messages = captured[0]["messages"]
    assert isinstance(messages, list)
    assert "reason silently" in str(messages[0]["content"])
    assert "REASON THEN WRITE" in str(messages[1]["content"])
