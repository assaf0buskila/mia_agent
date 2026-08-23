"""Thin OpenAI-compatible Chat Completions client for tool calling and structured output.

Deliberately not a refactor of `sales_reply.py` / `owner_reply.py`: those are live,
heavily-tested paraphrase paths and this slice does not touch them. This client serves the
new owner agent loop and memory extraction.

Wire-shape notes, all from the current Chat Completions reference:

- Tools nest under `function`: `{"type":"function","function":{name, description,
  parameters, strict}}`. The Responses API flattens this; Chat Completions does not.
- `message.tool_calls[].function.arguments` is a **JSON string**, not an object.
- `finish_reason == "length"` means the response — possibly a tool-call argument string —
  is truncated. It must be checked *before* parsing JSON, or a truncation is misreported
  as a malformed-JSON bug.
- `message.refusal` is a non-null string on a safety refusal, with `content` null.
- The Gemini OpenAI-compat layer accepts the same `tools` shape but **silently ignores**
  unsupported parameters, so a 200 there is not proof the schema was honoured. Callers
  validate the parsed object and fall back.
"""

from __future__ import annotations

import json
from typing import Any, NamedTuple

import httpx

from app.core.errors import MiaError

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
GEMINI_CHAT_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

DEFAULT_TIMEOUT = 45.0
MAX_TOOL_ARGUMENT_CHARS = 20_000


class LlmError(MiaError):
    code = "llm_call_failed"
    http_status = 502


class ToolCall(NamedTuple):
    call_id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str


class LlmResponse(NamedTuple):
    text: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str
    refusal: str
    tokens_in: int
    tokens_out: int
    raw_message: dict[str, Any]

    def truncated(self) -> bool:
        return self.finish_reason == "length"

    def refused(self) -> bool:
        return bool(self.refusal)


def _clamp_tokens(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(value, 10_000_000))


def function_tool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
    strict: bool = True,
) -> dict[str, Any]:
    """Build one Chat Completions tool definition.

    Under `strict: true` the schema must mark every property required and set
    `additionalProperties: false` on every object; optionality is expressed as a
    `["type", "null"]` union rather than by omitting the key.
    """
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
            "strict": strict,
        },
    }


def json_schema_format(
    *, name: str, schema: dict[str, Any], strict: bool = True
) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "schema": schema, "strict": strict},
    }


