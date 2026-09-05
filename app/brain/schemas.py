"""Typed records for Mia's brain. Serializable domain data only — no SDK objects, no secrets.

Vocabulary follows the documented agent-memory taxonomy (LangMem / Letta / Generative
Agents); the supersede-don't-delete rule follows Mem0g. Sources are cited in
docs/ARCHITECTURE.md (Brain).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# Park et al. rate memory poignancy 1..10. An importance below this is never written:
# the cheapest and most effective forgetting mechanism is not writing it down.
MIN_IMPORTANCE_TO_STORE = 3
MAX_IMPORTANCE = 10
MIN_IMPORTANCE = 1


class MemoryKind(StrEnum):
    """Which memory system a record belongs to."""

    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    WORKING = "working"
    PREFERENCE = "preference"


class MemoryCategory(StrEnum):
    """What the record is about. Drives filtered retrieval and the profile projection."""

    IDENTITY = "identity"
    BACKGROUND = "background"
    BUSINESS = "business"
    PROJECT = "project"
    SKILL = "skill"
    GOAL = "goal"
    PREFERENCE = "preference"
    COMMUNICATION = "communication"
    WORKFLOW = "workflow"
    DECISION = "decision"
    TASK = "task"
    RELATIONSHIP = "relationship"
    EVENT = "event"
    OTHER = "other"


class KnowledgeCategory(StrEnum):
    """Taxonomy for ingested website / business knowledge."""

    PERSONAL = "personal"
    PROJECT = "project"
    SERVICE = "service"
    PRODUCT = "product"
    EXPERIENCE = "experience"
    SKILL = "skill"
    PORTFOLIO = "portfolio"
    BUSINESS = "business"
    CURRENT_WORK = "current_work"
    CONTACT = "contact"
    PROCESS = "process"
    FAQ = "faq"
    TESTIMONIAL = "testimonial"
    PRICING = "pricing"
    OTHER = "other"


class EntityKind(StrEnum):
    PERSON = "person"
    COMPANY = "company"
    PRODUCT = "product"
    PROJECT = "project"
    TECHNOLOGY = "technology"
    OTHER = "other"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class MemorySource(StrEnum):
    """Provenance. Owner memory may only be written from owner-controlled surfaces."""

    TELEGRAM = "telegram"
    WEBSITE_KNOWLEDGE = "website_knowledge"
    MANUAL = "manual"
    REFLECTION = "reflection"
    TOOL = "tool"


class MemoryOperation(StrEnum):
    """The four documented Mem0 reconciliation operations."""

    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    NOOP = "noop"


def clamp_importance(value: object) -> int:
    """Coerce a model-supplied rating onto the documented 1..10 poignancy scale."""
    if isinstance(value, bool):
        return MIN_IMPORTANCE
    if isinstance(value, int | float):
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(float(value.strip()))
        except (ValueError, AttributeError):
            return MIN_IMPORTANCE
    else:
        return MIN_IMPORTANCE
    return max(MIN_IMPORTANCE, min(MAX_IMPORTANCE, number))


def clamp_confidence(value: object) -> float:
    if isinstance(value, bool):
        return 1.0
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (ValueError, AttributeError):
            return 1.0
    else:
        return 1.0
    if number != number:  # NaN
        return 1.0
    return max(0.0, min(1.0, number))


class MemoryCandidate(BaseModel):
    """A fact the extractor proposes storing. Not yet reconciled against existing memory."""

    model_config = ConfigDict(frozen=True)

    text: str
    kind: MemoryKind = MemoryKind.SEMANTIC
    category: MemoryCategory = MemoryCategory.OTHER
    importance: int = MIN_IMPORTANCE
    confidence: float = 1.0
    entities: tuple[str, ...] = ()
    supersedes_hint: str = ""

    def worth_storing(self) -> bool:
        return bool(self.text.strip()) and self.importance >= MIN_IMPORTANCE_TO_STORE


class MemoryRecord(BaseModel):
    """A stored memory, as read back for retrieval and prompting."""

    model_config = ConfigDict(frozen=True)

    memory_id: str
    kind: MemoryKind
    category: MemoryCategory
    text: str
    importance: int
    confidence: float
    status: MemoryStatus
    source: MemorySource
    source_ref: str = ""
    subject: str = "owner"
    occurred_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str = ""
    use_count: int = 0
    superseded_by: str = ""
    entities: tuple[str, ...] = ()


class KnowledgeChunk(BaseModel):
    """One retrievable piece of ingested website / business knowledge."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    source_id: str
    category: KnowledgeCategory
    title: str
    text: str
    url: str = ""
    ordinal: int = 0
    content_hash: str = ""
    fetched_at: str = ""


class KnowledgeEntity(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_id: str
    kind: EntityKind
    name: str
    aliases: tuple[str, ...] = ()
    summary: str = ""
    mention_count: int = 0
    first_seen_at: str = ""
    last_seen_at: str = ""


class RetrievedItem(BaseModel):
    """A scored retrieval hit with the provenance needed to prevent hallucination."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    text: str
    origin: str
    label: str = ""
    score: float = 0.0
    similarity: float = 0.0
    recency: float = 0.0
    importance: float = 0.0
    source_ref: str = ""
    # The category assigned at ingest (`KnowledgeCategory`). Carried so the answer
    # path can trust it instead of re-deriving "is this a price" by substring.
    category: str = ""

    def provenance(self) -> str:
        """Short, promptable citation. Never contains secrets or raw provider payloads."""
        if self.source_ref:
            return f"{self.origin}:{self.source_ref}"
        return self.origin


class BrainContext(BaseModel):
    """Everything the brain decided to put in front of the model for one request."""

    model_config = ConfigDict(frozen=True)

    profile: str = ""
    memories: tuple[RetrievedItem, ...] = ()
    knowledge: tuple[RetrievedItem, ...] = ()
    open_questions: tuple[str, ...] = ()
    used_chars: int = 0
    degraded: bool = False

    def is_empty(self) -> bool:
        return not (self.profile or self.memories or self.knowledge)


class KnowledgeGap(BaseModel):
    """Something Mia noticed she does not know. Asked at most once, then recorded."""

    model_config = ConfigDict(frozen=True)

    gap_id: str
    topic: str
    question: str
    category: MemoryCategory = MemoryCategory.OTHER
    priority: int = MIN_IMPORTANCE
    status: str = "open"
    asked_at: str = ""
    answered_at: str = ""
    created_at: str = ""


class ExtractionResult(BaseModel):
    """Outcome of one background extraction pass over a closed owner exchange."""

    candidates: tuple[MemoryCandidate, ...] = ()
    gaps: tuple[str, ...] = Field(default_factory=tuple)
    skipped: int = 0
