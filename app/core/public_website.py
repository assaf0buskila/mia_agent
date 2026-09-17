"""Server-side origin bind + rate limits for the public Ask Mia HTTP surface.

Browser CORS in ``app.main`` only withholds ``Access-Control-Allow-Origin``.
It does not stop curl (or a stolen widget URL) from burning STT/LLM. These
four POSTs therefore fail closed on unknown/missing Origin and are capped
per client IP (and per session where a session id exists).

The widget posts with ``credentials: 'omit'``; the browser still sends
``Origin``. Allowlist is ``MIA_CORS_ORIGINS`` plus the public host so the
same-origin preview page keeps working.

``Origin`` is the only request-supplied value this guard may key off. Fetch
appends ``Origin`` to every request whose method is not GET/HEAD, and every
route behind this guard is a POST, so a browser -- widget, preview harness or
a same-origin call from the app itself -- always sends one. An earlier
fallback accepted a missing ``Origin`` when ``Sec-Fetch-Site`` said
``same-origin``/``same-site`` or ``Referer`` named an allowlisted origin, and
then synthesised the origin from the request's own base URL. All three are
attacker-supplied strings, and ``Sec-Fetch-Site`` is a forbidden header name
that page JavaScript cannot set, so the fallback protected no real client
while letting ``curl -H 'Sec-Fetch-Site: same-origin'`` through the whole
boundary. It is gone; nothing in the tree needed it (the widget is a browser,
and ``scripts/probe_live_website.py``, ``scripts/smoke_production.py`` and
``scripts/smoke_website_telegram.py`` all send ``Origin`` explicitly).
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from ipaddress import ip_address
from threading import Lock
from time import monotonic
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from app.core.config import MiaEnv, Settings, get_settings

_LOG = logging.getLogger(__name__)

WINDOW_SECONDS = 900

LIMITS_PER_IP = {
    "session": 30,
    # The widget emits page/section/CTA/form lifecycle events, usually fewer than
    # twenty per visit. Keep a generous IP ceiling while bounding scripted floods.
    "message": 40,
    "event": 40,
    "voice": 12,
    "handoff": 8,
    # /end triggers finalization: a summary plus a Telegram push to Assaf. The widget
    # fires it once per session on pagehide, so real traffic is far under this.
    "end": 20,
}

LIMITS_PER_SESSION = {
    "message": 40,
    "voice": 8,
    "handoff": 4,
    "end": 4,
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


def client_ip(request: Request, *, settings: Settings | None = None) -> str:
    """Return the validated rate-limit peer for the deployed network shape.

    Production runs behind an ALB configured to append its peer to X-Forwarded-For.
    Earlier entries are client supplied and must not select the rate-limit bucket.
    Outside production there is no trusted proxy boundary, so forwarded headers are
    ignored and the direct request peer is used.
    """
    settings = settings or get_settings()
    if settings.env == MiaEnv.PROD:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return _validated_ip(forwarded.rsplit(",", maxsplit=1)[-1])
    if request.client is not None and request.client.host:
        return _validated_ip(request.client.host)
    return "unknown"


def _origin_rejected(bucket: str, *, reason: str) -> HTTPException:
    """Build the 403 and log the one line that makes a real rejection visible.

    Only the bucket and a fixed reason code are logged. The Origin/Referer/
    Sec-Fetch-Site values are attacker supplied, so they never reach the log.
    """
    _LOG.warning("website origin bind rejected bucket=%s reason=%s", bucket, reason)
    return HTTPException(status_code=403, detail="origin not allowed")


def _rate_limited(bucket: str, *, reason: str) -> HTTPException:
    """Build the 429 and log it under its own reason code.

    The per-IP and per-session ceilings are tuned separately, so a flood has to be
    attributable to the one that actually fired. The session id is not logged.
    """
    _LOG.warning("website request rate limited bucket=%s reason=%s", bucket, reason)
    return HTTPException(
        status_code=429,
        detail="rate limited",
        headers={"Retry-After": str(WINDOW_SECONDS)},
    )


def enforce_public_website(request: Request, *, bucket: str) -> None:
    settings = get_settings()
    origin = request.headers.get("origin", "")
    if not origin_allowed(origin, settings):
        # Absent and present-but-unknown are different operational stories. Absent
        # means a client that is not a browser (see the module docstring); unknown
        # means an embed on a host nobody put in MIA_CORS_ORIGINS.
        raise _origin_rejected(
            bucket,
            reason="origin_header_absent" if not origin.strip() else "origin_not_allowlisted",
        )
    ip = client_ip(request, settings=settings)
    ip_limit = LIMITS_PER_IP[bucket]
    if not _limiter.allow(f"{bucket}:ip:{ip}", limit=ip_limit):
        raise _rate_limited(bucket, reason="rate_limit_per_ip")
    session_id = request.path_params.get("session_id")
    session_limit = LIMITS_PER_SESSION.get(bucket)
    if session_id and session_limit is not None:
        if not _limiter.allow(f"{bucket}:session:{session_id}", limit=session_limit):
            raise _rate_limited(bucket, reason="rate_limit_per_session")


def public_website_guard(bucket: str):
    """Build the per-route guard, failing at import time on an unknown bucket.

    `enforce_public_website` indexes LIMITS_PER_IP directly, so a typo used to raise
    KeyError inside the request and surface as a 500 on a public endpoint -- the guard
    itself becoming the outage. Routes are declared at import, so validate here and
    let a bad bucket break startup loudly instead.
    """
    if bucket not in LIMITS_PER_IP:
        raise KeyError(
            f"unknown public-website rate-limit bucket {bucket!r}; "
            f"add it to LIMITS_PER_IP (known: {sorted(LIMITS_PER_IP)})"
        )

    def _guard(request: Request) -> None:
        enforce_public_website(request, bucket=bucket)

    return _guard


def _normalize_origin(value: str) -> str:
    return value.strip().rstrip("/")


def _validated_ip(value: str) -> str:
    try:
        return str(ip_address(value.strip()))
    except ValueError:
        return "unknown"


def _origin_from_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return _normalize_origin(f"{parsed.scheme}://{parsed.netloc}")
