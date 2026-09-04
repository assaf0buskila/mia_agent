"""Retrieval and ranking.

Two stages, both portable across SQLite and PostgreSQL because both run in Python:

1. **Candidate generation.** An exact-cosine list over stored vectors and a BM25 keyword
   list over the same rows, fused with Reciprocal Rank Fusion at the documented constant
   `k = 60`. Keyword ranking is computed in-process rather than with Postgres `ts_rank`
   or SQLite `bm25()` on purpose — that is the only way one code path serves both engines,
   and over a few thousand short documents it costs microseconds.

2. **Memory re-rank.** The Generative Agents score — relevance + recency + importance,
   each min-max normalized across the candidate set. Applied to memories only; recency
   and poignancy are meaningless for a scraped web page, so knowledge chunks stop at
   stage 1.

Sources for every constant are in docs/BRAIN_ARCHITECTURE.md.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import UTC, datetime

from app.brain.schemas import RetrievedItem

# Azure AI Search documents RRF performing best at a small k, "such as 60".
RRF_K = 60
# Generative Agents: exponential decay over hours since last access, factor 0.995.
RECENCY_DECAY = 0.995
# BM25 defaults.
BM25_K1 = 1.5
BM25_B = 0.75

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Hebrew and English function words carry no retrieval signal and skew BM25 on short docs.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was",
        "were", "be", "with", "that", "this", "it", "as", "at", "by", "from", "what",
        "which", "who", "how", "my", "me", "i", "you", "your",
        "של", "את", "עם", "על", "אני", "אתה", "הוא", "היא", "זה", "זאת", "מה", "מי",
        "איך", "כמה", "יש", "אין", "לי", "לך", "לו", "הם", "הן", "כל", "גם", "אבל",
    }
)


def tokenize(text: str) -> list[str]:
    """Unicode-aware word tokens. Hebrew and English both fall out of `[^\\W_]+`."""
    return [
        token
        for token in (match.group(0).lower() for match in _TOKEN_RE.finditer(text))
        if token and token not in _STOPWORDS
    ]


def hours_since(timestamp: str, *, now: datetime | None = None) -> float:
    """Hours between an ISO-8601 stamp and now. Unparseable or future stamps give 0."""
    if not timestamp:
        return 0.0
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    delta = (reference - parsed).total_seconds() / 3600.0
    return max(0.0, delta)


def recency_score(timestamp: str, *, now: datetime | None = None) -> float:
    """Documented decay: 0.995 ** hours_since(last_accessed)."""
    return RECENCY_DECAY ** hours_since(timestamp, now=now)


def bm25_scores(query: str, documents: list[tuple[str, str]]) -> dict[str, float]:
    """BM25 over `(doc_id, text)` pairs. Empty query or corpus scores nothing."""
    query_tokens = tokenize(query)
    if not query_tokens or not documents:
        return {}
    tokenized: dict[str, list[str]] = {doc_id: tokenize(text) for doc_id, text in documents}
    lengths = {doc_id: len(tokens) for doc_id, tokens in tokenized.items()}
    total_length = sum(lengths.values())
    if not total_length:
        return {}
    avg_length = total_length / len(tokenized)
    doc_frequency: Counter[str] = Counter()
    for tokens in tokenized.values():
        for token in set(tokens):
            doc_frequency[token] += 1
    corpus_size = len(tokenized)
    scores: dict[str, float] = {}
    for doc_id, tokens in tokenized.items():
        if not tokens:
            continue
        counts = Counter(tokens)
        length = lengths[doc_id]
        score = 0.0
        for token in query_tokens:
            frequency = counts.get(token, 0)
            if not frequency:
                continue
            n_docs = doc_frequency[token]
            idf = math.log(1.0 + (corpus_size - n_docs + 0.5) / (n_docs + 0.5))
            denominator = frequency + BM25_K1 * (
                1.0 - BM25_B + BM25_B * length / avg_length
            )
            score += idf * (frequency * (BM25_K1 + 1.0)) / denominator
        if score > 0.0:
            scores[doc_id] = score
    return scores


def _ranks(scores: dict[str, float]) -> dict[str, int]:
    """1-based rank per id, highest score first. Ties break on id for determinism."""
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return {doc_id: index + 1 for index, (doc_id, _score) in enumerate(ordered)}


def reciprocal_rank_fusion(
    ranked_lists: list[tuple[dict[str, float], float]],
    *,
    k: int = RRF_K,
) -> dict[str, float]:
    """Fuse `(scores, weight)` lists as sum of `weight / (k + rank)`.

    RRF exists because the input scorers have incompatible ranges — cosine sits in
    [-1, 1] while BM25 is unbounded — so fusing on rank rather than score is the only
    sound combination.
    """
    fused: dict[str, float] = {}
    for scores, weight in ranked_lists:
        if not scores or weight <= 0.0:
            continue
        for doc_id, rank in _ranks(scores).items():
            fused[doc_id] = fused.get(doc_id, 0.0) + weight / (k + rank)
    return fused


def min_max_normalize(scores: dict[str, float]) -> dict[str, float]:
    """Scale to [0,1] across the candidate set, as the Generative Agents score requires."""
    if not scores:
        return {}
    values = list(scores.values())
    lowest = min(values)
    highest = max(values)
    if math.isclose(highest, lowest):
        return {doc_id: 1.0 for doc_id in scores}
    span = highest - lowest
    return {doc_id: (value - lowest) / span for doc_id, value in scores.items()}


class MemoryScoreWeights:
    """Retrieval weights. Config-tunable so evals can move them without a code change."""

    def __init__(
        self, *, relevance: float = 1.0, recency: float = 0.5, importance: float = 0.3
    ) -> None:
        self.relevance = max(0.0, relevance)
        self.recency = max(0.0, recency)
        self.importance = max(0.0, importance)


def rank_memories(
    *,
    query: str,
    candidates: list[dict[str, object]],
    similarity: dict[str, float],
    weights: MemoryScoreWeights,
    limit: int,
    now: datetime | None = None,
) -> list[RetrievedItem]:
    """Stage 1 + stage 2 over memory candidates.

    `candidates` carry `id`, `text`, `importance`, `last_used_at` and optional `label` /
    `source_ref`. `similarity` is the exact-cosine map; it may be empty, in which case
    retrieval degrades to keyword-only rather than returning nothing.
    """
    if not candidates or limit <= 0:
        return []
    documents = [(str(item["id"]), str(item.get("text", ""))) for item in candidates]
    keyword = bm25_scores(query, documents)
    fused = reciprocal_rank_fusion([(similarity, 1.0), (keyword, 0.5)])
    if not fused:
        # No vector and no keyword overlap: fall back to importance so a bare "what do you
        # know about me?" still returns the most significant memories instead of nothing.
        fused = {
            str(item["id"]): float(item.get("importance", 1)) / 10.0 for item in candidates
        }
    relevance = min_max_normalize(fused)
    by_id = {str(item["id"]): item for item in candidates}
    scored: list[RetrievedItem] = []
    for doc_id, relevance_score in relevance.items():
        item = by_id.get(doc_id)
        if item is None:
            continue
        recency = recency_score(str(item.get("last_used_at", "")), now=now)
        importance = float(item.get("importance", 1)) / 10.0
        total = (
            weights.relevance * relevance_score
            + weights.recency * recency
            + weights.importance * importance
        )
        scored.append(
            RetrievedItem(
                item_id=doc_id,
                text=str(item.get("text", "")),
                origin=str(item.get("origin", "memory")),
                label=str(item.get("label", "")),
                score=total,
                similarity=similarity.get(doc_id, 0.0),
                recency=recency,
                importance=importance,
                source_ref=str(item.get("source_ref", "")),
                category=str(item.get("category", "")),
            )
        )
    scored.sort(key=lambda hit: (-hit.score, hit.item_id))
    return scored[:limit]


def rank_knowledge(
    *,
    query: str,
    candidates: list[dict[str, object]],
    similarity: dict[str, float],
    limit: int,
    min_similarity: float = 0.0,
) -> list[RetrievedItem]:
    """Stage 1 only. Recency and poignancy do not apply to ingested documents.

    `min_similarity` is an evidence floor applied to the RAW cosine, not to `score`.
    It has to be: `min_max_normalize` rescales the candidate set so the best hit is
    always 1.0, however weak it actually is, so the fused score can rank hits but can
    never answer "is any of this about the question". With a small corpus something is
    otherwise always returned and then rendered to the model as fact. A floor of 0.0
    keeps every hit, which is the historical behaviour.
    """
    if not candidates or limit <= 0:
        return []
    documents = [(str(item["id"]), str(item.get("text", ""))) for item in candidates]
    keyword = bm25_scores(query, documents)
    fused = reciprocal_rank_fusion([(similarity, 1.0), (keyword, 0.5)])
    if not fused:
        return []
    by_id = {str(item["id"]): item for item in candidates}
    normalized = min_max_normalize(fused)
    hits: list[RetrievedItem] = []
    for doc_id, score in normalized.items():
        item = by_id.get(doc_id)
        if item is None:
            continue
        raw = similarity.get(doc_id, 0.0)
        if raw < min_similarity:
            continue
        hits.append(
            RetrievedItem(
                item_id=doc_id,
                text=str(item.get("text", "")),
                origin=str(item.get("origin", "knowledge")),
                label=str(item.get("label", "")),
                score=score,
                similarity=raw,
                source_ref=str(item.get("source_ref", "")),
                category=str(item.get("category", "")),
            )
        )
    hits.sort(key=lambda hit: (-hit.score, hit.item_id))
    return hits[:limit]


def deduplicate(items: list[RetrievedItem], *, threshold: float = 0.82) -> list[RetrievedItem]:
    """Drop near-duplicate hits by token overlap, keeping the higher-scoring one.

    Two memories that say the same thing waste context budget and make the model more
    confident than the evidence warrants.
    """
    kept: list[RetrievedItem] = []
    seen: list[set[str]] = []
    for item in items:
        tokens = set(tokenize(item.text))
        if not tokens:
            continue
        duplicate = False
        for previous in seen:
            smaller = min(len(tokens), len(previous))
            if smaller < 3:
                continue
            if len(tokens & previous) / smaller >= threshold:
                duplicate = True
                break
        if duplicate:
            continue
        kept.append(item)
        seen.append(tokens)
    return kept


def fit_to_budget(items: list[RetrievedItem], *, max_chars: int) -> tuple[list[RetrievedItem], int]:
    """Take hits in score order until the character budget is spent."""
    if max_chars <= 0:
        return [], 0
    kept: list[RetrievedItem] = []
    used = 0
    for item in items:
        cost = len(item.text) + 1
        if used + cost > max_chars:
            continue
        kept.append(item)
        used += cost
    return kept, used
