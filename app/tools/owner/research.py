"""Owner research tool: bounded web research reads."""

from __future__ import annotations

from typing import Any

from app.capabilities.policy import execute_capability
from app.capabilities.research import research_handlers
from app.core.errors import PermissionDenied
from app.integrations.research import ResearchSnippet, format_sources_block
from app.tools.owner.types import _NOT_CONNECTED, ToolContext, ToolResult, _empty


def _research_search(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, error="query is required")
    if ctx.research is None:
        return ToolResult(ok=True, text=_NOT_CONNECTED)
    try:
        out = execute_capability(
            "research.search",
            principal=ctx.principal,
            args={"query": query},
            handlers=research_handlers(ctx.research),
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        return ToolResult(ok=True, text=_NOT_CONNECTED)
    hits = out.get("hits") or []
    snippets = [
        ResearchSnippet(
            title=str(item.get("title") or ""),
            url=str(item.get("url") or ""),
            excerpt=str(item.get("excerpt") or ""),
        )
        for item in hits
        if isinstance(item, dict)
    ]
    text = format_sources_block(snippets)
    return _empty(text, "Research search returned nothing. Check the Firecrawl key.")
