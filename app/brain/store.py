"""Persistence for Mia's brain.

A separate store from `LeadStore` on purpose: `LeadStore` is 2900 lines of
heavily-tested sales/ops state, and the brain has a different lifecycle, different
write policy and different failure mode. They share the session, not the class.

Supersede, never delete. An outdated fact keeps its row with `status='superseded'` and a
`superseded_by` pointer, so "why does Mia think X?" stays answerable months later.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.brain.schemas import (
    EntityKind,
    KnowledgeCategory,
    KnowledgeChunk,
    KnowledgeEntity,
    KnowledgeGap,
    MemoryCategory,
    MemoryKind,
    MemoryRecord,
    MemorySource,
    MemoryStatus,
    clamp_confidence,
    clamp_importance,
)
from app.brain.vectors import encode_vector
from app.db.models import (
    KnowledgeChunkRow,
    KnowledgeEntityRow,
    KnowledgeGapRow,
    KnowledgeSourceRow,
    MemoryEntityLinkRow,
    MemoryRow,
)

MAX_MEMORY_TEXT = 1200
MAX_CHUNK_TEXT = 4000
MAX_ENTITY_NAME = 160
# Ceiling on rows pulled into the in-process similarity scan. Exact search over a few
# thousand vectors is ~170ms; this bounds the worst case if the corpus ever grows.
MAX_SCAN_ROWS = 5000


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_memory_id() -> str:
    return f"mem_{uuid4().hex[:16]}"


def new_gap_id() -> str:
    return f"gap_{uuid4().hex[:16]}"


def new_entity_id() -> str:
    return f"ent_{uuid4().hex[:16]}"


def entity_key(name: str) -> str:
    """Case- and whitespace-insensitive key so 'MYstudio' and 'mystudio' are one entity."""
    return " ".join(name.strip().lower().split())[:MAX_ENTITY_NAME]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_id_for(source_id: str, ordinal: int, text: str) -> str:
    digest = hashlib.sha256(f"{source_id}:{ordinal}:{text}".encode()).hexdigest()
    return f"chk_{digest[:24]}"


def _json_list(raw: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed if isinstance(item, str))


def _enum_or_default(enum_cls, value: str, default):
    try:
        return enum_cls(value)
    except ValueError:
        return default


class BrainStore:
    """Read/write access to memories, knowledge, entities and gaps."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------------------------------------------------------------- memories

    def save_memory(
        self,
        *,
        text: str,
        kind: MemoryKind,
        category: MemoryCategory,
        importance: int,
        confidence: float = 1.0,
        source: MemorySource = MemorySource.TELEGRAM,
        source_ref: str = "",
        subject: str = "owner",
        occurred_at: str = "",
        entities: tuple[str, ...] = (),
        embedding: list[float] | None = None,
        embedding_model: str = "",
    ) -> str:
        """Insert one memory. Returns its `memory_id`."""
        cleaned = text.strip()[:MAX_MEMORY_TEXT]
        if not cleaned:
            raise ValueError("refusing to store an empty memory")
        stamp = now_iso()
        memory_id = new_memory_id()
        row = MemoryRow(
            memory_id=memory_id,
            subject=subject,
            kind=kind.value,
            category=category.value,
            text=cleaned,
            importance=clamp_importance(importance),
            confidence=clamp_confidence(confidence),
            status=MemoryStatus.ACTIVE.value,
            source=source.value,
            source_ref=source_ref[:255],
            occurred_at=occurred_at or stamp,
            created_at=stamp,
            updated_at=stamp,
            last_used_at=stamp,
            use_count=0,
            superseded_by="",
            entities_json=json.dumps(list(entities), ensure_ascii=False),
            embedding=encode_vector(embedding) if embedding else "",
            embedding_model=embedding_model if embedding else "",
            embedding_dim=len(embedding) if embedding else 0,
        )
        self.session.add(row)
        self.session.flush()
        for name in entities:
            self.link_entity(memory_id=memory_id, name=name)
        return memory_id

    def update_memory(
        self,
        memory_id: str,
        *,
        text: str | None = None,
        importance: int | None = None,
        confidence: float | None = None,
        category: MemoryCategory | None = None,
        embedding: list[float] | None = None,
        embedding_model: str = "",
    ) -> bool:
        """Rewrite a memory in place (LangMem profile semantics: update, don't duplicate)."""
        row = self._memory_row(memory_id)
        if row is None:
            return False
        if text is not None:
            cleaned = text.strip()[:MAX_MEMORY_TEXT]
            if cleaned:
                row.text = cleaned
        if importance is not None:
            row.importance = clamp_importance(importance)
        if confidence is not None:
            row.confidence = clamp_confidence(confidence)
        if category is not None:
            row.category = category.value
        if embedding:
            row.embedding = encode_vector(embedding)
            row.embedding_model = embedding_model
            row.embedding_dim = len(embedding)
        row.updated_at = now_iso()
        self.session.flush()
        return True

    def supersede_memory(self, memory_id: str, *, replacement_id: str = "") -> bool:
        """Mark a memory invalid without deleting it (Mem0g: mark invalid, don't remove)."""
        row = self._memory_row(memory_id)
        if row is None or row.status != MemoryStatus.ACTIVE.value:
            return False
        row.status = MemoryStatus.SUPERSEDED.value
        row.superseded_by = replacement_id
        row.updated_at = now_iso()
        self.session.flush()
        return True

    def touch_memories(self, memory_ids: list[str]) -> None:
        """Bump last-used on every memory actually put in the prompt.

        The recency component decays over *last access*, not creation, so this has to run
        on retrieval or recency scoring is meaningless.
        """
        if not memory_ids:
            return
        stamp = now_iso()
        rows = self.session.scalars(
            select(MemoryRow).where(MemoryRow.memory_id.in_(memory_ids))
        ).all()
        for row in rows:
            row.last_used_at = stamp
            row.use_count = int(row.use_count or 0) + 1
        self.session.flush()

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        row = self._memory_row(memory_id)
        return self._to_record(row) if row is not None else None

    def list_memories(
        self,
        *,
        subject: str = "owner",
        kinds: tuple[MemoryKind, ...] = (),
        categories: tuple[MemoryCategory, ...] = (),
        status: MemoryStatus = MemoryStatus.ACTIVE,
        limit: int = MAX_SCAN_ROWS,
    ) -> list[MemoryRecord]:
        stmt = select(MemoryRow).where(
            MemoryRow.subject == subject, MemoryRow.status == status.value
        )
        if kinds:
            stmt = stmt.where(MemoryRow.kind.in_([item.value for item in kinds]))
        if categories:
            stmt = stmt.where(MemoryRow.category.in_([item.value for item in categories]))
        stmt = stmt.order_by(
            MemoryRow.importance.desc(), MemoryRow.updated_at.desc(), MemoryRow.id.desc()
        ).limit(min(limit, MAX_SCAN_ROWS))
        return [self._to_record(row) for row in self.session.scalars(stmt).all()]

    def memory_vectors(
        self,
        *,
        subject: str = "owner",
        kinds: tuple[MemoryKind, ...] = (),
        limit: int = MAX_SCAN_ROWS,
    ) -> list[tuple[str, str]]:
        """`(memory_id, encoded_vector)` for live rows that actually carry an embedding."""
        stmt = select(MemoryRow.memory_id, MemoryRow.embedding).where(
            MemoryRow.subject == subject,
            MemoryRow.status == MemoryStatus.ACTIVE.value,
            MemoryRow.embedding != "",
        )
        if kinds:
            stmt = stmt.where(MemoryRow.kind.in_([item.value for item in kinds]))
        stmt = stmt.limit(min(limit, MAX_SCAN_ROWS))
        return [(row[0], row[1]) for row in self.session.execute(stmt).all()]

    def count_memories(self, *, status: MemoryStatus = MemoryStatus.ACTIVE) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(MemoryRow)
                .where(MemoryRow.status == status.value)
            )
            or 0
        )

    def _memory_row(self, memory_id: str) -> MemoryRow | None:
        if not memory_id:
            return None
        return self.session.scalars(
            select(MemoryRow).where(MemoryRow.memory_id == memory_id)
        ).first()

    def _to_record(self, row: MemoryRow) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row.memory_id,
            kind=_enum_or_default(MemoryKind, row.kind, MemoryKind.SEMANTIC),
            category=_enum_or_default(MemoryCategory, row.category, MemoryCategory.OTHER),
            text=row.text,
            importance=int(row.importance or 1),
            confidence=float(row.confidence or 1.0),
            status=_enum_or_default(MemoryStatus, row.status, MemoryStatus.ACTIVE),
            source=_enum_or_default(MemorySource, row.source, MemorySource.TELEGRAM),
            source_ref=row.source_ref or "",
            subject=row.subject or "owner",
            occurred_at=row.occurred_at or "",
            created_at=row.created_at or "",
            updated_at=row.updated_at or "",
            last_used_at=row.last_used_at or "",
            use_count=int(row.use_count or 0),
            superseded_by=row.superseded_by or "",
            entities=_json_list(row.entities_json),
        )

    # --------------------------------------------------------------- knowledge

    def upsert_knowledge_source(
        self,
        *,
        source_id: str,
        url: str,
        kind: str,
        source_hash: str,
        chunk_count: int,
        error: str = "",
    ) -> None:
        row = self.session.scalars(
            select(KnowledgeSourceRow).where(KnowledgeSourceRow.source_id == source_id)
        ).first()
        stamp = now_iso()
        if row is None:
            row = KnowledgeSourceRow(source_id=source_id)
            self.session.add(row)
        row.url = url[:500]
        row.kind = kind[:32]
        row.content_hash = source_hash
        row.fetched_at = stamp
        row.chunk_count = chunk_count
        row.status = "error" if error else "active"
        row.error = error[:255]
        self.session.flush()

    def knowledge_source_hash(self, source_id: str) -> str:
        row = self.session.scalars(
            select(KnowledgeSourceRow).where(KnowledgeSourceRow.source_id == source_id)
        ).first()
        return row.content_hash if row is not None else ""

    def replace_knowledge_chunks(
        self,
        *,
        source_id: str,
        chunks: list[tuple[KnowledgeChunk, list[float] | None]],
        embedding_model: str = "",
    ) -> int:
        """Re-ingest is idempotent: retire the old chunks for this source, insert the new.

        Retiring rather than deleting keeps provenance for anything already cited.
        """
        existing = self.session.scalars(
            select(KnowledgeChunkRow).where(
                KnowledgeChunkRow.source_id == source_id,
                KnowledgeChunkRow.status == "active",
            )
        ).all()
        for row in existing:
            row.status = "retired"
        stamp = now_iso()
        written = 0
        for chunk, embedding in chunks:
            cleaned = chunk.text.strip()[:MAX_CHUNK_TEXT]
            if not cleaned:
                continue
            self.session.add(
                KnowledgeChunkRow(
                    chunk_id=chunk.chunk_id,
                    source_id=source_id,
                    category=chunk.category.value,
                    title=chunk.title[:255],
                    text=cleaned,
                    url=chunk.url[:500],
                    ordinal=chunk.ordinal,
                    content_hash=chunk.content_hash or content_hash(cleaned),
                    fetched_at=stamp,
                    status="active",
                    embedding=encode_vector(embedding) if embedding else "",
                    embedding_model=embedding_model if embedding else "",
                    embedding_dim=len(embedding) if embedding else 0,
                )
            )
            written += 1
        self.session.flush()
        return written

    def list_knowledge_chunks(
        self,
        *,
        categories: tuple[KnowledgeCategory, ...] = (),
        source_id: str = "",
        limit: int = MAX_SCAN_ROWS,
    ) -> list[KnowledgeChunk]:
        stmt = select(KnowledgeChunkRow).where(KnowledgeChunkRow.status == "active")
        if source_id:
            stmt = stmt.where(KnowledgeChunkRow.source_id == source_id)
        if categories:
            stmt = stmt.where(
                KnowledgeChunkRow.category.in_([item.value for item in categories])
            )
        stmt = stmt.order_by(KnowledgeChunkRow.source_id, KnowledgeChunkRow.ordinal).limit(
            min(limit, MAX_SCAN_ROWS)
        )
        return [self._to_chunk(row) for row in self.session.scalars(stmt).all()]

    def get_knowledge_chunks(self, chunk_ids: list[str]) -> list[KnowledgeChunk]:
        if not chunk_ids:
            return []
        rows = self.session.scalars(
            select(KnowledgeChunkRow).where(KnowledgeChunkRow.chunk_id.in_(chunk_ids))
        ).all()
        return [self._to_chunk(row) for row in rows]

    def knowledge_vectors(
        self, *, source_id: str = "", limit: int = MAX_SCAN_ROWS
    ) -> list[tuple[str, str]]:
        stmt = select(KnowledgeChunkRow.chunk_id, KnowledgeChunkRow.embedding).where(
            KnowledgeChunkRow.status == "active", KnowledgeChunkRow.embedding != ""
        )
        if source_id:
            stmt = stmt.where(KnowledgeChunkRow.source_id == source_id)
        return [
            (row[0], row[1])
            for row in self.session.execute(stmt.limit(min(limit, MAX_SCAN_ROWS))).all()
        ]

    def count_knowledge_chunks(self) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(KnowledgeChunkRow)
                .where(KnowledgeChunkRow.status == "active")
            )
            or 0
        )

    def _to_chunk(self, row: KnowledgeChunkRow) -> KnowledgeChunk:
        return KnowledgeChunk(
            chunk_id=row.chunk_id,
            source_id=row.source_id,
            category=_enum_or_default(
                KnowledgeCategory, row.category, KnowledgeCategory.OTHER
            ),
            title=row.title or "",
            text=row.text,
            url=row.url or "",
            ordinal=int(row.ordinal or 0),
            content_hash=row.content_hash or "",
            fetched_at=row.fetched_at or "",
        )

    # ---------------------------------------------------------------- entities

    def link_entity(
        self,
        *,
        memory_id: str,
        name: str,
        kind: EntityKind = EntityKind.OTHER,
        summary: str = "",
    ) -> str:
        key = entity_key(name)
        if not key:
            return ""
        stamp = now_iso()
        row = self.session.scalars(
            select(KnowledgeEntityRow).where(KnowledgeEntityRow.entity_key == key)
        ).first()
        if row is None:
            row = KnowledgeEntityRow(
                entity_id=new_entity_id(),
                entity_key=key,
                kind=kind.value,
                name=name.strip()[:MAX_ENTITY_NAME],
                aliases_json="[]",
                summary=summary,
                mention_count=0,
                first_seen_at=stamp,
            )
            self.session.add(row)
        if kind is not EntityKind.OTHER and row.kind == EntityKind.OTHER.value:
            row.kind = kind.value
        if summary and not row.summary:
            row.summary = summary
        row.mention_count = int(row.mention_count or 0) + 1
        row.last_seen_at = stamp
        existing_link = self.session.scalars(
            select(MemoryEntityLinkRow).where(
                MemoryEntityLinkRow.memory_id == memory_id,
                MemoryEntityLinkRow.entity_key == key,
            )
        ).first()
        if existing_link is None and memory_id:
            self.session.add(
                MemoryEntityLinkRow(memory_id=memory_id, entity_key=key, created_at=stamp)
            )
        self.session.flush()
        return key

    def list_entities(self, *, limit: int = 100) -> list[KnowledgeEntity]:
        rows = self.session.scalars(
            select(KnowledgeEntityRow)
            .order_by(KnowledgeEntityRow.mention_count.desc(), KnowledgeEntityRow.name)
            .limit(limit)
        ).all()
        return [
            KnowledgeEntity(
                entity_id=row.entity_id,
                kind=_enum_or_default(EntityKind, row.kind, EntityKind.OTHER),
                name=row.name,
                aliases=_json_list(row.aliases_json),
                summary=row.summary or "",
                mention_count=int(row.mention_count or 0),
                first_seen_at=row.first_seen_at or "",
                last_seen_at=row.last_seen_at or "",
            )
            for row in rows
        ]

    def memory_ids_for_entity(self, name: str, *, limit: int = 50) -> list[str]:
        key = entity_key(name)
        if not key:
            return []
        rows = self.session.scalars(
            select(MemoryEntityLinkRow.memory_id)
            .where(MemoryEntityLinkRow.entity_key == key)
            .limit(limit)
        ).all()
        return list(rows)

    # -------------------------------------------------------------------- gaps

    def open_gap(
        self,
        *,
        topic: str,
        question: str,
        category: MemoryCategory = MemoryCategory.OTHER,
        priority: int = 5,
    ) -> str:
        """Record something Mia does not know. One open gap per topic, ever."""
        normalized = " ".join(topic.strip().lower().split())[:160]
        if not normalized or not question.strip():
            return ""
        existing = self.session.scalars(
            select(KnowledgeGapRow).where(
                KnowledgeGapRow.topic == normalized,
                KnowledgeGapRow.status.in_(("open", "asked")),
            )
        ).first()
        if existing is not None:
            return existing.gap_id
        gap_id = new_gap_id()
        self.session.add(
            KnowledgeGapRow(
                gap_id=gap_id,
                topic=normalized,
                question=question.strip(),
                category=category.value,
                priority=clamp_importance(priority),
                status="open",
                created_at=now_iso(),
            )
        )
        self.session.flush()
        return gap_id

    def list_open_gaps(self, *, limit: int = 10) -> list[KnowledgeGap]:
        rows = self.session.scalars(
            select(KnowledgeGapRow)
            .where(KnowledgeGapRow.status == "open")
            .order_by(KnowledgeGapRow.priority.desc(), KnowledgeGapRow.id)
            .limit(limit)
        ).all()
        return [self._to_gap(row) for row in rows]

    def mark_gap_asked(self, gap_id: str) -> bool:
        row = self._gap_row(gap_id)
        if row is None or row.status != "open":
            return False
        row.status = "asked"
        row.asked_at = now_iso()
        self.session.flush()
        return True

    def resolve_gap(self, gap_id: str) -> bool:
        row = self._gap_row(gap_id)
        if row is None or row.status == "answered":
            return False
        row.status = "answered"
        row.answered_at = now_iso()
        self.session.flush()
        return True

    def _gap_row(self, gap_id: str) -> KnowledgeGapRow | None:
        if not gap_id:
            return None
        return self.session.scalars(
            select(KnowledgeGapRow).where(KnowledgeGapRow.gap_id == gap_id)
        ).first()

    def _to_gap(self, row: KnowledgeGapRow) -> KnowledgeGap:
        return KnowledgeGap(
            gap_id=row.gap_id,
            topic=row.topic,
            question=row.question,
            category=_enum_or_default(MemoryCategory, row.category, MemoryCategory.OTHER),
            priority=int(row.priority or 1),
            status=row.status,
            asked_at=row.asked_at or "",
            answered_at=row.answered_at or "",
            created_at=row.created_at or "",
        )
