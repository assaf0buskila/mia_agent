"""Bounded, privacy preserving timing for the owner turn."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

_LOG = logging.getLogger("mia.owner_timing")
_ALLOWED_STAGES = frozenset(
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
        "send",
        "learning",
        "owner_turn",
    }
)
# The subset of stages that can report a bounded-timeout reason code via
# `log_timeout_stage` below -- i.e. a child call that either never started, or
# started and was cut off, because too little of the parent budget was left.
_TIMEOUT_STAGES = frozenset({"model_primary", "model_fallback", "tool", "final_model"})
_SAFE = re.compile(r"[^a-zA-Z0-9_.:-]")
_KNOWN_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4", "gemini-", "claude-", "text-")
_SOURCE_HASH: ContextVar[str] = ContextVar("mia_owner_source_hash", default="")


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


@contextmanager
def owner_stage(
    stage: str, *, source_ref: str = "", model: str = "", tool: str = ""
) -> Iterator[None]:
    """Measure one stage while emitting only an allowlisted, bounded event."""
    name = _safe(stage, 32) if stage in _ALLOWED_STAGES else "unknown"
    started = monotonic()
    outcome = "ok"
    source_hash = hashlib.sha256(
        str(source_ref or "").encode("utf-8", "replace")
    ).hexdigest()[:16] if source_ref else _SOURCE_HASH.get()
    token = _SOURCE_HASH.set(source_hash)
    try:
        yield
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception:
        outcome = "error"
        raise
    finally:
        _LOG.info(
            "owner_stage stage=%s outcome=%s latency_ms=%d model=%s tool=%s source_ref_hash=%s",
            name,
            outcome,
            min(2_147_483_647, max(0, int((monotonic() - started) * 1000))),
            _safe_model(model),
            _safe_tool(tool),
            source_hash,
        )
        _SOURCE_HASH.reset(token)


def log_timeout_stage(stage: str, *, source_ref: str = "") -> None:
    """One allowlisted, bounded line for a child call bounded out of its budget.

    Emitted when `child_call_timeout` refuses to start a call (not enough of the
    parent turn budget left), or when a call that did start ran out its own
    bound -- so a CloudWatch reader can grep `timeout_stage=` and learn WHICH
    child ran out without any transcript, message, or provider content ever
    being logged. `stage` must be one of the four reason codes a reader can
    act on (`model_primary`, `model_fallback`, `tool`, `final_model`); anything
    else logs as "unknown" rather than silently accepting an unbounded value.

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
