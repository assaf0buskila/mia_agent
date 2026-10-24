"""Bounded, privacy preserving timing for the owner turn and the owner OS routes."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
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
        # Input preprocessing (media download, STT, transcript persistence,
        # coalescing) that must complete BEFORE the owner execution deadline
        # starts -- see app/workers/telegram_owner.py.
        "input_preprocess",
        # Emitted by LlmModelChain.complete instead of the bare "model_attempt"
        # so a reader can tell the first rung tried in one chain call apart
        # from a later rung reached only because an earlier one failed.
        "model_primary",
        "model_fallback",
        # The tool-less final prose turn in run_owner_agent (force_prose),
        # distinct from "model" (a tool-calling turn) for observability.
        "final_model",
        "tool",
        # Not a measured span -- a timeout reason code only (see _TIMEOUT_STAGES,
        # which is the gate `log_timeout_stage` actually checks; this set is
        # `owner_stage`'s). Listed here so the two sets stay in the subset relation
        # that `test_timeout_stages_are_a_subset_of_allowed_stages` pins, and so a
        # future `owner_stage("tool_abandoned")` would not raise `unknown_owner_stage`.
        "tool_abandoned",
        "send",
        "learning",
        "owner_turn",
    }
)
# Owner OS route stages. os_mirror is reserved for the PostgreSQL mirror worker.
_OS_STAGES = frozenset({"os_auth", "os_read", "os_commit", "os_verify", "os_mirror"})
_ALLOWED_STAGES = _TURN_STAGES | _OS_STAGES
# The subset of stages that can report a bounded-timeout reason code via
# `log_timeout_stage` below -- i.e. a child call that either never started, or
# started and was cut off, because too little of the parent budget was left.
#
# `tool` and `tool_abandoned` are deliberately two codes for what the owner sees
# as one message. `tool` means the dispatch was refused before its thread started,
# so no handler ran and no external write can have happened. `tool_abandoned`
# means the handler ran to completion and its result was discarded, so a write may
# genuinely have executed and a blind retry is NOT safe. That difference is
# invisible in the reply text and is the main thing a reader needs from these logs.
_TIMEOUT_STAGES = frozenset(
    {"model_primary", "model_fallback", "tool", "tool_abandoned", "final_model"}
)
# The only Server-Timing names the backend may emit, in emission order.
# "ledger" is the PostgreSQL authority (PostgresOsBackend and the v2 reads). "total" is the whole
# server-side request wall time and is last: only the OwnerOSBoundary sets it, never a stage.
SERVER_TIMING_METRICS = ("auth", "db", "script", "ledger", "verify", "total")
_PHASE_METRICS = tuple(name for name in SERVER_TIMING_METRICS if name != "total")
# Timings must never let a client tell unknown, expired or revoked credentials apart.
_NO_TIMING_STATUSES = frozenset({401, 403})
_MAX_MS = 2_147_483_647
_SAFE = re.compile(r"[^a-zA-Z0-9_.:-]")
_REASON = re.compile(r"[a-z][a-z0-9_]{1,47}")
_KNOWN_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4", "gemini-", "claude-", "text-")
_SOURCE_HASH: ContextVar[str] = ContextVar("mia_owner_source_hash", default="")
# The random id of the private request in flight (uuid4 hex, set by OwnerOSBoundary). It joins the
# server log lines to the response header and the phone; it identifies nothing else.
_REQUEST_ID: ContextVar[str] = ContextVar("mia_owner_request_id", default="")
_OPS = frozenset(
    {
        "snapshot", "schema", "cached", "propose", "proposal", "approve", "reject", "receipt",
        "pending", "proposal_request", "commit", "undo", "abandon", "deleted", "auth", "other",
    }
)


def current_request_id() -> str:
    """The id of the private request in flight, or ``"none"`` outside one."""
    return _REQUEST_ID.get() or "none"


def request_id_or_new() -> str:
    """The id in flight, else a fresh random one (an envelope rendered outside any request)."""
    rid = _REQUEST_ID.get()
    return rid if rid else uuid.uuid4().hex


@contextmanager
def request_id_scope(rid: str) -> Iterator[str]:
    """Hold ``rid`` as the current request id for the whole of one private request."""
    token = _REQUEST_ID.set(rid)
    try:
        yield rid
    finally:
        _REQUEST_ID.reset(token)


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
    if metric and (stage not in _OS_STAGES or metric not in _PHASE_METRICS):
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
                "owner_stage stage=%s metric=%s outcome=%s latency_ms=%d reason=%s rid=%s",
                stage,
                metric or "none",
                outcome,
                elapsed,
                reason,
                _safe(current_request_id(), 40),
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


def log_timeout_stage(stage: str, *, source_ref: str = "") -> None:
    """One allowlisted, bounded line for a child call bounded out of its budget.

    Emitted when `child_call_timeout` refuses to start a call (not enough of the
    parent turn budget left), or when a call that did start ran out its own
    bound -- so a CloudWatch reader can grep `timeout_stage=` and learn WHICH
    child ran out without any transcript, message, or provider content ever
    being logged. `stage` must be one of the five reason codes a reader can act
    on (`model_primary`, `model_fallback`, `tool`, `tool_abandoned`,
    `final_model`); anything else logs as "unknown" rather than silently
    accepting an unbounded value.

    `tool` vs `tool_abandoned` is the one distinction that decides whether a
    retry is safe: `tool` means the dispatch was refused before its thread
    started, so nothing ran; `tool_abandoned` means the handler completed and
    its result was discarded, so an external write may have executed. Both look
    identical to the owner, so this is the only place it survives.

    No `source_ref` given reuses the hash already set by an enclosing
    `owner_stage`, exactly like `owner_stage` itself does -- so calling this
    from inside `LlmModelChain.complete` (which has no source_ref of its own)
    still ties the line to the same turn as the wrapping "model"/"final_model"
    stage.
    """
    name = stage if stage in _TIMEOUT_STAGES else "unknown"
    source_hash = (
        hashlib.sha256(str(source_ref or "").encode("utf-8", "replace")).hexdigest()[:16]
        if source_ref
        else _SOURCE_HASH.get()
    )
    _LOG.info("owner_stage_timeout timeout_stage=%s source_ref_hash=%s", name, source_hash)


def log_owner_os_request(*, op: str, status: int, total_ms: int, rid: str) -> None:
    """The one summary line of a private owner request: a fixed op, a status, integer ms, the id.

    ``op`` comes from the boundary's fixed (method, path) table, never the raw path; anything
    outside it logs as ``other``.
    """
    _LOG.info(
        "owner_os_request op=%s status=%d total_ms=%d rid=%s",
        op if op in _OPS else "other",
        int(status),
        min(_MAX_MS, max(0, int(total_ms))),
        _safe(rid, 40),
    )


def owner_phase(metric: str, stage: str) -> AbstractContextManager[None]:
    """owner_stage plus a Server-Timing metric, and silent outside an owner OS request.

    The shared ports also serve Telegram and the owner tools; those callers have no active
    request, so they neither log OS stages nor accumulate a metric. ``total`` is refused: only the
    boundary sets it, once, for the whole request.
    """
    if metric not in _PHASE_METRICS or stage not in _OS_STAGES:
        raise ValueError("unknown_server_timing_phase")
    if _REQUEST.get() is None:
        return nullcontext()
    return owner_stage(stage, metric=metric)
