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
        "tool",
        "send",
        "learning",
        "owner_turn",
    }
)
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
