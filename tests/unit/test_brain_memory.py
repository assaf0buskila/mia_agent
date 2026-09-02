"""Memory behaviour: storage, retrieval quality, supersession, and provenance.

These assert what the brain actually *does* — which memory comes back for which question,
what happens when a preference changes, whether a fact survives a rewrite — not merely
that a call returned 200.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from app.brain.context import (
    assemble_owner_context,
    build_profile_block,
    render_context_block,
    retrieve_knowledge,
    retrieve_memories,
)
from app.brain.embeddings import DisabledEmbeddingPort, FakeEmbeddingPort
from app.brain.extraction import parse_extraction, reconcile_candidate
from app.brain.retrieval import (
    MemoryScoreWeights,
    bm25_scores,
    deduplicate,
    min_max_normalize,
    recency_score,
    reciprocal_rank_fusion,
)
from app.brain.schemas import (
    MemoryCandidate,
    MemoryCategory,
    MemoryKind,
    MemoryOperation,
    MemorySource,
    MemoryStatus,
)
from app.brain.store import BrainStore
from app.brain.vectors import (
    VectorError,
    decode_vector,
    encode_vector,
    l2_normalize,
    rank_by_similarity,
)
from app.db.session import get_session_factory, init_db
from app.integrations.llm_client import LlmClient

WEIGHTS = MemoryScoreWeights()


def _brain() -> BrainStore:
    init_db()
    return BrainStore(get_session_factory()())


_SUBJECT_SEQUENCE = itertools.count()


def _subject() -> str:
    """A fresh owner scope per test.

    The suite shares one in-memory SQLite database across every test in the process, so
    anything asserting on "all memories" needs its own subject or it reads other tests'
    fixtures.
    """
    return f"owner_test_{next(_SUBJECT_SEQUENCE)}"


def _seed(
    brain: BrainStore,
    emb: FakeEmbeddingPort,
    facts: list[tuple],
    *,
    subject: str = "owner",
) -> dict[str, str]:
    ids: dict[str, str] = {}
    for text, kind, category, importance in facts:
        ids[text] = brain.save_memory(
            text=text,
            kind=kind,
            category=category,
            importance=importance,
            subject=subject,
            source=MemorySource.TELEGRAM,
            embedding=emb.embed([text])[0],
            embedding_model=emb.model,
        )
    return ids


# --------------------------------------------------------------------- vectors


def test_vector_round_trip_preserves_values() -> None:
    original = l2_normalize([0.5, -0.25, 0.75, 1.0])
    decoded = list(decode_vector(encode_vector(original)))
    assert len(decoded) == len(original)
    for left, right in zip(original, decoded, strict=True):
        assert abs(left - right) < 1e-6


def test_normalized_vectors_make_cosine_a_dot_product() -> None:
    vector = l2_normalize([3.0, 4.0])
    assert abs(sum(value * value for value in vector) - 1.0) < 1e-9


def test_zero_vector_does_not_explode() -> None:
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]


def test_corrupt_vector_row_is_skipped_not_fatal() -> None:
    """One poisoned row must not take down retrieval for everything else."""
    good = encode_vector(l2_normalize([1.0, 0.0]))
    ranked = rank_by_similarity(
        [1.0, 0.0],
        [("good", good), ("corrupt", "!!!not-base64!!!"), ("wrong_dim", encode_vector([1.0]))],
        limit=5,
    )
    assert [item_id for item_id, _score in ranked] == ["good"]


def test_empty_vector_is_refused() -> None:
    try:
        encode_vector([])
    except VectorError:
        return
    raise AssertionError("expected VectorError")


# ------------------------------------------------------------------- retrieval


def test_bm25_ranks_the_lexically_matching_document_first() -> None:
    scores = bm25_scores(
        "supabase stack",
        [
            ("a", "Assaf uses Python FastAPI and Supabase as his stack"),
            ("b", "Cafe Ana is a client website"),
        ],
    )
    assert scores["a"] > scores.get("b", 0.0)


def test_rrf_fuses_two_disagreeing_rankings() -> None:
    fused = reciprocal_rank_fusion([({"a": 0.9, "b": 0.1}, 1.0), ({"b": 50.0, "a": 1.0}, 1.0)])
    assert set(fused) == {"a", "b"}
    # Both rank first once, so RRF should keep them close rather than letting the
    # unbounded BM25 score dominate the bounded cosine one.
    assert abs(fused["a"] - fused["b"]) < 0.01


def test_recency_decays_over_hours_not_creation() -> None:
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    fresh = recency_score(now.isoformat(), now=now)
    day_old = recency_score((now - timedelta(hours=24)).isoformat(), now=now)
    week_old = recency_score((now - timedelta(hours=168)).isoformat(), now=now)
    assert fresh > day_old > week_old
    assert abs(fresh - 1.0) < 1e-9


def test_min_max_normalize_handles_a_single_value() -> None:
    assert min_max_normalize({"a": 5.0}) == {"a": 1.0}


def test_deduplicate_drops_a_restatement() -> None:
    from app.brain.schemas import RetrievedItem

    items = [
        RetrievedItem(
            item_id="1", text="Assaf uses Python FastAPI and Supabase", origin="m", score=1.0
        ),
        RetrievedItem(
            item_id="2", text="Assaf uses Python FastAPI and Supabase", origin="m", score=0.9
        ),
        RetrievedItem(
            item_id="3", text="Cafe Ana is a client website he built", origin="m", score=0.8
        ),
    ]
    kept = deduplicate(items)
    assert [item.item_id for item in kept] == ["1", "3"]


# ------------------------------------------------- Assaf's evaluation scenarios


def test_what_projects_am_i_working_on() -> None:
    brain, emb = _brain(), FakeEmbeddingPort()
    _seed(
        brain,
        emb,
        [
            (
                "Assaf is building Mia, an AI growth and sales operator",
                MemoryKind.WORKING,
                MemoryCategory.PROJECT,
                9,
            ),
            (
                "AssafWeb is Assaf's AI growth studio with payments",
                MemoryKind.WORKING,
                MemoryCategory.PROJECT,
                8,
            ),
            ("Assaf likes strong coffee", MemoryKind.SEMANTIC, MemoryCategory.OTHER, 3),
        ],
    )
    hits = retrieve_memories(
        brain, query="what projects am I building right now", embedding_port=emb, weights=WEIGHTS
    )
    top = " ".join(hit.text for hit in hits[:2])
    assert "Mia" in top or "AssafWeb" in top
    assert "coffee" not in hits[0].text


def test_what_are_my_preferred_technologies() -> None:
    brain, emb = _brain(), FakeEmbeddingPort()
    _seed(
        brain,
        emb,
        [
            (
                "Assaf uses Python FastAPI and Supabase as his main stack",
                MemoryKind.SEMANTIC,
                MemoryCategory.SKILL,
                8,
            ),
            (
                "Cafe Ana is a client website Assaf built",
                MemoryKind.SEMANTIC,
                MemoryCategory.BUSINESS,
                4,
            ),
        ],
    )
    hits = retrieve_memories(
        brain, query="which technologies and stack do I prefer", embedding_port=emb, weights=WEIGHTS
    )
    assert "Supabase" in hits[0].text


def test_what_do_you_know_about_me_returns_the_profile() -> None:
    brain, emb, subject = _brain(), FakeEmbeddingPort(), _subject()
    _seed(
        brain,
        emb,
        [
            (
                "Assaf Buskila is an independent AI Solutions Engineer in Israel",
                MemoryKind.SEMANTIC,
                MemoryCategory.IDENTITY,
                10,
            ),
            (
                "Assaf prefers blunt direct feedback",
                MemoryKind.PREFERENCE,
                MemoryCategory.COMMUNICATION,
                9,
            ),
        ],
        subject=subject,
    )
    profile, ids = build_profile_block(brain, subject=subject)
    assert "AI Solutions Engineer" in profile
    assert "blunt" in profile
    assert len(ids) == 2


def test_profile_facts_are_not_repeated_in_the_retrieved_section() -> None:
    """A fact shown twice wastes budget and makes one source look like two."""
    brain, emb = _brain(), FakeEmbeddingPort()
    _seed(
        brain,
        emb,
        [
            (
                "Assaf Buskila is an AI Solutions Engineer",
                MemoryKind.SEMANTIC,
                MemoryCategory.IDENTITY,
                10,
            ),
            ("Assaf is building Mia", MemoryKind.WORKING, MemoryCategory.PROJECT, 9),
        ],
    )
    context = assemble_owner_context(brain, query="who am I", embedding_port=emb)
    profile_texts = {line.lstrip("- ") for line in context.profile.splitlines()}
    for item in context.memories:
        assert item.text not in profile_texts


def test_a_changed_preference_supersedes_the_old_one() -> None:
    """Assaf changes his mind; the new preference must win and the old must not surface."""
    brain, emb = _brain(), FakeEmbeddingPort()
    old_text = "Assaf wants Mia to send him a daily brief every morning"
    old_id = brain.save_memory(
        text=old_text,
        kind=MemoryKind.PREFERENCE,
        category=MemoryCategory.PREFERENCE,
        importance=7,
        source=MemorySource.TELEGRAM,
        embedding=emb.embed([old_text])[0],
        embedding_model=emb.model,
    )
    new_text = "Assaf wants the brief weekly, not daily"
    new_id = brain.save_memory(
        text=new_text,
        kind=MemoryKind.PREFERENCE,
        category=MemoryCategory.PREFERENCE,
        importance=8,
        source=MemorySource.TELEGRAM,
        embedding=emb.embed([new_text])[0],
        embedding_model=emb.model,
    )
    assert brain.supersede_memory(old_id, replacement_id=new_id) is True

    live = [record.text for record in brain.list_memories()]
    assert new_text in live
    assert old_text not in live
    # Superseded, not deleted: the audit trail survives.
    superseded = brain.get_memory(old_id)
    assert superseded.status is MemoryStatus.SUPERSEDED
    assert superseded.superseded_by == new_id
    hits = retrieve_memories(
        brain, query="how often do I want the brief", embedding_port=emb, weights=WEIGHTS
    )
    assert all(old_text != hit.text for hit in hits)


def test_a_preference_persists_across_later_conversations() -> None:
    """Stored once, retrievable much later, with the recency decay applied not fatal."""
    brain, emb = _brain(), FakeEmbeddingPort()
    text = "Assaf wants Mia to always answer in Hebrew unless he writes English"
    brain.save_memory(
        text=text,
        kind=MemoryKind.PREFERENCE,
        category=MemoryCategory.COMMUNICATION,
        importance=9,
        source=MemorySource.TELEGRAM,
        embedding=emb.embed([text])[0],
        embedding_model=emb.model,
    )
    much_later = datetime.now(UTC) + timedelta(days=45)
    hits = retrieve_memories(
        brain,
        query="which language should you answer in",
        embedding_port=emb,
        weights=WEIGHTS,
        now=much_later,
    )
    assert any("Hebrew" in hit.text for hit in hits)


def test_website_knowledge_and_memory_both_surface_for_one_question() -> None:
    from app.brain.knowledge import FakeDocumentFetcher, ingest_website

    brain, emb = _brain(), FakeEmbeddingPort()
    _seed(
        brain,
        emb,
        [
            (
                "Assaf decided to price the voice agent per project, not per minute",
                MemoryKind.EPISODIC,
                MemoryCategory.DECISION,
                8,
            )
        ],
    )
    body = (
        "# Site\n\n## Hebrew Voice Agent\n"
        "A natural Hebrew voice agent that answers the phone after hours and books "
        "appointments into the calendar.\n"
    )
    ingest_website(
        brain,
        website_url="https://www.assafweb.com",
        sources=["llms-full.txt"],
        fetcher=FakeDocumentFetcher({"https://www.assafweb.com/llms-full.txt": body}),
        embedding_port=emb,
    )
    context = assemble_owner_context(brain, query="voice agent", embedding_port=emb)
    assert context.memories or context.knowledge
    rendered = render_context_block(context)
    assert "voice agent" in rendered.lower()


def test_what_does_my_website_say_about_a_service() -> None:
    from app.brain.knowledge import FakeDocumentFetcher, ingest_website

    brain, emb = _brain(), FakeEmbeddingPort()
    body = (
        "# Site\n\n## Digital Business Card\n"
        "One page digital business card with click to call, WhatsApp and navigation "
        "buttons, shared as a link and QR code.\n\n"
        "## Business Automations\n"
        "Automatic lead follow-up the moment a lead arrives.\n"
    )
    ingest_website(
        brain,
        website_url="https://www.assafweb.com",
        sources=["llms-full.txt"],
        fetcher=FakeDocumentFetcher({"https://www.assafweb.com/llms-full.txt": body}),
        embedding_port=emb,
    )
    hits = retrieve_knowledge(brain, query="digital business card QR code", embedding_port=emb)
    assert hits
    assert "business card" in hits[0].text.lower()


def test_context_block_states_the_boundary_of_what_is_known() -> None:
    """Hallucination prevention: the prompt must say what counts as known."""
    brain, emb = _brain(), FakeEmbeddingPort()
    _seed(brain, emb, [("Assaf lives in Israel", MemoryKind.SEMANTIC, MemoryCategory.IDENTITY, 8)])
    rendered = render_context_block(
        assemble_owner_context(brain, query="where do I live", embedding_port=emb)
    )
    assert "do not know" in rendered
    assert "Never invent" in rendered


def test_empty_brain_retrieves_nothing() -> None:
    """An unpopulated subject must return nothing at all, never a fabricated stand-in."""
    brain, emb = _brain(), FakeEmbeddingPort()
    hits = retrieve_memories(
        brain,
        query="what is my sister's name",
        embedding_port=emb,
        weights=WEIGHTS,
        subject="nobody_with_no_memories",
    )
    assert hits == []


def test_unrelated_question_is_bounded_by_the_do_not_know_instruction() -> None:
    """Retrieval is recall-oriented, so an off-topic query can still surface context.

    Ranking alone cannot decide that a fact is absent — that is the prompt's job. What
    must hold is that the context block never claims coverage it does not have, and that
    nothing outside the stored set appears.
    """
    brain, emb = _brain(), FakeEmbeddingPort()
    stored = {
        text: None
        for text in [
            "Assaf runs an AI automation studio",
            "Assaf built Cafe Ana",
        ]
    }
    _seed(
        brain,
        emb,
        [
            ("Assaf runs an AI automation studio", MemoryKind.SEMANTIC, MemoryCategory.BUSINESS, 7),
            ("Assaf built Cafe Ana", MemoryKind.SEMANTIC, MemoryCategory.BUSINESS, 6),
        ],
    )
    context = assemble_owner_context(
        brain, query="what is my sister's name", embedding_port=emb
    )
    rendered = render_context_block(context)
    assert "do not know" in rendered
    # Whatever surfaced must be a real stored memory, never invented.
    known = set(stored) | {
        record.text for record in brain.list_memories()
    }
    for item in context.memories:
        assert item.text in known


def test_retrieval_degrades_to_keyword_when_embeddings_are_off() -> None:
    """No embedding provider must mean worse ranking, never an empty brain."""
    brain, emb = _brain(), FakeEmbeddingPort()
    _seed(
        brain,
        emb,
        [("Assaf uses Python FastAPI and Supabase", MemoryKind.SEMANTIC, MemoryCategory.SKILL, 8)],
    )
    disabled = DisabledEmbeddingPort()
    hits = retrieve_memories(
        brain, query="supabase python", embedding_port=disabled, weights=WEIGHTS
    )
    assert hits
    assert "Supabase" in hits[0].text
    context = assemble_owner_context(brain, query="supabase", embedding_port=disabled)
    assert context.degraded is True


@pytest.mark.parametrize("budget", [200, 300, 1000, 4000])
def test_retrieval_never_exceeds_the_character_budget(budget: int) -> None:
    """The always-on profile must be clamped too, or a small budget is blown by it alone."""
    brain, emb = _brain(), FakeEmbeddingPort()
    _seed(
        brain,
        emb,
        [
            (
                f"Assaf fact number {index} about his automation business",
                MemoryKind.SEMANTIC,
                MemoryCategory.IDENTITY if index % 2 else MemoryCategory.BUSINESS,
                6,
            )
            for index in range(30)
        ],
    )
    context = assemble_owner_context(
        brain, query="automation business", embedding_port=emb, max_chars=budget
    )
    assert context.used_chars <= budget
    # A small budget must still leave room for something retrieved for this question.
    if budget >= 1000:
        assert context.memories


def test_touch_updates_last_used_so_recency_means_something() -> None:
    brain, emb = _brain(), FakeEmbeddingPort()
    ids = _seed(
        brain,
        emb,
        [("Assaf runs an automation studio", MemoryKind.SEMANTIC, MemoryCategory.BUSINESS, 7)],
    )
    memory_id = next(iter(ids.values()))
    before = brain.get_memory(memory_id)
    assemble_owner_context(brain, query="automation studio", embedding_port=emb, touch=True)
    after = brain.get_memory(memory_id)
    assert after.use_count > before.use_count


# ------------------------------------------------------- extraction behaviour


def test_low_importance_facts_are_never_stored() -> None:
    """The cheapest forgetting mechanism is not writing it down."""
    result = parse_extraction(
        {
            "facts": [
                {
                    "text": "Assaf had a sandwich",
                    "kind": "episodic",
                    "category": "event",
                    "importance": 1,
                    "entities": [],
                },
                {
                    "text": "Assaf founded AssafWeb",
                    "kind": "semantic",
                    "category": "business",
                    "importance": 9,
                    "entities": [],
                },
            ],
            "questions": [],
        }
    )
    assert [candidate.text for candidate in result.candidates] == ["Assaf founded AssafWeb"]
    assert result.skipped == 1


def test_extraction_survives_a_malformed_payload() -> None:
    result = parse_extraction({"facts": ["not an object", {"no_text": 1}], "questions": "nope"})
    assert result.candidates == ()
    assert result.gaps == ()


def test_exact_restatement_is_a_noop_without_calling_the_model() -> None:
    brain, emb = _brain(), FakeEmbeddingPort()
    ids = _seed(
        brain,
        emb,
        [
            (
                "Assaf uses Python FastAPI and Supabase as his stack",
                MemoryKind.SEMANTIC,
                MemoryCategory.SKILL,
                8,
            )
        ],
    )
    existing = brain.get_memory(next(iter(ids.values())))
    operation, target, _text = reconcile_candidate(
        LlmClient(api_key="", model=""),
        candidate=MemoryCandidate(
            text="Assaf uses Python FastAPI and Supabase as his stack", importance=7
        ),
        neighbours=[existing],
    )
    assert operation is MemoryOperation.NOOP
    assert target == existing.memory_id


def test_first_ever_fact_is_added_without_a_model_call() -> None:
    operation, target, text = reconcile_candidate(
        LlmClient(api_key="", model=""),
        candidate=MemoryCandidate(text="Assaf launched AssafWeb", importance=8),
        neighbours=[],
    )
    assert operation is MemoryOperation.ADD
    assert target == ""
    assert text == "Assaf launched AssafWeb"


def test_gaps_are_opened_once_and_resolved() -> None:
    brain = _brain()
    first = brain.open_gap(
        topic="pricing", question="What is your typical project range?", priority=7
    )
    second = brain.open_gap(topic="Pricing", question="asked differently", priority=7)
    assert first == second
    assert [gap.topic for gap in brain.list_open_gaps()] == ["pricing"]
    assert brain.mark_gap_asked(first) is True
    assert brain.list_open_gaps() == []
    assert brain.resolve_gap(first) is True


def test_entities_are_linked_and_counted() -> None:
    brain, emb = _brain(), FakeEmbeddingPort()
    brain.save_memory(
        text="AssafWeb handles payments",
        kind=MemoryKind.SEMANTIC,
        category=MemoryCategory.PROJECT,
        importance=7,
        source=MemorySource.TELEGRAM,
        entities=("AssafWeb",),
        embedding=emb.embed(["AssafWeb handles payments"])[0],
        embedding_model=emb.model,
    )
    brain.save_memory(
        text="AssafWeb has user accounts",
        kind=MemoryKind.SEMANTIC,
        category=MemoryCategory.PROJECT,
        importance=7,
        source=MemorySource.TELEGRAM,
        entities=("assafweb",),
        embedding=emb.embed(["AssafWeb has user accounts"])[0],
        embedding_model=emb.model,
    )
    entities = {entity.name.lower(): entity for entity in brain.list_entities()}
    assert "assafweb" in entities
    # Case-insensitive key: 'AssafWeb' and 'assafweb' are one entity, not two.
    assert entities["assafweb"].mention_count == 2
    assert len(brain.memory_ids_for_entity("AssafWeb")) == 2
