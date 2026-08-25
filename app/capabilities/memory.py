"""Owner memory.search — brain retrieval behind capability/policy."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.brain.context import retrieve_memories
from app.brain.embeddings import EmbeddingPort
from app.brain.retrieval import MemoryScoreWeights
from app.brain.store import BrainStore
from app.core.errors import InvalidArguments


def memory_search(
    *,
    brain: BrainStore,
    embedding_port: EmbeddingPort,
    weights: MemoryScoreWeights,
    args: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise InvalidArguments("query is required")
    hits = retrieve_memories(
        brain,
        query=query,
        embedding_port=embedding_port,
        weights=weights,
        limit=8,
        now=now,
    )
    return {
        "hits": [
            {
                "id": hit.item_id,
                "label": hit.label or "memory",
                "text": hit.text[:500],
            }
            for hit in hits
        ]
    }


def memory_handlers(
    *,
    brain: BrainStore,
    embedding_port: EmbeddingPort,
    weights: MemoryScoreWeights,
    now: datetime | None = None,
) -> dict[str, Any]:
    return {
        "memory.search": lambda args: memory_search(
            brain=brain,
            embedding_port=embedding_port,
            weights=weights,
            args=args,
            now=now,
        )
    }
