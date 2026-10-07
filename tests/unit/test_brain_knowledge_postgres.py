"""PostgreSQL proof for deterministic knowledge chunk replacement."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from app.brain.schemas import KnowledgeCategory, KnowledgeChunk
from app.brain.store import BrainStore
from app.db.base import Base
from app.db.models import KnowledgeChunkRow
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker


def _chunk(source_id: str, chunk_id: str, text_value: str, ordinal: int) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=chunk_id,
        source_id=source_id,
        category=KnowledgeCategory.SERVICE,
        title="Services",
        text=text_value,
        url=f"https://www.assafweb.com/{source_id}",
        ordinal=ordinal,
        content_hash=f"hash-{chunk_id}",
    )


@pytest.mark.skipif(not os.getenv("MIA_TEST_POSTGRES_URL"), reason="test PostgreSQL DSN not set")
def test_postgres_partial_and_concurrent_refresh_reuse_deterministic_chunk_rows() -> None:
    schema = f"test_brain_{uuid4().hex}"
    admin = create_engine(os.environ["MIA_TEST_POSTGRES_URL"])
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        os.environ["MIA_TEST_POSTGRES_URL"],
        connect_args={"options": f"-csearch_path={schema}"},
    )
    factory = sessionmaker(engine, expire_on_commit=False)
    source_id = "postgres-refresh.md"
    stable = _chunk(source_id, "chk_pg_stable", "Stable cited evidence.", 0)
    original = _chunk(source_id, "chk_pg_original", "Original cited evidence.", 1)
    revised = _chunk(source_id, "chk_pg_revised", "Revised cited evidence.", 1)
    try:
        Base.metadata.create_all(engine)
        with factory() as session:
            store = BrainStore(session)
            store.upsert_knowledge_source(
                source_id=source_id,
                url=stable.url,
                kind="http",
                source_hash="seed",
                chunk_count=2,
            )
            store.replace_knowledge_chunks(
                source_id=source_id, chunks=[(stable, None), (original, None)]
            )
            session.commit()

        with factory() as session:
            store = BrainStore(session)
            store.replace_knowledge_chunks(
                source_id=source_id, chunks=[(stable, None), (revised, None)]
            )
            store.replace_knowledge_chunks(
                source_id=source_id, chunks=[(stable, None), (revised, None)]
            )
            session.commit()
            rows = session.scalars(
                select(KnowledgeChunkRow).where(KnowledgeChunkRow.source_id == source_id)
            ).all()
            assert len(rows) == 3
            assert {row.chunk_id for row in rows if row.status == "active"} == {
                stable.chunk_id,
                revised.chunk_id,
            }

        barrier = Barrier(2)

        def refresh(suffix: str) -> str:
            contender = _chunk(
                source_id,
                f"chk_pg_contender_{suffix}",
                f"Concurrent evidence {suffix}.",
                1,
            )
            with factory() as session:
                barrier.wait(timeout=5)
                BrainStore(session).replace_knowledge_chunks(
                    source_id=source_id, chunks=[(stable, None), (contender, None)]
                )
                session.commit()
            return contender.chunk_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            contender_ids = set(pool.map(refresh, ("a", "b")))

        with factory() as session:
            rows = session.scalars(
                select(KnowledgeChunkRow).where(KnowledgeChunkRow.source_id == source_id)
            ).all()
            active_ids = {row.chunk_id for row in rows if row.status == "active"}
            assert stable.chunk_id in active_ids
            assert len(active_ids & contender_ids) == 1
            assert len(rows) == 5
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
