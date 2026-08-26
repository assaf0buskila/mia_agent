
"""Model fallback chain, and the observability that was missing.

Live symptom this covers: the owner agent was pinned to a model the account could not
call, every turn raised, and Telegram answered with the pre-brain keyword classifier —
with nothing in the logs to say why, and `/health` still reporting `ready: true`.
"""

from __future__ import annotations

import json

import httpx
import pytest
from app.capabilities.types import Principal
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
    from app.domain.owner_brain import NOTE_AGENT_FAILURE_TEXT, answer_owner
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
        principal=Principal.owner(source="test"),
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
    # The text contract moved (see test_brain_end_to_end.py for the full story): a
    # NOTE turn the agent was allowed to run but failed now gets the honest
    # NOTE_AGENT_FAILURE_TEXT instead of the caller's canned "CANNED" fallback. The
    # actual point of this test -- that the failure REASON is recorded and visible,
    # naming the model and the HTTP status -- is unchanged and asserted below.
    assert result.text == NOTE_AGENT_FAILURE_TEXT
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
        principal=Principal.owner(source="test"),
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


# --------------------------------------------------- cross-provider fallback


def _settings_with_both() -> object:
    settings = get_settings()
    settings.openai_api_key = "k"
    settings.gemini_api_key = "g"
    settings.owner_agent_model = "openai-primary"
    settings.owner_agent_fallback_model = "openai-secondary"
    settings.owner_agent_gemini_model = "gemini-3.7-flash"
    settings.extraction_model = "openai-extract"
    return settings


def test_gemini_is_the_last_resort_in_the_agent_chain() -> None:
    """An OpenAI-side block on every model must not kill the console when Gemini works."""
    chain = build_agent_client(_settings_with_both())
    assert chain.models == ("openai-primary", "openai-secondary", "gemini-3.7-flash")


def test_extraction_also_falls_over_to_gemini() -> None:
    from app.domain.owner_brain import build_extraction_client

    chain = build_extraction_client(_settings_with_both())
    assert chain.models == ("openai-extract", "gemini-3.7-flash")


def test_gemini_is_skipped_without_a_key_or_a_model() -> None:
    settings = _settings_with_both()
    settings.gemini_api_key = ""
    assert build_agent_client(settings).models == ("openai-primary", "openai-secondary")
    settings = _settings_with_both()
    settings.owner_agent_gemini_model = ""
    assert build_agent_client(settings).models == ("openai-primary", "openai-secondary")


def test_gemini_client_targets_the_openai_compat_endpoint() -> None:
    """The compat endpoint takes the identical nested `tools` shape, so the loop is unchanged."""
    from app.domain.owner_brain import _gemini_clients
    from app.integrations.llm_client import GEMINI_CHAT_URL

    settings = _settings_with_both()
    clients = _gemini_clients(settings, settings.owner_agent_gemini_model)
    assert len(clients) == 1
    assert clients[0]._url == GEMINI_CHAT_URL
    assert clients[0].model == "gemini-3.7-flash"


def test_the_chain_reaches_gemini_when_every_openai_model_is_blocked() -> None:
    chain = LlmModelChain(
        [
            _client("openai-primary", _status(404)),
            _client("openai-secondary", _status(404)),
            _client("gemini-3.7-flash", _ok("שלום")),
        ]
    )
    assert chain.complete(messages=_messages()).text == "שלום"
    assert chain.last_model == "gemini-3.7-flash"
