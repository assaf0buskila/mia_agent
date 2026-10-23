"""Owner model/embedding HTTP pool; adapters and model chains remain per request.

Like site_http, the pool is lazy, locked, and has a process-exit safety net. It holds
no credentials, conversation, or fallback state. Reject cookies so a provider response
cannot persist request state into another turn. The website keeps its own pool.
"""

from __future__ import annotations

import atexit
from http.cookiejar import CookieJar, DefaultCookiePolicy
from threading import Lock

import httpx

from app.integrations.deadline_http import enable_http_deadlines

_LOCK = Lock()
_CLIENT: httpx.Client | None = None


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request) -> bool:
        return False

    def return_ok(self, cookie, request) -> bool:
        return False


def get_owner_http_client() -> httpx.Client:
    global _CLIENT
    with _LOCK:
        if _CLIENT is None or _CLIENT.is_closed:
            _CLIENT = httpx.Client(
                # Embeddings without a turn deadline retain their 30s adapter default;
                # LLM calls and timed retrieval override this on each request.
                timeout=30.0,
                cookies=CookieJar(policy=_NoCookies()),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
            enable_http_deadlines(_CLIENT)
        return _CLIENT


def close_owner_http_client() -> None:
    """Call after owner requests/workers drain; atexit also covers standalone callers."""
    global _CLIENT
    with _LOCK:
        client, _CLIENT = _CLIENT, None
    if client is not None and not client.is_closed:
        client.close()


atexit.register(close_owner_http_client)