class LlmClient:
    """One model endpoint. Callers own retry/fallback policy across clients."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        url: str = OPENAI_CHAT_URL,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._url = url
        self._timeout = timeout
        self._client = client

    @property
    def model(self) -> str:
        return self._model

    def enabled(self) -> bool:
        return bool(self._api_key.strip() and self._model.strip())

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        max_completion_tokens: int | None = None,
    ) -> LlmResponse:
        if not self.enabled():
            raise LlmError("llm client is not configured")
        payload: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
            if parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = parallel_tool_calls
        if response_format is not None:
            payload["response_format"] = response_format
        if max_completion_tokens is not None:
            payload["max_completion_tokens"] = max_completion_tokens
        body = self._post(payload)
        return self._parse(body)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._client is not None:
                response = self._client.post(self._url, json=payload, headers=headers)
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(self._url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LlmError("llm request failed") from exc
        if response.status_code >= 400:
            raise LlmError(f"llm request failed: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise LlmError("llm response was not JSON") from exc
        if not isinstance(body, dict):
            raise LlmError("llm response was not an object")
        return body

    def _parse(self, body: dict[str, Any]) -> LlmResponse:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LlmError("llm response had no choices")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise LlmError("llm response choice was not an object")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise LlmError("llm response message was not an object")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        content = message.get("content")
        refusal = message.get("refusal")
        return LlmResponse(
            text=content.strip() if isinstance(content, str) else "",
            tool_calls=self._parse_tool_calls(message),
            finish_reason=str(choice.get("finish_reason") or ""),
            refusal=refusal.strip() if isinstance(refusal, str) else "",
            tokens_in=_clamp_tokens(usage.get("prompt_tokens")),
            tokens_out=_clamp_tokens(usage.get("completion_tokens")),
            raw_message=message,
        )

    def _parse_tool_calls(self, message: dict[str, Any]) -> tuple[ToolCall, ...]:
        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list):
            return ()
        calls: list[ToolCall] = []
        for raw in raw_calls:
            if not isinstance(raw, dict):
                continue
            function = raw.get("function")
            call_id = raw.get("id")
            if not isinstance(function, dict) or not isinstance(call_id, str):
                continue
            name = function.get("name")
            arguments = function.get("arguments")
            if not isinstance(name, str) or not name:
                continue
            raw_arguments = arguments if isinstance(arguments, str) else ""
            calls.append(
                ToolCall(
                    call_id=call_id,
                    name=name,
                    arguments=parse_tool_arguments(raw_arguments),
                    raw_arguments=raw_arguments[:MAX_TOOL_ARGUMENT_CHARS],
                )
            )
        return tuple(calls)


class LlmModelChain:
    """Try each model in order; advance on a terminal model-level failure.

    A configured-but-unusable model must not silently cost the whole feature. This
    happened live: the owner agent was pinned to a model the account could not call, every
    turn raised, and the console fell back to the pre-brain keyword classifier with nothing
    in the logs to say why.

    Only *model-level* failures advance the chain. A 429 or a 5xx is about load, not about
    this model being wrong, so it is raised rather than burning the fallback.
    """

    # 404 unknown or not-permitted model, 403 access denied, 410 retired. Each means
    # "this model will never work for this key" — try the next one.
    #
    # 400 is deliberately NOT here: a malformed payload is rejected identically by every
    # model, so advancing would just burn the fallback on the same bug. 429/500/503 are
    # load, not access — they raise so the caller sees a transient failure rather than
    # silently demoting to a cheaper model on every rate limit.
    ADVANCE_ON: frozenset[int] = frozenset({403, 404, 410})

    def __init__(self, clients: list[LlmClient]) -> None:
        self._clients = [client for client in clients if client.enabled()]
        self.last_model = ""
        self.errors: list[str] = []

    def enabled(self) -> bool:
        return bool(self._clients)

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(client.model for client in self._clients)

    def complete(self, **kwargs: Any) -> LlmResponse:
        if not self._clients:
            raise LlmError("no model configured")
        self.errors = []
        last: LlmError | None = None
        for index, client in enumerate(self._clients):
            self.last_model = client.model
            try:
                return client.complete(**kwargs)
            except LlmError as exc:
                self.errors.append(f"{client.model}:{exc}")
                status = _status_from_error(exc)
                is_last = index == len(self._clients) - 1
                if status is not None and status not in self.ADVANCE_ON and not is_last:
                    # Load or transport problem, not a model problem. Do not spend the
                    # fallback on it.
                    raise
                last = exc
        raise last or LlmError("all models failed")


def _status_from_error(error: LlmError) -> int | None:
    """Pull the HTTP status back out of the message this client formats."""
    text = str(error)
    marker = "HTTP "
    index = text.rfind(marker)
    if index < 0:
        return None
    digits = text[index + len(marker) :].strip()[:3]
    return int(digits) if digits.isdigit() else None


def parse_tool_arguments(raw: str) -> dict[str, Any]:
    """Decode a tool-call argument string. A malformed payload yields `{}`, never raises."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_json_object(text: str) -> dict[str, Any]:
    """Decode a structured-output body. Returns `{}` rather than raising on bad JSON."""
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def tool_result_message(call_id: str, payload: object) -> dict[str, Any]:
    """The follow-up message for one tool call.

    Exactly one of these per tool call, each keyed by its `tool_call_id`; a missing id
    breaks the next turn. `content` is a string, so the result is serialized here.
    """
    if isinstance(payload, str):
        content = payload
    else:
        content = json.dumps(payload, ensure_ascii=False, default=str)
    return {"role": "tool", "tool_call_id": call_id, "content": content}
