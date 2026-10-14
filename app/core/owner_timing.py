"""Bounded, privacy preserving timing for the owner turn and the owner OS routes."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from time import monotonic

_LOG = logging.getLogger("mia.owner_timing")
_TURN_STAGES = frozenset(
    {
        "coalesce",
        "stt",
        "image",
        "retrieval",
        "prefetch",
        "model",
        "model_attempt",
        "tool",
        "send",
        "learning",
        "owner_turn",
    }
)
# Owner OS route stages. os_mirror is reserved for the PostgreSQL mirror worker.
_OS_STAGES = frozenset({"os_auth", "os_read", "os_commit", "os_verify", "os_mirror"})
_ALLOWED_STAGES = _TURN_STAGES | _OS_STAGES
# The only Server-Timing names the backend may emit, in emission order.
# "ledger" is reserved for the PostgreSQL authority and has no emitter yet.
SERVER_TIMING_METRICS = ("auth", "db", "script", "ledger", "verify")
# Timings must never let a client tell unknown, expired or revoked credentials apart.
_NO_TIMING_STATUSES = frozenset({401, 403})
_MAX_MS = 2_147_483_647
_SAFE = re.compile(r"[^a-zA-Z0-9_.:-]")
_REASON = re.compile(r"[a-z][a-z0-9_]{1,47}")
_KNOWN_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4", "gemini-", "claude-", "text-")
_SOURCE_HASH: ContextVar[str] = ContextVar("mia_owner_source_hash", default="")


class RequestTiming:
    """Phase totals for one owner OS request, rendered as an allow-listed Server-Timing value."""

    __slots__ = ("authenticated", "_totals")

    def __init__(self) -> None:
        self.authenticated = False
        self._totals: dict[str, int] = {}

    def add(self, metric: str, ms: float) -> None:
        if metric not in SERVER_TIMING_METRICS:
            raise ValueError("unknown_server_timing_metric")
        self._totals[metric] = min(_MAX_MS, self._totals.get(metric, 0) + max(0, int(ms)))

    def header(self, status: int) -> str:
        """Return the header value, or "" when this response must not carry timings."""
        if not self.authenticated or status in _NO_TIMING_STATUSES:
            return ""
        return ", ".join(
            f"{name};dur={self._totals[name]}"
            for name in SERVER_TIMING_METRICS
            if name in self._totals
        )

    @contextmanager
    def activate(self) -> Iterator[RequestTiming]:
        token = _REQUEST.set(self)
        try:
            yield self
        finally:
            _REQUEST.reset(token)


_REQUEST: ContextVar[RequestTiming | None] = ContextVar("mia_owner_request_timing", default=None)


def mark_owner_authenticated() -> None:
    """Allow Server-Timing on this request's response; call only once credentials are proven."""
    timing = _REQUEST.get()
    if timing is not None:
        timing.authenticated = True


def record_server_timing(metric: str, ms: float) -> None:
    timing = _REQUEST.get()
    if timing is not None:
        timing.add(metric, ms)


def _safe(value: str, limit: int = 80) -> str:
    return _SAFE.sub("_", str(value or ""))[:limit]


def _safe_tool(value: str) -> str:
    if not value:
        return ""
    try:
        from app.tools.registries.owner_tools import get_tool

        return _safe(value, 64) if get_tool(value) is not None else "unknown"
    except Exception:
        return "unknown"


def _safe_model(value: str) -> str:
    raw = str(value or "").strip()
    return _safe(raw, 64) if raw.startswith(_KNOWN_MODEL_PREFIXES) else "configured"


def _reason(error: BaseException) -> str:
    """A snake_case code the application itself attached; never the exception message."""
    for attribute in ("code", "detail"):
        value = getattr(error, attribute, None)
        if isinstance(value, str) and _REASON.fullmatch(value):
            return value
    return "error"


@contextmanager
def owner_stage(
    stage: str,
    *,
    source_ref: str = "",
    model: str = "",
    tool: str = "",
    metric: str = "",
) -> Iterator[None]:
    """Measure one stage while emitting only an allowlisted, bounded event.

    An unknown stage raises. An owner OS stage may also add its duration to the active request's
    Server-Timing metric; the metric name must be one of SERVER_TIMING_METRICS.
    """
    if stage not in _ALLOWED_STAGES:
        raise ValueError("unknown_owner_stage")
    if metric and (stage not in _OS_STAGES or metric not in SERVER_TIMING_METRICS):
        raise ValueError("unknown_server_timing_metric")
    started = monotonic()
    outcome = "ok"
    reason = "none"
    source_hash = hashlib.sha256(
        str(source_ref or "").encode("utf-8", "replace")
    ).hexdigest()[:16] if source_ref else _SOURCE_HASH.get()
    token = _SOURCE_HASH.set(source_hash)
    try:
        yield
    except asyncio.CancelledError:
        outcome, reason = "cancelled", "cancelled"
        raise
    except Exception as error:
        outcome, reason = "error", _reason(error)
        raise
    finally:
        elapsed = min(_MAX_MS, max(0, int((monotonic() - started) * 1000)))
        if stage in _OS_STAGES:
            _LOG.info(
                "owner_stage stage=%s metric=%s outcome=%s latency_ms=%d reason=%s",
                stage,
                metric or "none",
                outcome,
                elapsed,
                reason,
            )
        else:
            _LOG.info(
                "owner_stage stage=%s outcome=%s latency_ms=%d model=%s tool=%s source_ref_hash=%s",
                stage,
                outcome,
                elapsed,
                _safe_model(model),
                _safe_tool(tool),
                source_hash,
            )
        if metric:
            record_server_timing(metric, elapsed)
        _SOURCE_HASH.reset(token)


def owner_phase(metric: str, stage: str) -> AbstractContextManager[None]:
    """owner_stage plus a Server-Timing metric, and silent outside an owner OS request.

    The shared ports also serve Telegram and the owner tools; those callers have no active
    request, so they neither log OS stages nor accumulate a metric.
    """
    if metric not in SERVER_TIMING_METRICS or stage not in _OS_STAGES:
        raise ValueError("unknown_server_timing_phase")
    if _REQUEST.get() is None:
        return nullcontext()
    return owner_stage(stage, metric=metric)
