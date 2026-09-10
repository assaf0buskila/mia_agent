"""Owner brain tools: durable memory and website-knowledge reads plus owner memory writes."""

from __future__ import annotations

import re
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
    if not _has_explicit_remember_intent(ctx.owner_text):
        return ToolResult(
            ok=False,
            error="lasting memory requires an explicit remember request in this owner message",
        )
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
    # Webhook retries and repeated model calls for one owner event are no-ops. A
    # different owner event may deliberately reaffirm the same fact.
    for existing in ctx.brain.list_memories(limit=200):
        if existing.source_ref == ctx.source_ref and existing.text == text:
            return ToolResult(ok=True, text="Already stored for this message.")
    memory_id = ctx.brain.save_memory(
        text=text,
        kind=kind,
        category=category,
        importance=clamp_importance(args.get("importance", 6)),
        source=MemorySource.TELEGRAM,
        source_ref=ctx.source_ref,
        embedding=vector,
        embedding_model=ctx.embedding_port.model,
    )
    supersedes = str(args.get("supersedes_memory_id") or "").strip()
    if supersedes:
        old = ctx.brain.get_memory(supersedes)
        if old is None or not ctx.brain.supersede_memory(supersedes, replacement_id=memory_id):
            return ToolResult(
                ok=False,
                error="replacement was stored but the named prior memory was not active",
            )
    return ToolResult(ok=True, text="Stored.")


_REMEMBER_REQUEST_RE = re.compile(
    r"^(?:please\s+)?(?:remember\s+(?!when\b)(?:that\s+|this\s+|instead\s+)?\S|"
    r"(?:can|could|would)\s+you\s+(?:please\s+)?remember\s+(?:that|this)\b|"
    r"(?:save|store)\s+(?:this|that|the following).{0,20}(?:memory|remember)|"
    r"(?:correct|update|replace)\s+(?:your\s+)?memory\s*:|"
    r"(?:בבקשה\s+)?(?:תזכור|תזכרי|זכור|זכרי)(?:\s+(?:ש|את זה|במקום)|\s*:)|"
    r"(?:אפשר|האם תוכל|האם תוכלי)\s+(?:ש)?(?:תזכור|תזכרי)\s+ש|"
    r"(?:שמור|שמרי)(?:\s+(?:את זה\s+)?בזיכרון|\s*:)|"
    r"(?:תקן|תקני|עדכן|עדכני)\s+(?:את\s+)?הזיכרון\s*:)",
    re.IGNORECASE,
)
_REMEMBER_NEGATION_RE = re.compile(
    r"(?:\b(?:do not|don't|dont|never)\s+(?:remember|save)\b|"
    r"(?:אל|לא)\s+(?:תזכור|תזכרי|זכור|זכרי|שמור|שמרי))",
    re.IGNORECASE,
)


def _has_explicit_remember_intent(owner_text: str) -> bool:
    """Only the current authenticated owner's words can authorize lasting memory."""
    text = owner_text.strip()
    if not text or text.startswith(("\"", "'", "“", "‘", "`")):
        return False
    return bool(_REMEMBER_REQUEST_RE.search(text)) and not bool(_REMEMBER_NEGATION_RE.search(text))


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
