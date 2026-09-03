"""Visitor site tools. Seller. Few tools. No owner Composio."""

from __future__ import annotations

from typing import Any

from app.capabilities.knowledge import knowledge_handlers
from app.capabilities.policy import execute_capability
from app.core.errors import PermissionDenied
from app.domain.two_state import VISITOR_TOOLS, MiaState, may_run
from app.surfaces.published_facts import lookup_published_fact
from app.tools.registries.owner_tools import (
    MAX_TOOL_RESULT_CHARS,
    ToolContext,
    ToolResult,
    ToolSpec,
    _string_arg,
)

_VISITOR_REGISTRY: dict[str, ToolSpec] = {}


def _register(spec: ToolSpec) -> None:
    if spec.name not in VISITOR_TOOLS:
        raise ValueError(f"visitor tool not in VISITOR_TOOLS: {spec.name}")
    _VISITOR_REGISTRY[spec.name] = spec


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
        return ToolResult(ok=True, text=lookup_published_fact(query))
    lines = [f"- [{hit.get('label') or 'site'}] {hit.get('text') or ''}" for hit in hits]
    return ToolResult(ok=True, text="\n".join(lines), max_chars=MAX_TOOL_RESULT_CHARS)


def _published_facts(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del ctx
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, error="query is required")
    return ToolResult(ok=True, text=lookup_published_fact(query))


_register(
    ToolSpec(
        name="search_knowledge",
        description=(
            "Published AssafWeb facts for a visitor: services and process. "
            "Never invent a price or a metric. Missing is allowed."
        ),
        parameters=_string_arg("query", "What the visitor asked, in their words."),
        handler=_search_knowledge,
    )
)
_register(
    ToolSpec(
        name="published_facts",
        description=(
            "Allowlisted published facts when the knowledge index has no hit. "
            "No prices. No invented numbers."
        ),
        parameters=_string_arg("query", "Visitor question."),
        handler=_published_facts,
    )
)


def visitor_tool_names() -> tuple[str, ...]:
    return tuple(name for name in VISITOR_TOOLS if name in _VISITOR_REGISTRY)


def execute_visitor_tool(name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if not may_run(state=MiaState.VISITOR, tool=name):
        return ToolResult(ok=False, error="visitor cannot run that tool")
    spec = _VISITOR_REGISTRY.get(name)
    if spec is None:
        return ToolResult(ok=False, error=f"unknown visitor tool: {name}")
    try:
        return spec.handler(ctx, arguments or {})
    except Exception as exc:  # noqa: BLE001 - one bad tool must not kill the turn
        return ToolResult(ok=False, error=f"{type(exc).__name__}")
