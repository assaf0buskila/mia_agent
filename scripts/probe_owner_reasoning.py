"""Run inside the release container: real owner-model tool continuation, no house tools.

Uses the container's existing credentials without printing them. The only function
is a local constant; this cannot read mail, write CRM, or message anyone. Execute
with run_ecs_command.py and pass this file's contents as python -c code.
"""

import json
import logging
import sys

from app.core.config import get_settings
from app.domain.owner.brain import build_agent_client
from app.integrations.llm_client import function_tool


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        settings = get_settings()
        client = build_agent_client(settings)
        expected_model = settings.owner_agent_model
        messages = [
            {
                "role": "system",
                "content": "Call release_probe once, then reply with its returned code only.",
            },
            {"role": "user", "content": "Run the release probe."},
        ]
        tool = function_tool(
            name="release_probe",
            description="Returns a release verification code.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        )
        first = client.complete(
            messages=messages,
            tools=[tool],
            tool_choice="required",
            parallel_tool_calls=False,
            max_completion_tokens=1500,
            timeout=30,
        )
        if first.truncated() or first.refused() or len(first.tool_calls) != 1:
            raise ValueError("tool call contract")
        call = first.tool_calls[0]
        if call.name != "release_probe" or call.arguments:
            raise ValueError("unexpected tool")
        messages.append(first.raw_message)
        messages.append(
            {"role": "tool", "tool_call_id": call.call_id, "content": "MIA_REASONING_PROBE_OK"}
        )
        second = client.complete(
            messages=messages,
            tools=[tool],
            tool_choice="none",
            parallel_tool_calls=False,
            max_completion_tokens=1500,
            timeout=30,
        )
        if second.truncated() or second.refused() or second.tool_calls:
            raise ValueError("continuation contract")
        if second.text.strip() != "MIA_REASONING_PROBE_OK":
            raise ValueError("continuation output")
        if client.model != expected_model:
            raise ValueError("primary model not used")
        print(
            json.dumps(
                {
                    "result": "PASS",
                    "primary_model": client.model,
                    "reasoning": settings.owner_agent_reasoning_effort,
                    "function_calls": 1,
                    "continuation": True,
                    "tokens_in": first.tokens_in + second.tokens_in,
                    "tokens_out": first.tokens_out + second.tokens_out,
                }
            )
        )
        return 0
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
