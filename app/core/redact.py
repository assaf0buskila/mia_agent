import re
from typing import Any
from urllib.parse import unquote, urlsplit

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


def _oauth_queries(value: str) -> str:
    """Visit each whitespace-delimited run once; never retry overlapping URI starts.

    Each run has one slash search, one query search and at most one URL parse/decode.
    Their total input length is <= len(value), so even malformed runs take O(n).
    Query punctuation is deliberately consumed through the end of the run, as before.
    """
    if "?" not in value:
        return value
    pieces = []
    copied = 0
    end = 0
    while end < len(value):
        if value[end].isspace():
            end += 1
            continue
        start = end
        while end < len(value) and not value[end].isspace():
            end += 1
        token = value[start:end]
        slash = token.find("/")
        if slash < 0:
            continue
        query = token.find("?", slash)
        if query < 0:
            continue
        uri_start = slash
        for scheme in ("https:", "http:"):
            candidate = slash - len(scheme)
            if candidate >= 0 and token[candidate:slash].lower() == scheme:
                uri_start = candidate
                break
        try:
            path = unquote(urlsplit(token[uri_start:query]).path).lower()
        except ValueError:
            continue
        if path == "/oauth" or path.startswith("/oauth/"):
            pieces.extend((value[copied:start + query + 1], "[redacted]"))
            copied = end
    if not pieces:
        return value
    pieces.append(value[copied:])
    return "".join(pieces)


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
        return _oauth_queries(_BOT_URL.sub(r"\1[redacted]", value))
    # A %-style log arg that isn't a str/dict/list (e.g. the `httpx.URL` object
    # httpx's own request logger passes as an arg, not a pre-formatted string)
    # would otherwise skip every branch above and reach the log untouched --
    # that gap let the Telegram bot token leak into CloudWatch. Only replace
    # the value when its string form actually contains a token: anything else
    # (an int status code formatted with %d, for instance) must come back
    # exactly as given or %-formatting downstream breaks.
    try:
        text = str(value)
    except Exception:
        return value
    cleaned = redact(text)
    return cleaned if cleaned != text else value
