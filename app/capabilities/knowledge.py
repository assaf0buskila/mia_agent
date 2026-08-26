"""knowledge.search — AssafWeb corpus behind capability/policy. Owner and client."""

from __future__ import annotations

from typing import Any

from app.brain.context import retrieve_knowledge
from app.brain.embeddings import EmbeddingPort
from app.brain.store import BrainStore
from app.core.errors import InvalidArguments


def knowledge_search(
    *,
    brain: BrainStore,
    embedding_port: EmbeddingPort,
    args: dict[str, Any],
) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise InvalidArguments("query is required")
    hits = retrieve_knowledge(
        brain, query=query, embedding_port=embedding_port, limit=5
    )
    return {
        "hits": [
            {
                "id": hit.item_id,
                "label": hit.label or "site",
                "text": hit.text[:500],
            }
            for hit in hits
        ]
    }


def knowledge_handlers(
    *,
    brain: BrainStore,
    embedding_port: EmbeddingPort,
) -> dict[str, Any]:
    return {
        "knowledge.search": lambda args: knowledge_search(
            brain=brain,
            embedding_port=embedding_port,
            args=args,
        )
    }
