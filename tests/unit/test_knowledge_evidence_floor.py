"""Weak retrieval must say "not published", not hand over the least-bad chunk.

`min_max_normalize` rescales the candidate set so the best hit is always 1.0, however
weak it is. With 33 chunks in the corpus that means an unrelated question still gets a
top-scoring hit, which the website then renders under "the ONLY facts you may state".

The floor is therefore applied to the RAW cosine, which is the only absolute signal in
the pipeline. The number itself is not chosen here — `scripts/calibrate_knowledge_floor.py`
measures it against real positive and negative queries.
"""

from __future__ import annotations

from app.brain.retrieval import rank_knowledge

CANDIDATES: list[dict[str, object]] = [
    {
        "id": "chunk_price",
        "text": "בניית אתר תדמית מתחילה ב-4,000 שקלים.",
        "origin": "knowledge",
        "label": "pricing",
        "source_ref": "https://www.assafweb.com/pricing",
        "category": "pricing",
    },
    {
        "id": "chunk_about",
        "text": "AssafWeb בונה אתרים ואוטומציות לעסקים קטנים.",
        "origin": "knowledge",
        "label": "about",
        "source_ref": "https://www.assafweb.com/",
        "category": "general",
    },
]


def test_the_normalized_score_cannot_express_weak_evidence() -> None:
    """The reason the floor exists at all: top hit is 1.0 even when nothing matches."""
    hits = rank_knowledge(
        query="מי ניצח במונדיאל 2022?",
        candidates=CANDIDATES,
        similarity={"chunk_price": 0.04, "chunk_about": 0.02},
        limit=5,
    )
    assert hits
    assert hits[0].score == 1.0  # a barely-related chunk still scores perfectly
    assert hits[0].similarity == 0.04  # the raw signal tells the truth


def test_a_floor_drops_evidence_that_does_not_answer_the_question() -> None:
    hits = rank_knowledge(
        query="מי ניצח במונדיאל 2022?",
        candidates=CANDIDATES,
        similarity={"chunk_price": 0.04, "chunk_about": 0.02},
        limit=5,
        min_similarity=0.30,
    )
    assert hits == []


def test_a_floor_keeps_evidence_that_does_answer_it() -> None:
    hits = rank_knowledge(
        query="כמה עולה אתר?",
        candidates=CANDIDATES,
        similarity={"chunk_price": 0.71, "chunk_about": 0.33},
        limit=5,
        min_similarity=0.30,
    )
    assert [hit.item_id for hit in hits] == ["chunk_price", "chunk_about"]


def test_a_floor_can_drop_the_weak_half_of_a_mixed_result() -> None:
    hits = rank_knowledge(
        query="כמה עולה אתר?",
        candidates=CANDIDATES,
        similarity={"chunk_price": 0.71, "chunk_about": 0.11},
        limit=5,
        min_similarity=0.30,
    )
    assert [hit.item_id for hit in hits] == ["chunk_price"]


def test_the_default_floor_changes_nothing() -> None:
    """Historical behaviour stays until the calibration script sets a real number."""
    ungated = rank_knowledge(
        query="anything",
        candidates=CANDIDATES,
        similarity={"chunk_price": 0.04, "chunk_about": 0.02},
        limit=5,
    )
    explicit_zero = rank_knowledge(
        query="anything",
        candidates=CANDIDATES,
        similarity={"chunk_price": 0.04, "chunk_about": 0.02},
        limit=5,
        min_similarity=0.0,
    )
    assert [hit.item_id for hit in ungated] == [hit.item_id for hit in explicit_zero]
    assert len(ungated) == 2
