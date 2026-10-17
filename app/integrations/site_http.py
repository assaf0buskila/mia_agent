"""Website-only reusable HTTP transport for model calls.

The client carries no authentication or visitor state. Each LLM request supplies its
own Authorization header and timeout, so connection pooling cannot cross those values.
"""

from __future__ import annotations

import atexit
from threading import Lock

import httpx

_LOCK = Lock()
_CLIENT: httpx.Client | None = None


def get_site_http_client() -> httpx.Client:
    global _CLIENT
    with _LOCK:
        if _CLIENT is None or _CLIENT.is_closed:
            _CLIENT = httpx.Client(
                timeout=None,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return _CLIENT


def close_site_http_client() -> None:
    global _CLIENT
    with _LOCK:
        client, _CLIENT = _CLIENT, None
    if client is not None and not client.is_closed:
        client.close()


atexit.register(close_site_http_client)
