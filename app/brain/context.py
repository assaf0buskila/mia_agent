"""The unified knowledge layer.

One entry point — `assemble_owner_context` — that combines the always-on owner profile,
long-term memory, ingested website/business knowledge and open questions into a bounded,
provenance-tagged block for the model.

Retrieval only. This module never writes memory and never calls a tool. It degrades in
three steps so the owner console never goes dark: embeddings present → hybrid semantic +
keyword; embeddings absent → keyword only; brain disabled or empty → an empty context that
callers treat as "no extra knowledge", not as an error.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.brain.embeddings import EmbeddingError, EmbeddingPort
from app.brain.retrieval import (
    MemoryScoreWeights,
    deduplicate,
    fit_to_budget,
    rank_knowledge,
    rank_memories,
)
from app.brain.schemas import (
    BrainContext,
    MemoryCategory,
    MemoryKind,
    RetrievedItem,
)
from app.brain.store import BrainStore
from app.brain.vectors import rank_by_similarity

# Categories that make up the always-on profile. Small, curated, never retrieval-ranked:
# these are the facts that should be in front of the model on every single turn.
PROFILE_CATEGORIES: tuple[MemoryCategory, ...] = (
    MemoryCategory.IDENTITY,
    MemoryCategory.BACKGROUND,
    MemoryCategory.COMMUNICATION,
    MemoryCategory.PREFERENCE,
)
MAX_PROFILE_FACTS = 12
MAX_PROFILE_CHARS = 1200
DEFAULT_MEMORY_LIMIT = 8
DEFAULT_KNOWLEDGE_LIMIT = 5
# Fraction of the character budget reserved for ingested knowledge; the rest goes to
# memory. Memory wins ties because it is owner-specific and knowledge is public.
KNOWLEDGE_BUDGET_SHARE = 0.4
# Ceiling on the always-on profile as a fraction of the total budget, so retrieval for the
# current question always has room even when `max_chars` is small.
PROFILE_BUDGET_SHARE = 0.4
_CANDIDATE_POOL = 50


def _embed_query(port: EmbeddingPort, query: str) -> list[float]:
    """Embed the query, or return an empty vector so retrieval degrades to keyword-only."""
    if not port.enabled() or not query.strip():
        return []
    try:
        vectors = port.embed([query])
    except EmbeddingError:
        return []
    return vectors[0] if vectors else []


def _similarity_map(
    query_vector: list[float], rows: list[tuple[str, str]], *, limit: int
) -> dict[str, float]:
    if not query_vector or not rows:
        return {}
    return dict(rank_by_similarity(query_vector, rows, limit=limit))


def build_profile_block(
    store: BrainStore, *, subject: str = "owner", max_chars: int = MAX_PROFILE_CHARS
) -> tuple[str, set[str]]:
    """The stable core: who Assaf is, how he works, what he prefers.

    Ordered by importance so a truncated profile keeps the most significant facts. Returns
    the rendered block plus the ids it consumed, so retrieval can skip them — repeating a
    profile fact in the retrieved section wastes budget and inflates the model's confidence
    by making one fact look like two sources.
    """
    records = store.list_memories(
        subject=subject, categories=PROFILE_CATEGORIES, limit=MAX_PROFILE_FACTS * 3
    )
    if not records:
        return "", set()
    budget = max(0, min(max_chars, MAX_PROFILE_CHARS))
    lines: list[str] = []
    used_ids: set[str] = set()
    used = 0
    for record in records:
        line = f"- {record.text.strip()}"
        if used + len(line) + 1 > budget or len(lines) >= MAX_PROFILE_FACTS:
            break
        lines.append(line)
        used_ids.add(record.memory_id)
        used += len(line) + 1
    return "\n".join(lines), used_ids


def retrieve_memories(
    store: BrainStore,
    *,
    query: str,
    embedding_port: EmbeddingPort,
    weights: MemoryScoreWeights,
    limit: int = DEFAULT_MEMORY_LIMIT,
    kinds: tuple[MemoryKind, ...] = (),
    subject: str = "owner",
    exclude_ids: set[str] | None = None,
    now: datetime | None = None,
) -> list[RetrievedItem]:
    skip = exclude_ids or set()
    records = [
        record
        for record in store.list_memories(subject=subject, kinds=kinds)
        if record.memory_id not in skip
    ]
    if not records:
        return []
    query_vector = _embed_query(embedding_port, query)
    similarity = {
        memory_id: score
        for memory_id, score in _similarity_map(
            query_vector,
            store.memory_vectors(subject=subject, kinds=kinds),
            limit=_CANDIDATE_POOL,
        ).items()
        if memory_id not in skip
    }
    candidates = [
        {
            "id": record.memory_id,
            "text": record.text,
            "importance": record.importance,
            "last_used_at": record.last_used_at or record.created_at,
            "origin": "memory",
            "label": record.kind.value,
            "source_ref": record.source_ref or record.source.value,
        }
        for record in records
    ]
    ranked = rank_memories(
        query=query,
        candidates=candidates,
        similarity=similarity,
        weights=weights,
        limit=limit * 2,
        now=now,
    )
    return deduplicate(ranked)[:limit]


def retrieve_knowledge(
    store: BrainStore,
    *,
    query: str,
    embedding_port: EmbeddingPort,
    limit: int = DEFAULT_KNOWLEDGE_LIMIT,
) -> list[RetrievedItem]:
    chunks = store.list_knowledge_chunks()
    if not chunks:
        return []
    query_vector = _embed_query(embedding_port, query)
    similarity = _similarity_map(
        query_vector, store.knowledge_vectors(), limit=_CANDIDATE_POOL
    )
    candidates = [
        {
            "id": chunk.chunk_id,
            "text": chunk.text,
            "origin": "knowledge",
            "label": chunk.title or chunk.category.value,
            "source_ref": chunk.url or chunk.source_id,
            "category": chunk.category.value,
        }
        for chunk in chunks
    ]
    ranked = rank_knowledge(
        query=query, candidates=candidates, similarity=similarity, limit=limit * 2
    )
    return deduplicate(ranked)[:limit]


def assemble_owner_context(
    store: BrainStore,
    *,
    query: str,
    embedding_port: EmbeddingPort,
    max_chars: int = 4000,
    weights: MemoryScoreWeights | None = None,
    memory_limit: int = DEFAULT_MEMORY_LIMIT,
    knowledge_limit: int = DEFAULT_KNOWLEDGE_LIMIT,
    include_gaps: bool = True,
    touch: bool = True,
    now: datetime | None = None,
) -> BrainContext:
    """Assemble the most relevant context for one owner request, within a char budget.

    `touch=True` bumps last-used on every memory actually included, which is what makes the
    recency component meaningful — the decay is defined over last access, not creation.
    """
    score_weights = weights or MemoryScoreWeights()
    # The profile is always-on, but it must never consume the whole budget or a small
    # `max_chars` would leave no room for anything retrieved for this actual question.
    profile, profile_ids = build_profile_block(
        store, max_chars=int(max_chars * PROFILE_BUDGET_SHARE)
    )
    profile_cost = len(profile)
    remaining = max(0, max_chars - profile_cost)
    knowledge_budget = int(remaining * KNOWLEDGE_BUDGET_SHARE)
    memory_budget = remaining - knowledge_budget

    memories = retrieve_memories(
        store,
        query=query,
        embedding_port=embedding_port,
        weights=score_weights,
        limit=memory_limit,
        exclude_ids=profile_ids,
        now=now,
    )
    knowledge = retrieve_knowledge(
        store, query=query, embedding_port=embedding_port, limit=knowledge_limit
    )
    memories, memory_used = fit_to_budget(memories, max_chars=memory_budget)
    knowledge, knowledge_used = fit_to_budget(knowledge, max_chars=knowledge_budget)

    if touch and memories:
        store.touch_memories([item.item_id for item in memories])

    open_questions: tuple[str, ...] = ()
    if include_gaps:
        open_questions = tuple(gap.question for gap in store.list_open_gaps(limit=3))

    return BrainContext(
        profile=profile,
        memories=tuple(memories),
        knowledge=tuple(knowledge),
        open_questions=open_questions,
        used_chars=profile_cost + memory_used + knowledge_used,
        degraded=not embedding_port.enabled(),
    )


def assemble_visitor_context(
    store: BrainStore,
    *,
    query: str,
    embedding_port: EmbeddingPort,
    max_chars: int = 1200,
    knowledge_limit: int = 3,
) -> BrainContext:
    """Knowledge-only context for a website visitor.

    HARD SAFETY INVARIANT: this retrieves published website/business knowledge ONLY. It
    must never call `retrieve_memories`, `build_profile_block`, `store.list_memories`,
    `store.memory_vectors`, `store.touch_memories`, or `store.list_open_gaps`. A website
    visitor must never be able to read owner memory (`docs/PRD.md`: "a website visitor
    can never write it" — the read side of that same boundary is enforced here). The
    returned context always has an empty `profile`, empty `memories`, and empty
    `open_questions`; only `knowledge` is ever populated.
    """
    knowledge = retrieve_knowledge(
        store, query=query, embedding_port=embedding_port, limit=knowledge_limit
    )
    knowledge, knowledge_used = fit_to_budget(knowledge, max_chars=max(0, max_chars))
    return BrainContext(
        profile="",
        memories=(),
        knowledge=tuple(knowledge),
        open_questions=(),
        used_chars=knowledge_used,
        degraded=not embedding_port.enabled(),
    )


def render_visitor_knowledge_block(context: BrainContext) -> tuple[str, ...]:
    """One rendered, provenance-tagged line per knowledge item. `()` when empty.

    Same line style as the knowledge section of `render_context_block`, so the two
    surfaces (owner Telegram, website visitor) read the same published facts identically.
    """
    return tuple(
        f"- [{item.label or 'site'}] {item.text}" for item in context.knowledge
    )


def render_context_block(context: BrainContext) -> str:
    """Render the context for a prompt, with provenance on every retrieved line.

    Provenance is the anti-hallucination lever: the model is told exactly which lines it
    is allowed to treat as known, and everything else is explicitly not known.
    """
    if context.is_empty():
        return ""
    sections: list[str] = []
    if context.profile:
        sections.append(f"WHO ASSAF IS (stable profile):\n{context.profile}")
    if context.memories:
        lines = [
            f"- [{item.label or 'memory'}] {item.text}" for item in context.memories
        ]
        sections.append("REMEMBERED FROM PAST CONVERSATIONS:\n" + "\n".join(lines))
    if context.knowledge:
        lines = [
            f"- [{item.label or 'site'}] {item.text}" for item in context.knowledge
        ]
        sections.append("FROM ASSAFWEB KNOWLEDGE BASE:\n" + "\n".join(lines))
    if context.open_questions:
        lines = [f"- {question}" for question in context.open_questions]
        sections.append(
            "THINGS YOU STILL DO NOT KNOW (ask at most one, only if it helps now):\n"
            + "\n".join(lines)
        )
    sections.append(
        "Everything above is what you know. If a fact is not there, say you do not know "
        "it yet. Never invent a personal detail, a client, a number or a date."
    )
    return "\n\n".join(sections)


def utc_now() -> datetime:
    return datetime.now(UTC)
