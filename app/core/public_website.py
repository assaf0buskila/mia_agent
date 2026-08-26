"""Server-side origin bind + rate limits for the public Ask Mia HTTP surface.

Browser CORS in ``app.main`` only withholds ``Access-Control-Allow-Origin``.
It does not stop curl (or a stolen widget URL) from burning STT/LLM. These
four POSTs therefore fail closed on unknown/missing Origin and are capped
per client IP (and per session where a session id exists).

The widget posts with ``credentials: 'omit'``; the browser still sends
``Origin``. Allowlist is ``MIA_CORS_ORIGINS`` plus the public host so the
same-origin preview page keeps working.
"""

from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock
from time import monotonic
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from app.core.config import Settings, get_settings

WINDOW_SECONDS = 900

LIMITS_PER_IP = {
    "session": 30,
    "message": 40,
    "voice": 12,
    "handoff": 8,
}

LIMITS_PER_SESSION = {
    "message": 40,
    "voice": 8,
    "handoff": 4,
}


class _SlidingWindow:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str, *, limit: int, window_s: float = WINDOW_SECONDS) -> bool:
        now = monotonic()
        cutoff = now - window_s
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_limiter = _SlidingWindow()


def reset_public_website_limiter() -> None:
    _limiter.reset()


def allowed_website_origins(settings: Settings) -> frozenset[str]:
    origins: set[str] = set()
    for item in settings.cors_origin_list():
        cleaned = _normalize_origin(item)
        if cleaned:
            origins.add(cleaned)
    public = _origin_from_url(settings.public_base_url)
    if public:
        origins.add(public)
    origins.discard("*")
    origins.discard("null")
    return frozenset(origins)


def origin_allowed(origin: str, settings: Settings) -> bool:
    cleaned = _normalize_origin(origin)
    if not cleaned or cleaned == "null":
        return False
    return cleaned in allowed_website_origins(settings)


def client_ip(request: Request) -> str:
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


def enforce_public_website(request: Request, *, bucket: str) -> None:
    settings = get_settings()
    origin = request.headers.get("origin", "")
    if not origin_allowed(origin, settings):
        raise HTTPException(status_code=403, detail="origin not allowed")
    ip = client_ip(request)
    ip_limit = LIMITS_PER_IP[bucket]
    if not _limiter.allow(f"{bucket}:ip:{ip}", limit=ip_limit):
        raise HTTPException(
            status_code=429,
            detail="rate limited",
            headers={"Retry-After": str(WINDOW_SECONDS)},
        )
    session_id = request.path_params.get("session_id")
    session_limit = LIMITS_PER_SESSION.get(bucket)
    if session_id and session_limit is not None:
        if not _limiter.allow(f"{bucket}:session:{session_id}", limit=session_limit):
            raise HTTPException(
                status_code=429,
                detail="rate limited",
                headers={"Retry-After": str(WINDOW_SECONDS)},
            )


def public_website_guard(bucket: str):
    def _guard(request: Request) -> None:
        enforce_public_website(request, bucket=bucket)

    return _guard


def _normalize_origin(value: str) -> str:
    return value.strip().rstrip("/")


def _origin_from_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return _normalize_origin(f"{parsed.scheme}://{parsed.netloc}")
