"""Owner brain tools: durable memory and website-knowledge reads plus owner memory writes."""

from __future__ import annotations

from typing import Any

from app.brain.schemas import MemoryCategory, MemoryKind, MemorySource, clamp_importance
from app.capabilities.knowledge import knowledge_handlers
from app.capabilities.memory import memory_handlers
from app.capabilities.policy import execute_capability
from app.core.errors import PermissionDenied
from app.tools.owner.types import ToolContext, ToolResult

# ------------------------------------------------------------------ brain tools


def _search_memory(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, error="query is required")
    try:
        out = execute_capability(
            "memory.search",
            principal=ctx.principal,
            args={"query": query},
            handlers=memory_handlers(
                brain=ctx.brain,
                embedding_port=ctx.embedding_port,
                weights=ctx.weights(),
                now=ctx.now,
            ),
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="memory search denied")
    hits = out.get("hits") or []
    if not hits:
        return ToolResult(ok=True, text="No stored memory matches that.")
    ids = [str(hit.get("id") or "") for hit in hits if hit.get("id")]
    if ids:
        ctx.brain.touch_memories(ids)
    lines = [f"- [{hit.get('label') or 'memory'}] {hit.get('text') or ''}" for hit in hits]
    return ToolResult(ok=True, text="\n".join(lines))


def _search_knowledge(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, error="query is required")
    try:
        out = execute_capability(
            "knowledge.search",
            principal=ctx.principal,
            args={"query": query},
            handlers=knowledge_handlers(
                brain=ctx.brain,
                embedding_port=ctx.embedding_port,
            ),
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="knowledge search denied")
    hits = out.get("hits") or []
    if not hits:
        return ToolResult(ok=True, text="Nothing in the website knowledge base matches that.")
    lines = [f"- [{hit.get('label') or 'site'}] {hit.get('text') or ''}" for hit in hits]
    return ToolResult(ok=True, text="\n".join(lines))


def _remember(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Owner-scoped memory write. R1: never leaves the system, so it needs no approval."""
    text = str(args.get("text") or "").strip()
    if not text:
        return ToolResult(ok=False, error="text is required")
    if not ctx.settings.memory_write_enabled:
        return ToolResult(ok=False, error="memory writing is disabled")
    try:
        kind = MemoryKind(str(args.get("kind") or MemoryKind.SEMANTIC.value))
    except ValueError:
        kind = MemoryKind.SEMANTIC
    try:
        category = MemoryCategory(str(args.get("category") or MemoryCategory.OTHER.value))
    except ValueError:
        category = MemoryCategory.OTHER
    vector = None
    if ctx.embedding_port.enabled():
        vectors = ctx.embedding_port.embed([text])
        vector = vectors[0] if vectors else None
    ctx.brain.save_memory(
        text=text,
        kind=kind,
        category=category,
        importance=clamp_importance(args.get("importance", 6)),
        source=MemorySource.TELEGRAM,
        source_ref=ctx.source_ref,
        embedding=vector,
        embedding_model=ctx.embedding_port.model,
    )
    return ToolResult(ok=True, text="Stored.")


def _list_known_entities(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    entities = ctx.brain.list_entities(limit=25)
    if not entities:
        return ToolResult(ok=True, text="No entities recorded yet.")
    lines = [
        f"- {entity.name} ({entity.kind.value}, mentioned {entity.mention_count}x)"
        for entity in entities
    ]
    return ToolResult(ok=True, text="\n".join(lines))
