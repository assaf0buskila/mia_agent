import re
from typing import Any

_BOT_URL = re.compile(
    r"(https://api\.telegram\.org/(?:file/)?bot)[0-9]+:[A-Za-z0-9_-]+",
    re.IGNORECASE,
)

_REDACT_KEYS = {
    "password",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "secret",
    "composio_api_key",
    "composio_webhook_secret",
    "firecrawl_api_key",
    "apify_token",
    "database_url",
    "phone",
    "email",
    "raw_audio",
}


def redact(value: Any) -> Any:
    """Strip secrets and PII from structures before they hit logs or traces."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in _REDACT_KEYS:
                out[key] = "[redacted]"
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _BOT_URL.sub(r"\1[redacted]", value)
    return value
