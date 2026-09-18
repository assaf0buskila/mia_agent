"""Shared purpose-specific LLM transports and provider fallback.

Owner and website OpenAI reasoning use Responses. Gemini uses its compatible
Chat Completions endpoint. Tool schemas are normalized at the transport boundary;
provider-private continuation state never crosses to another provider. Callers
validate tool requests and own execution, approval and side-effect deduplication.
Extraction has a separate configurable provider chain.
"""

from __future__ import annotations

import json
import logging
from time import monotonic
from typing import Any, NamedTuple
from urllib.parse import urlparse

import httpx

from app.core.errors import MiaError
from app.core.models import model_chain
from app.core.owner_timing import log_timeout_stage, owner_stage

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
GEMINI_CHAT_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

_LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 45.0
MAX_TOOL_ARGUMENT_CHARS = 20_000
_GEMINI_REASONING_LEVELS = frozenset({"none", "minimal", "low", "medium", "high"})


class LlmError(MiaError):
    code = "llm_call_failed"
    http_status = 502


def _reasoning_for_endpoint(value: str, url: str) -> str:
    """Map configured effort to the levels supported by each endpoint."""
    effort = value.strip().lower()
    if not effort:
        return ""
    if url == GEMINI_CHAT_URL:
        if effort in {"xhigh", "max"}:
            return "high"
        return effort if effort in _GEMINI_REASONING_LEVELS else ""
    return effort


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


def _message_text(content: object) -> str:
    """Chat Completions `message.content` is a string, or a list of parts."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str) and item.strip():
            parts.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def _chat_message(message: dict[str, Any]) -> dict[str, Any]:
    """Drop Responses-only replay data before a Chat Completions fallback."""
    return {key: value for key, value in message.items() if key != "_responses_output"}


def _responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Flatten one existing Chat Completions function tool for Responses."""
    function = tool.get("function")
    if not isinstance(function, dict):
        return dict(tool)
    return {
        "type": "function",
        "name": function.get("name"),
        "description": function.get("description", ""),
        "parameters": function.get("parameters", {}),
        "strict": function.get("strict", True),
    }


def _responses_content(content: object) -> object:
    """Translate the small Chat multimodal subset used by Telegram images."""
    if not isinstance(content, list):
        return content
    converted: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            converted.append({"type": "input_text", "text": str(part.get("text") or "")})
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            converted.append({"type": "input_image", "image_url": str(url or "")})
    return converted


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


