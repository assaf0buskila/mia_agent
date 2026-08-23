-- Brain: long-term memory, ingested knowledge, entities, knowledge gaps.
-- Additive only. Portable types so the same DDL applies on SQLite and PostgreSQL:
-- no SERIAL, no JSONB, no NOW() default, no gen_random_uuid(), no vector type.
-- Embeddings are base64 float32 in a TEXT column (app/brain/vectors.py, ADR-026).

CREATE TABLE IF NOT EXISTS brain_memories (
  id INTEGER PRIMARY KEY,
  memory_id VARCHAR(40) NOT NULL UNIQUE,
  subject VARCHAR(64) NOT NULL DEFAULT 'owner',
  kind VARCHAR(16) NOT NULL,
  category VARCHAR(32) NOT NULL DEFAULT 'other',
  text TEXT NOT NULL,
  importance INTEGER NOT NULL DEFAULT 1,
  confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  source VARCHAR(32) NOT NULL DEFAULT 'telegram',
  source_ref VARCHAR(255) NOT NULL DEFAULT '',
  occurred_at VARCHAR(64) NOT NULL DEFAULT '',
  created_at VARCHAR(64) NOT NULL DEFAULT '',
  updated_at VARCHAR(64) NOT NULL DEFAULT '',
  last_used_at VARCHAR(64) NOT NULL DEFAULT '',
  use_count INTEGER NOT NULL DEFAULT 0,
  superseded_by VARCHAR(40) NOT NULL DEFAULT '',
  entities_json TEXT NOT NULL DEFAULT '[]',
  embedding TEXT NOT NULL DEFAULT '',
  embedding_model VARCHAR(64) NOT NULL DEFAULT '',
  embedding_dim INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_brain_memories_memory_id ON brain_memories (memory_id);
CREATE INDEX IF NOT EXISTS ix_brain_memories_subject ON brain_memories (subject);
CREATE INDEX IF NOT EXISTS ix_brain_memories_kind ON brain_memories (kind);
CREATE INDEX IF NOT EXISTS ix_brain_memories_status ON brain_memories (status);
CREATE INDEX IF NOT EXISTS ix_brain_memories_live ON brain_memories (subject, status, kind);
CREATE INDEX IF NOT EXISTS ix_brain_memories_category ON brain_memories (category, status);

CREATE TABLE IF NOT EXISTS brain_knowledge_sources (
  id INTEGER PRIMARY KEY,
  source_id VARCHAR(64) NOT NULL UNIQUE,
  url VARCHAR(500) NOT NULL DEFAULT '',
  kind VARCHAR(32) NOT NULL DEFAULT '',
  content_hash VARCHAR(64) NOT NULL DEFAULT '',
  fetched_at VARCHAR(64) NOT NULL DEFAULT '',
  chunk_count INTEGER NOT NULL DEFAULT 0,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  error VARCHAR(255) NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_brain_knowledge_sources_source_id
  ON brain_knowledge_sources (source_id);

CREATE TABLE IF NOT EXISTS brain_knowledge_chunks (
  id INTEGER PRIMARY KEY,
  chunk_id VARCHAR(64) NOT NULL UNIQUE,
  source_id VARCHAR(64) NOT NULL,
  category VARCHAR(32) NOT NULL DEFAULT 'other',
  title VARCHAR(255) NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  url VARCHAR(500) NOT NULL DEFAULT '',
  ordinal INTEGER NOT NULL DEFAULT 0,
  content_hash VARCHAR(64) NOT NULL DEFAULT '',
  fetched_at VARCHAR(64) NOT NULL DEFAULT '',
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  embedding TEXT NOT NULL DEFAULT '',
  embedding_model VARCHAR(64) NOT NULL DEFAULT '',
  embedding_dim INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_brain_knowledge_chunks_chunk_id
  ON brain_knowledge_chunks (chunk_id);
CREATE INDEX IF NOT EXISTS ix_brain_knowledge_chunks_source_id
  ON brain_knowledge_chunks (source_id);
CREATE INDEX IF NOT EXISTS ix_brain_knowledge_chunks_status
  ON brain_knowledge_chunks (status);
CREATE INDEX IF NOT EXISTS ix_brain_chunks_source
  ON brain_knowledge_chunks (source_id, status);
CREATE INDEX IF NOT EXISTS ix_brain_chunks_category
  ON brain_knowledge_chunks (category, status);

CREATE TABLE IF NOT EXISTS brain_entities (
  id INTEGER PRIMARY KEY,
  entity_id VARCHAR(40) NOT NULL,
  entity_key VARCHAR(160) NOT NULL UNIQUE,
  kind VARCHAR(24) NOT NULL DEFAULT 'other',
  name VARCHAR(160) NOT NULL,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  summary TEXT NOT NULL DEFAULT '',
  mention_count INTEGER NOT NULL DEFAULT 0,
  first_seen_at VARCHAR(64) NOT NULL DEFAULT '',
  last_seen_at VARCHAR(64) NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_brain_entities_entity_id ON brain_entities (entity_id);
CREATE INDEX IF NOT EXISTS ix_brain_entities_entity_key ON brain_entities (entity_key);

CREATE TABLE IF NOT EXISTS brain_memory_entities (
  id INTEGER PRIMARY KEY,
  memory_id VARCHAR(40) NOT NULL,
  entity_key VARCHAR(160) NOT NULL,
  created_at VARCHAR(64) NOT NULL DEFAULT '',
  CONSTRAINT uq_brain_memory_entity UNIQUE (memory_id, entity_key)
);

CREATE INDEX IF NOT EXISTS ix_brain_memory_entities_memory_id
  ON brain_memory_entities (memory_id);
CREATE INDEX IF NOT EXISTS ix_brain_memory_entities_entity_key
  ON brain_memory_entities (entity_key);

CREATE TABLE IF NOT EXISTS brain_knowledge_gaps (
  id INTEGER PRIMARY KEY,
  gap_id VARCHAR(40) NOT NULL UNIQUE,
  topic VARCHAR(160) NOT NULL,
  question TEXT NOT NULL,
  category VARCHAR(32) NOT NULL DEFAULT 'other',
  priority INTEGER NOT NULL DEFAULT 1,
  status VARCHAR(16) NOT NULL DEFAULT 'open',
  asked_at VARCHAR(64) NOT NULL DEFAULT '',
  answered_at VARCHAR(64) NOT NULL DEFAULT '',
  created_at VARCHAR(64) NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_brain_knowledge_gaps_gap_id ON brain_knowledge_gaps (gap_id);
CREATE INDEX IF NOT EXISTS ix_brain_knowledge_gaps_topic ON brain_knowledge_gaps (topic);
CREATE INDEX IF NOT EXISTS ix_brain_gaps_status ON brain_knowledge_gaps (status, priority);
