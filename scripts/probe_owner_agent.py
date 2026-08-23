"""Prove which models this key can actually call, and that the owner agent runs.

    uv run python scripts/probe_owner_agent.py

Why this exists: `/health` reports `owner_agent: ready` when the model *string* is
non-empty. It cannot tell whether OpenAI accepts that model. For a full day the owner
console looked healthy while every turn silently fell back to the pre-brain keyword
classifier, because a configured-but-unusable model is indistinguishable from normal
operation until you look at the HTTP status.

Two layers, per OpenAI's documented behaviour:

1. `GET /v1/models/{id}` — cheap, and the only documented early warning for retirement
   (`shutdown_date`). It is NOT authoritative for "can this key call it": the docs never
   state the list is scoped to project permissions.
2. One real completion capped at a single output token — the only check that actually
   proves callability.

Prints statuses and error codes, never the API key. Read-only.
"""

from __future__ import annotations

import json

import httpx
from app.core.config import get_settings
from app.core.models import model_chain

MODELS_URL = "https://api.openai.com/v1/models"
CHAT_URL = "https://api.openai.com/v1/chat/completions"
_TIMEOUT = 30.0


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _describe_error(response: httpx.Response) -> str:
    """Pull type/code/message out of the error envelope. Never prints the key."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return json.dumps(body)[:200]
    parts = [
        f"type={error.get('type')}",
        f"code={error.get('code')}",
        f"message={error.get('message')}",
    ]
    return " ".join(str(part) for part in parts)[:300]


def _retrieve(client: httpx.Client, key: str, model: str) -> None:
    try:
        response = client.get(f"{MODELS_URL}/{model}", headers=_headers(key))
    except httpx.HTTPError as exc:
        print(f"    GET /v1/models/{model}: TRANSPORT FAILURE ({type(exc).__name__})")
        return
    if response.status_code == 200:
        body = response.json()
        shutdown = body.get("shutdown_date")
        note = f" shutdown_date={shutdown}" if shutdown else ""
        print(f"    GET  exists=yes owned_by={body.get('owned_by')}{note}")
        if shutdown:
            print("         WARNING: this model has a retirement date")
    else:
        print(f"    GET  exists=NO  http={response.status_code} {_describe_error(response)}")


def _call(client: httpx.Client, key: str, model: str) -> bool:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_completion_tokens": 1,
    }
    try:
        response = client.post(CHAT_URL, json=payload, headers=_headers(key))
    except httpx.HTTPError as exc:
        print(f"    CALL TRANSPORT FAILURE ({type(exc).__name__})")
        return False
    if response.status_code == 200:
        print("    CALL ok  -> this key CAN use this model")
        return True
    detail = _describe_error(response)
    print(f"    CALL FAILED http={response.status_code} {detail}")
    if response.status_code in (403, 404):
        print(
            "         Most likely a project model-permissions allowlist/denylist.\n"
            "         Check: OpenAI dashboard -> project -> Limits -> Model usage,\n"
            "         or GET /organization/projects/{project_id}/model_permissions"
        )
    elif response.status_code == 429:
        print("         Rate limit or billing. Billing errors do not recover on retry.")
    elif response.status_code == 400:
        print("         Bad request shape, not access. Every model would reject this.")
    return False


def main() -> int:
    settings = get_settings()
    key = settings.openai_api_key.strip()
    if not key:
        print("MIA_OPENAI_API_KEY is not set.")
        return 2

    configured: list[tuple[str, str]] = []
    for name in model_chain(
        settings.owner_agent_model, settings.owner_agent_fallback_model
    ):
        configured.append(("owner agent", name))
    if settings.extraction_model.strip():
        configured.append(("memory extraction", settings.extraction_model.strip()))
    for name in model_chain(settings.sales_model, settings.sales_fallback_model):
        configured.append(("website sales (known-good reference)", name))
    if settings.embedding_model.strip():
        configured.append(("embeddings", settings.embedding_model.strip()))

    if not configured:
        print("No models configured. Set MIA_OWNER_AGENT_MODEL at minimum.")
        return 2

    print("Owner agent model probe. Read-only; the API key is never printed.\n")
    usable = 0
    seen: set[str] = set()
    with httpx.Client(timeout=_TIMEOUT) as client:
        for role, model in configured:
            print(f"[{role}] {model}")
            if model in seen:
                print("    (already probed above)")
                continue
            seen.add(model)
            _retrieve(client, key, model)
            # Embeddings are not chat models; a chat call would 400 for the wrong reason.
            if role == "embeddings":
                print("    CALL skipped (embedding model, not a chat model)")
            elif _call(client, key, model):
                usable += 1
            print()

    print(f"{usable} chat model(s) callable by this key.")
    if not usable:
        print(
            "\nThe owner agent cannot run. Telegram will keep answering with the old\n"
            "keyword classifier until at least one chat model above returns CALL ok."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