def json_schema_format(*, name: str, schema: dict[str, Any], strict: bool = True) -> dict[str, Any]:
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
        reasoning_effort: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._url = url
        self._timeout = timeout
        self._reasoning_effort = _reasoning_for_endpoint(reasoning_effort, url)
        self._client = client

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        """Endpoint host. Two rungs on the same host share one outage."""
        return (urlparse(self._url).hostname or "").lower()

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
        timeout: float | None = None,
    ) -> LlmResponse:
        if not self.enabled():
            raise LlmError("llm client is not configured")
        if self._url == OPENAI_RESPONSES_URL:
            payload = self._responses_payload(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                max_completion_tokens=max_completion_tokens,
                response_format=response_format,
            )
            body = self._post(payload, timeout=timeout)
            return self._parse_responses(body)

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [_chat_message(message) for message in messages],
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
            if parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = parallel_tool_calls
            # GPT-5.6 Chat Completions rejects function tools unless reasoning is
            # disabled (or the caller moves to /v1/responses). Without this, live
            # owner-agent turns 400 as Hebrew "שגיאת ספק" / provider_error.
            if self._url == OPENAI_CHAT_URL:
                payload["reasoning_effort"] = "none"
            elif self._reasoning_effort:
                payload["reasoning_effort"] = self._reasoning_effort
        elif self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        if response_format is not None:
            payload["response_format"] = response_format
        if max_completion_tokens is not None:
            payload["max_completion_tokens"] = max_completion_tokens
        try:
            body = self._post(payload, timeout=timeout)
        except LlmError as exc:
            # Some models 400 on `parallel_tool_calls`. Drop it and retry once so a
            # capabilities-style question is not killed by a tool-calling flag.
            if (
                tools
                and parallel_tool_calls is not None
                and _status_from_error(exc) == 400
                and "parallel_tool_calls" in payload
            ):
                payload.pop("parallel_tool_calls", None)
                body = self._post(payload, timeout=timeout)
            else:
                raise
        return self._parse(body)

    def _post(self, payload: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._client is not None:
                response = self._client.post(
                    self._url, json=payload, headers=headers, timeout=timeout or self._timeout
                )
            else:
                with httpx.Client(timeout=timeout or self._timeout) as client:
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

    def _responses_payload(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        parallel_tool_calls: bool | None,
        max_completion_tokens: int | None,
        response_format: dict[str, Any] | None,
    ) -> dict[str, Any]:
        input_items: list[dict[str, Any]] = []
        instructions: list[str] = []
        for message in messages:
            raw_output = message.get("_responses_output")
            if isinstance(raw_output, list):
                input_items.extend(item for item in raw_output if isinstance(item, dict))
                continue
            role = message.get("role")
            if role == "system":
                content = message.get("content")
                if isinstance(content, str) and content:
                    instructions.append(content)
                continue
            if role == "tool":
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(message.get("tool_call_id") or ""),
                        "output": str(message.get("content") or ""),
                    }
                )
                continue
            if role == "assistant" and isinstance(message.get("tool_calls"), list):
                content = message.get("content")
                if isinstance(content, str) and content:
                    input_items.append({"role": "assistant", "content": content})
                for raw_call in message["tool_calls"]:
                    function = raw_call.get("function") if isinstance(raw_call, dict) else None
                    if not isinstance(function, dict):
                        continue
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": str(raw_call.get("id") or ""),
                            "name": str(function.get("name") or ""),
                            "arguments": str(function.get("arguments") or "{}"),
                        }
                    )
                continue
            input_items.append(
                {
                    "role": role or "user",
                    "content": _responses_content(message.get("content") or ""),
                }
            )
        payload: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "store": False,
            # Reasoning models require their returned reasoning items on the next
            # stateless tool turn. Encrypted content lets that work under store=false.
            "include": ["reasoning.encrypted_content"],
        }
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        if self._reasoning_effort:
            payload["reasoning"] = {"effort": self._reasoning_effort}
        if tools:
            payload["tools"] = [_responses_tool(tool) for tool in tools]
            payload["tool_choice"] = tool_choice or "auto"
            if parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = parallel_tool_calls
        if max_completion_tokens is not None:
            payload["max_output_tokens"] = max_completion_tokens
        if response_format is not None:
            schema = response_format.get("json_schema")
            if response_format.get("type") == "json_schema" and isinstance(schema, dict):
                payload["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": schema.get("name", "response"),
                        "schema": schema.get("schema", {}),
                        "strict": schema.get("strict", True),
                    }
                }
        return payload

    def _parse_responses(self, body: dict[str, Any]) -> LlmResponse:
        output = body.get("output")
        if not isinstance(output, list):
            raise LlmError("llm response had no output")
        status = str(body.get("status") or "")
        incomplete = body.get("incomplete_details")
        reason = incomplete.get("reason") if isinstance(incomplete, dict) else ""
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        if status == "incomplete" and reason == "max_output_tokens":
            # Output can contain a syntactically complete-looking function call even
            # though generation stopped early. Expose neither prose nor calls: the
            # agent sees a truncation and cannot execute partial intent.
            return LlmResponse(
                text="",
                tool_calls=(),
                finish_reason="length",
                refusal="",
                tokens_in=_clamp_tokens(usage.get("input_tokens")),
                tokens_out=_clamp_tokens(usage.get("output_tokens")),
                raw_message={"_responses_output": []},
            )
        if status != "completed":
            raise LlmError(f"llm response was not completed: {status or 'unknown'}")
        calls: list[ToolCall] = []
        text_parts: list[str] = []
        refusal = ""
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                raw_arguments = item.get("arguments")
                raw_arguments = raw_arguments if isinstance(raw_arguments, str) else ""
                call_id = item.get("call_id")
                name = item.get("name")
                if isinstance(call_id, str) and isinstance(name, str) and name:
                    calls.append(
                        ToolCall(
                            call_id=call_id,
                            name=name,
                            arguments=parse_tool_arguments(raw_arguments),
                            raw_arguments=raw_arguments[:MAX_TOOL_ARGUMENT_CHARS],
                        )
                    )
            if item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    text_parts.append(part["text"].strip())
                elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                    refusal = part["refusal"].strip()
        return LlmResponse(
            text="\n".join(part for part in text_parts if part),
            tool_calls=tuple(calls),
            finish_reason=status,
            refusal=refusal,
            tokens_in=_clamp_tokens(usage.get("input_tokens")),
            tokens_out=_clamp_tokens(usage.get("output_tokens")),
            raw_message={
                "role": "assistant",
                "content": "\n".join(part for part in text_parts if part) or None,
                "tool_calls": [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.raw_arguments,
                        },
                    }
                    for call in calls
                ],
                "_responses_output": output,
            },
        )

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
        finish_reason = str(choice.get("finish_reason") or "")
        raw_refusal = message.get("refusal")
        refusal = raw_refusal.strip() if isinstance(raw_refusal, str) else ""
        if finish_reason == "length":
            # Same contract as the Responses transport. Generation stopped early, so
            # partial prose can read as a finished sentence and a function call can look
            # well formed while its argument string is cut mid-JSON -- which
            # `parse_tool_arguments` silently normalises to `{}` rather than raising.
            # Expose neither prose nor calls: the caller sees only a truncation, and
            # cannot dispatch half a generated intent.
            #
            # The refusal survives the strip. It is a decided answer about the request,
            # not a fragment of generated output, and dropping it turned a model that
            # refused and then ran out of budget into a plain "truncated" -- the weaker
            # and less honest of the two reason codes.
            _LOG.warning(
                "llm chat reply stripped reason=truncated model=%s tokens_out=%s",
                self._model,
                _clamp_tokens(usage.get("completion_tokens")),
            )
            return LlmResponse(
                text="",
                tool_calls=(),
                finish_reason="length",
                refusal=refusal,
                tokens_in=_clamp_tokens(usage.get("prompt_tokens")),
                tokens_out=_clamp_tokens(usage.get("completion_tokens")),
                raw_message={"role": "assistant", "content": ""},
            )
        content = message.get("content")
        return LlmResponse(
            text=_message_text(content),
            tool_calls=self._parse_tool_calls(message),
            finish_reason=finish_reason,
            refusal=refusal,
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
    # 400 without tools is a payload bug (every model rejects it). 400 *with* tools is
    # often this model rejecting the tool schema / parallel_tool_calls; the next model
    # in the chain may still answer. 429/500/503 are load, not access — they raise so
    # the caller sees a transient failure rather than silently demoting.
    ADVANCE_ON: frozenset[int] = frozenset({403, 404, 410})

    # "This provider is down or refusing right now." Advancing to another rung on the
    # SAME host burns the fallback on the same outage, which is why these do not appear
    # in ADVANCE_ON. Crossing to a DIFFERENT provider is the whole point of the Gemini
    # rung, so those statuses do advance when the next client is another host.
    ADVANCE_ON_CROSS_PROVIDER: frozenset[int] = frozenset(
        {401, 408, 409, 425, 429, 500, 502, 503, 504}
    )

    def __init__(self, clients: list[LlmClient]) -> None:
        self._clients = [client for client in clients if client.enabled()]
        self._active_index = 0
        self.last_model = ""
        self.errors: list[str] = []

    def enabled(self) -> bool:
        return bool(self._clients)

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(client.model for client in self._clients)

    @property
    def model(self) -> str:
        """The active rung, useful for a release probe without exposing credentials."""
        if not self._clients:
            return ""
        return self._clients[self._active_index].model

    def reachable_rungs(self, messages: object) -> int:
        """How many rungs `complete` would actually TRY for these messages.

        Not the same as `len(self.models)`, and the difference is load-bearing for
        any caller deciding whether to impose a per-attempt cap. On a tool
        continuation this chain deliberately skips sibling rungs on the SAME
        provider (encrypted Responses reasoning belongs to the endpoint that
        produced it), so an OpenAI-primary + OpenAI-fallback chain has exactly ONE
        reachable rung on every step after a tool call. Capping that rung buys
        nothing -- there is no sibling left to protect -- and only converts a call
        that would have succeeded into a provider error.

        Deliberately computed here rather than by a caller reproducing the
        candidate-index logic: two copies of this rule would drift, and the
        `complete` below is the only thing that decides what "reachable" means.
        """
        if not self._clients:
            return 0
        if not _has_tool_continuation(messages):
            return len(self._clients)
        active_provider = self._clients[self._active_index].provider
        return 1 + sum(
            1
            for later in self._clients[self._active_index + 1 :]
            if later.provider != active_provider
        )

    def complete(self, **kwargs: Any) -> LlmResponse:
        if not self._clients:
            raise LlmError("no model configured")
        self.errors = []
        last: LlmError | None = None
        # Chain-only cap on ONE rung's own attempt, independent of the overall
        # chain deadline below (`timeout=`). Popped here and never forwarded to
        # `LlmClient.complete`, which has no `attempt_timeout` parameter and would
        # raise TypeError if this reached it. This is the RC2 fix: without it, a
        # slow/unavailable primary rung could hold the entire `timeout=` chain
        # deadline for itself, leaving nothing for a fallback rung behind it.
        attempt_timeout = kwargs.pop("attempt_timeout", None)
        continuation = _has_tool_continuation(kwargs.get("messages"))
        if not continuation:
            # A reusable purpose client begins each independent turn at its primary.
            self._active_index = 0
        requested_timeout = kwargs.get("timeout")
        deadline = (
            monotonic() + float(requested_timeout)
            if isinstance(requested_timeout, (int, float)) and requested_timeout > 0
            else None
        )
        if continuation:
            active_provider = self._clients[self._active_index].provider
            candidate_indexes = [self._active_index]
            candidate_indexes.extend(
                index
                for index in range(self._active_index + 1, len(self._clients))
                if self._clients[index].provider != active_provider
            )
        else:
            candidate_indexes = list(range(len(self._clients)))
        for index in candidate_indexes:
            client = self._clients[index]
            self.last_model = client.model
            # The first rung actually tried in THIS call is "model_primary" (under
            # continuation that may not be `self._clients[0]`); every later rung
            # reached only because an earlier one failed is "model_fallback". Kept
            # as "model_attempt" would still work (`_ALLOWED_STAGES` keeps it valid
            # for any other reader), but this split is what lets a log reader tell
            # a starved fallback apart from a failed primary.
            stage_name = "model_primary" if index == candidate_indexes[0] else "model_fallback"
            try:
                attempt_kwargs = dict(kwargs)
                if deadline is not None:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        log_timeout_stage(stage_name)
                        raise LlmError("llm request failed: deadline exceeded")
                    attempt_kwargs["timeout"] = remaining
                if attempt_timeout is not None:
                    # `min(attempt_timeout, remaining_chain_budget)` per the
                    # contract: this rung never gets more than its own configured
                    # cap, and never more than what the chain has left either.
                    existing = attempt_kwargs.get("timeout")
                    attempt_kwargs["timeout"] = (
                        min(attempt_timeout, existing)
                        if isinstance(existing, (int, float))
                        else attempt_timeout
                    )
                with owner_stage(stage_name, model=client.model):
                    response = client.complete(**attempt_kwargs)
            except LlmError as exc:
                self.errors.append(f"{client.model}:{_llm_error_class(exc)}")
                status = _status_from_error(exc)
                is_last = index == candidate_indexes[-1]
                tools_sent = bool(kwargs.get("tools"))
                # Look past sibling rungs: with OpenAI primary + OpenAI fallback +
                # Gemini, the primary must still advance so the chain can walk through
                # the sibling and reach the other provider.
                crosses_provider = any(
                    later.provider != client.provider for later in self._clients[index + 1 :]
                )
                if (
                    status is not None
                    and status not in self.ADVANCE_ON
                    and not (status == 400 and tools_sent)
                    and not (status in self.ADVANCE_ON_CROSS_PROVIDER and crosses_provider)
                    and not is_last
                ):
                    # Load or transport problem, not a model problem. Do not spend the
                    # fallback on it. A 400 with tools is the exception: this model may
                    # reject the tool payload while the next one accepts it.
                    raise
                last = exc
                continue
            if response.truncated():
                # A budget exhausted by this model's own reasoning is not a dead model,
                # and it must stop being relabelled "empty_reply" -- that is why callers
                # never saw a truncation on this transport. But how far it travels is
                # the same question ADVANCE_ON_CROSS_PROVIDER already answers. A sibling
                # rung on the SAME host runs the same prompt into the same ceiling, so
                # spending it fixes nothing. A DIFFERENT provider does not reproduce it:
                # the OpenAI Responses truncation is that model's own reasoning eating
                # the output budget, which the Gemini rung never spends. Both real
                # chains are OpenAI rungs then a Gemini rung, so the cross-provider
                # rescue is the normal case and returning here would drop the website
                # lead the Gemini rung would have classified. Gemini is last, so a
                # truncation there still reaches the caller and still fails closed.
                #
                # Deliberately computed here rather than shared with the LlmError
                # branch's `crosses_provider`: that one looks past sibling rungs in
                # `self._clients`, this one looks only at rungs that will actually be
                # tried. On these chains the two agree, but they answer different
                # questions and are kept apart so neither drifts into the other.
                remaining = candidate_indexes[candidate_indexes.index(index) + 1 :]
                self.errors.append(f"{client.model}:truncated")
                if any(self._clients[i].provider != client.provider for i in remaining):
                    _LOG.warning(
                        "llm chain advancing past truncation reason=truncated "
                        "model=%s tokens_out=%s",
                        client.model,
                        response.tokens_out,
                    )
                    last = LlmError("llm request failed: truncated")
                    continue
                _LOG.warning(
                    "llm chain returned truncation reason=truncated model=%s tokens_out=%s",
                    client.model,
                    response.tokens_out,
                )
                self._active_index = index
                return response
            if not response.text and not response.tool_calls:
                # A 200 with empty content is how some model ids fail live: the account
                # can "call" them, they return no prose and no tools, and the owner
                # console falls through to the NOTE failure line. Try the next model.
                self.errors.append(f"{client.model}:empty_reply")
                last = LlmError("llm request failed: empty reply HTTP 200")
                continue
            # Tool continuation state, especially encrypted Responses reasoning,
            # belongs to the endpoint/model that produced it. Stay on that rung for
            # the rest of this agent turn; only advance if it later fails.
            self._active_index = index
            return response
        raise last or LlmError("all models failed")


def _has_tool_continuation(messages: object) -> bool:
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool" or isinstance(message.get("tool_calls"), list):
            return True
        output = message.get("_responses_output")
        if isinstance(output, list) and any(
            isinstance(item, dict) and item.get("type") == "function_call" for item in output
        ):
            return True
    return False


def _llm_error_class(error: LlmError) -> str:
    """Stable diagnostics without provider bodies, prompts, credentials, or media."""
    status = _status_from_error(error)
    if status == 429:
        return "rate_limited"
    if status in {401, 403}:
        return "access_denied"
    if status in {404, 410}:
        return "model_unavailable"
    if status is not None and 500 <= status <= 599:
        return "provider_unavailable"
    text = str(error).lower()
    if "deadline" in text or "timeout" in text:
        return "timeout"
    if "not json" in text or "not an object" in text or "no output" in text:
        return "invalid_response"
    if "empty" in text:
        return "empty_response"
    return "request_failed"


def build_site_client(settings: Any) -> LlmModelChain:
    """Purpose-specific website chain: OpenAI Responses, then Gemini compatibility."""
    timeout = settings.llm_request_timeout_seconds
    clients: list[LlmClient] = [
        LlmClient(
            api_key=settings.openai_api_key,
            model=name,
            url=OPENAI_RESPONSES_URL,
            timeout=timeout,
            reasoning_effort=settings.sales_reasoning_effort,
        )
        for name in model_chain(settings.sales_model, settings.sales_fallback_model)
    ]
    if settings.gemini_api_key.strip() and settings.sales_gemini_model.strip():
        clients.append(
            LlmClient(
                api_key=settings.gemini_api_key,
                model=settings.sales_gemini_model.strip(),
                url=GEMINI_CHAT_URL,
                timeout=timeout,
                reasoning_effort=settings.sales_reasoning_effort,
            )
        )
    return LlmModelChain(clients)


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
