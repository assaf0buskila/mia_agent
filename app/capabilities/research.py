"""Owner research.search — public snippets behind capability/policy."""

from __future__ import annotations

from typing import Any

from app.core.errors import InvalidArguments
from app.integrations.research import (
    MAX_QUERY_LEN,
    MAX_SNIPPETS_IN_ACK,
    ResearchPort,
    sanitize_snippets,
)


def research_search(port: ResearchPort, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise InvalidArguments("query is required")
    snippets = sanitize_snippets(port.search(query[:MAX_QUERY_LEN]))[:MAX_SNIPPETS_IN_ACK]
    return {
        "query": query[:MAX_QUERY_LEN],
        "count": len(snippets),
        "hits": [
            {"title": item.title, "url": item.url, "excerpt": item.excerpt}
            for item in snippets
        ],
    }


def research_handlers(port: ResearchPort) -> dict[str, Any]:
    return {"research.search": lambda args: research_search(port, args)}
