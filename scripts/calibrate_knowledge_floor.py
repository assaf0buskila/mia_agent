"""Measure a knowledge evidence floor instead of guessing one.

There is no `MIA_KNOWLEDGE_MIN_SIMILARITY` setting. The only knob is `min_similarity`,
an ordinary keyword argument on `retrieve_knowledge` (`app/brain/retrieval.py`) and
`get_context`/friends (`app/brain/context.py`), and every live caller today leaves it
at its default of `0.0` -- no evidence floor is applied before Mia quotes a published
chunk. Picking a non-zero value by feel is how a gate ends up either useless or
silently muting real answers, so this script derives a candidate from data instead:
embed a set of questions the published corpus genuinely answers and a set it plainly
does not, look at the top raw cosine each one gets, and take the floor from the gap
between the two groups. It only ever suggests a number; wiring it into a call site is
a separate, deliberate change.

Read-only. Touches the knowledge corpus and the embedding provider, nothing else.

    uv run python scripts/calibrate_knowledge_floor.py

Exit codes:
    0  clean separation; a candidate `min_similarity` value is printed
    1  the groups overlap -- no honest floor exists for this corpus and these queries
    2  the corpus or the embedding provider is not available
"""

from __future__ import annotations

from app.brain.context import retrieve_knowledge
from app.brain.embeddings import build_embedding_port
from app.brain.store import BrainStore
from app.core.config import get_settings
from app.db.session import get_session_factory

# Questions assafweb.com's published material genuinely answers.
POSITIVE = (
    "כמה עולה לבנות אתר?",
    "מה זה AssafWeb?",
    "אתם בונים אוטומציות בוואטסאפ?",
    "יש לכם סוכן קולי?",
    "מה השירותים שאתם נותנים?",
    "how much does a website cost?",
    "do you build AI agents?",
)

# Questions it plainly does not. None of these should ever quote a published fact.
NEGATIVE = (
    "מה מזג האוויר בתל אביב מחר?",
    "מתכון לעוגת שוקולד",
    "מי ניצח במונדיאל 2022?",
    "איך מטפלים בכאב גב תחתון?",
    "כמה עולה טיסה לתאילנד?",
    "what is the capital of Peru?",
    "recommend a good pasta recipe",
)


def _top_similarity(store: BrainStore, port, query: str) -> float:
    """Best raw cosine any published chunk gets for this query, ungated."""
    hits = retrieve_knowledge(
        store, query=query, embedding_port=port, limit=3, min_similarity=0.0
    )
    return max((hit.similarity for hit in hits), default=0.0)


def main() -> int:
    settings = get_settings()
    db = get_session_factory()()
    try:
        store = BrainStore(db)
        chunks = store.list_knowledge_chunks()
        if not chunks:
            print("no knowledge chunks ingested; run mia-ingest-knowledge first")
            return 2
        try:
            port = build_embedding_port(settings)
        except Exception as exc:  # noqa: BLE001 - calibration must explain itself
            print(f"embedding provider unavailable: {type(exc).__name__}: {exc}")
            return 2

        print(f"corpus: {len(chunks)} chunks")
        print(f"embedding: {settings.embedding_provider} {settings.embedding_model}")
        print()

        scored: dict[str, list[tuple[str, float]]] = {"positive": [], "negative": []}
        for label, queries in (("positive", POSITIVE), ("negative", NEGATIVE)):
            for query in queries:
                top = _top_similarity(store, port, query)
                scored[label].append((query, top))
                print(f"{label:8}  {top:.4f}  {query}")
        print()

        weakest_positive = min(score for _q, score in scored["positive"])
        strongest_negative = max(score for _q, score in scored["negative"])
        print(f"weakest positive : {weakest_positive:.4f}")
        print(f"strongest negative: {strongest_negative:.4f}")

        if weakest_positive <= strongest_negative:
            print()
            print("OVERLAP: no floor separates these groups.")
            print("A threshold here would either mute a real answer or admit an")
            print("unrelated one. Leave min_similarity at 0.0 at every call site and")
            print("fix retrieval or the corpus instead of inventing a number.")
            return 1

        # Sit in the gap, biased toward the negatives so a real question is never
        # muted by a borderline embedding. Never answering is worse than one weak hit.
        floor = strongest_negative + (weakest_positive - strongest_negative) * 0.35
        print()
        print(f"gap: {weakest_positive - strongest_negative:.4f}")
        print(f"suggested min_similarity = {floor:.3f}")
        print("(pass this to retrieve_knowledge/get_context at a call site; there is")
        print("no setting to update -- none exists)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
