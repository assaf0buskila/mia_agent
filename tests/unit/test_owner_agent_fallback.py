"""Model fallback chain, and the observability that was missing.

Live symptom this covers: the owner agent was pinned to a model the account could not
call, every turn raised, and Telegram answered with the pre-brain keyword classifier —
with nothing in the logs to say why, and `/health` still reporting `ready: true`.
"""

from __future__ import annotations

import json

import httpx
import pytest
from app.core.config import get_settings
from app.domain.owner_brain import build_agent_client
from app.integrations.llm_client import LlmClient, LlmError, LlmModelChain


def _client(model: str, handler) -> LlmClient:
    return LlmClient(
        api_key="k", model=model, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def _ok(text: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": text}}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    return handler


def _status(code: int):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(code, json={"error": {"code": "x", "message": "no"}})

    return handler


def _messages() -> list[dict]:
    return [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize("code", [403, 404, 410])
def test_a_model_the_key_cannot_call_advances_to_the_next(code: int) -> None:
    """403/404/410 mean this model will never work here. Try the fallback."""
    chain = LlmModelChain([_client("blocked", _status(code)), _client("good", _ok("hi"))])
    assert chain.complete(messages=_messages()).text == "hi"
    assert chain.last_model == "good"
    assert any("blocked" in item for item in chain.errors)


@pytest.mark.parametrize("code", [429, 500, 503])
def test_load_errors_do_not_burn_the_fallback(code: int) -> None:
    """A rate limit is not evidence the model is wrong; demoting on it hides the problem."""
    chain = LlmModelChain([_client("primary", _status(code)), _client("secondary", _ok("x"))])
    with pytest.raises(LlmError):
        chain.complete(messages=_messages())
    assert chain.last_model == "primary"


def test_a_bad_request_does_not_advance() -> None:
    """400 is a payload bug. Every model rejects it identically."""
    chain = LlmModelChain([_client("primary", _status(400)), _client("secondary", _ok("x"))])
    with pytest.raises(LlmError):
        chain.complete(messages=_messages())
    assert chain.last_model == "primary"


def test_the_last_model_still_reports_its_failure() -> None:
    chain = LlmModelChain([_client("a", _status(404)), _client("b", _status(404))])
    with pytest.raises(LlmError):
        chain.complete(messages=_messages())
    assert len(chain.errors) == 2


def test_the_fallback_model_is_actually_used() -> None:
    """MIA_OWNER_AGENT_FALLBACK_MODEL was documented but ignored: only chain[0] was built."""
    settings = get_settings()
    settings.openai_api_key = "k"
    settings.owner_agent_model = "primary-model"
    settings.owner_agent_fallback_model = "backup-model"
    chain = build_agent_client(settings)
    assert chain.models == ("primary-model", "backup-model")


def test_no_model_configured_is_disabled_not_an_error() -> None:
    settings = get_settings()
    settings.openai_api_key = "k"
    settings.owner_agent_model = ""
    settings.owner_agent_fallback_model = ""
    assert build_agent_client(settings).enabled() is False


def test_fallback_reason_is_recorded_so_the_failure_is_visible() -> None:
    """The actual defect: the drop to the classifier was silent."""
    from app.brain.embeddings import FakeEmbeddingPort
    from app.brain.store import BrainStore
    from app.db.session import get_session_factory, init_db
    from app.db.store import LeadStore
    from app.domain.owner_brain import answer_owner
    from app.domain.owner_tasks import OwnerTaskType

    init_db()
    session = get_session_factory()()
    session.commit()
    settings = get_settings()
    settings.openai_api_key = "k"
    settings.owner_agent_model = "unusable-model"
    settings.owner_agent_fallback_model = ""
    settings.memory_enabled = True

    chain = LlmModelChain([_client("unusable-model", _status(404))])
    result = answer_owner(
        store=LeadStore(session),
        brain=BrainStore(session),
        settings=settings,
        task_type=OwnerTaskType.NOTE,
        owner_text="מה קורה?",
        history=(),
        fallback_text="CANNED",
        kill_switch=False,
        demo_active=False,
        embedding_port=FakeEmbeddingPort(),
        client=chain,
    )
    assert result.used_agent is False
    assert result.text == "CANNED"
    # The whole point: the reason names the model and the HTTP status.
    assert "unusable-model" in result.fallback_reason
    assert "404" in result.fallback_reason


def test_a_deterministic_intent_reports_that_reason_not_a_failure() -> None:
    from app.brain.embeddings import FakeEmbeddingPort
    from app.brain.store import BrainStore
    from app.db.session import get_session_factory, init_db
    from app.db.store import LeadStore
    from app.domain.owner_brain import answer_owner
    from app.domain.owner_tasks import OwnerTaskType

    init_db()
    session = get_session_factory()()
    session.commit()
    result = answer_owner(
        store=LeadStore(session),
        brain=BrainStore(session),
        settings=get_settings(),
        task_type=OwnerTaskType.APPROVAL,
        owner_text="אשר",
        history=(),
        fallback_text="CANNED",
        kill_switch=False,
        demo_active=False,
        embedding_port=FakeEmbeddingPort(),
    )
    assert result.fallback_reason == "deterministic_intent"


def test_json_payload_shape_is_the_chat_completions_contract() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return _ok("x")(request)

    LlmModelChain([_client("m", handler)]).complete(messages=_messages())
    assert captured["model"] == "m"
    assert captured["messages"] == _messages()
