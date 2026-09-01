from __future__ import annotations


SCOPED_MEMORY_TABLE = "memory_records"

SCOPED_MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  record_id TEXT NOT NULL UNIQUE,
  agent_id TEXT NOT NULL DEFAULT 'natsume-kokoro-001',
  scope_type TEXT NOT NULL CHECK(scope_type IN ('user','workspace','conversation','task')),
  scope_id TEXT NOT NULL,
  workspace TEXT NOT NULL DEFAULT '',
  key TEXT NOT NULL,
  content TEXT NOT NULL,
  normalized_content TEXT NOT NULL,
  content_fingerprint TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'project',
  category TEXT NOT NULL DEFAULT 'decision',
  memory_type TEXT,
  title TEXT,
  source_type TEXT NOT NULL DEFAULT 'user',
  source_task_id TEXT,
  source_conversation_id TEXT,
  source_message_id TEXT,
  source_metadata_json TEXT NOT NULL DEFAULT '{}',
  tags TEXT NOT NULL DEFAULT '[]',
  applicable_version TEXT,
  project_signature TEXT NOT NULL DEFAULT '{}',
  confidence REAL NOT NULL DEFAULT 0.7,
  importance REAL NOT NULL DEFAULT 0.5,
  emotional_weight REAL NOT NULL DEFAULT 0.0,
  access_count INTEGER NOT NULL DEFAULT 0,
  last_accessed_at TEXT,
  occurred_at TEXT,
  valid_from TEXT,
  valid_until TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  supersedes_record_id TEXT,
  user_confirmed INTEGER NOT NULL DEFAULT 0,
  is_locked INTEGER NOT NULL DEFAULT 0,
  is_sensitive INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  last_verified_at TEXT,
  last_used_at TEXT,
  use_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0,
  failure_count INTEGER NOT NULL DEFAULT 0,
  rejected INTEGER NOT NULL DEFAULT 0,
  invalidated_reason TEXT,
  legacy_table TEXT,
  legacy_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(agent_id, scope_type, scope_id, key)
);
CREATE INDEX IF NOT EXISTS idx_memory_records_scope
  ON memory_records(agent_id, scope_type, scope_id, rejected, confidence, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_records_fingerprint
  ON memory_records(agent_id, scope_type, scope_id, content_fingerprint, rejected);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_records_legacy
  ON memory_records(legacy_table, legacy_id)
  WHERE legacy_table IS NOT NULL AND legacy_id IS NOT NULL;
"""

LONG_TERM_MEMORY_COMPATIBILITY_TRIGGERS = """
CREATE TEMP TRIGGER IF NOT EXISTS memory_records_from_memories_insert
AFTER INSERT ON main.memories BEGIN
  INSERT INTO memory_records(
    record_id,agent_id,scope_type,scope_id,workspace,key,content,normalized_content,content_fingerprint,
    kind,category,memory_type,title,source_type,source_conversation_id,source_message_id,source_metadata_json,
    tags,project_signature,confidence,importance,emotional_weight,access_count,last_accessed_at,occurred_at,
    valid_from,valid_until,status,supersedes_record_id,user_confirmed,is_locked,is_sensitive,metadata_json,
    rejected,legacy_table,legacy_id,created_at,updated_at
  ) VALUES(
    NEW.id,NEW.agent_id,'user',COALESCE(NEW.user_id,'administrator-001'),'','long-term:' || NEW.id,
    NEW.content,memory_normalize(NEW.content),memory_fingerprint(NEW.content),'personal','long_term',
    NEW.memory_type,NEW.title,NEW.source_type,NEW.source_conversation_id,NEW.source_message_id,
    json_object('compatibility_source','memories'),'[]','{}',NEW.confidence,NEW.importance,
    NEW.emotional_weight,NEW.access_count,NEW.last_accessed_at,NEW.occurred_at,NEW.valid_from,NEW.valid_until,
    NEW.status,NEW.supersedes_memory_id,NEW.user_confirmed,NEW.is_locked,NEW.is_sensitive,NEW.metadata_json,
    CASE WHEN NEW.status IN ('deleted','rejected') THEN 1 ELSE 0 END,'memories',NEW.id,NEW.created_at,NEW.updated_at
  )
  ON CONFLICT(record_id) DO UPDATE SET
    agent_id=excluded.agent_id,scope_type=excluded.scope_type,scope_id=excluded.scope_id,
    content=excluded.content,normalized_content=excluded.normalized_content,
    content_fingerprint=excluded.content_fingerprint,memory_type=excluded.memory_type,title=excluded.title,
    source_type=excluded.source_type,source_conversation_id=excluded.source_conversation_id,
    source_message_id=excluded.source_message_id,confidence=excluded.confidence,importance=excluded.importance,
    emotional_weight=excluded.emotional_weight,access_count=excluded.access_count,
    last_accessed_at=excluded.last_accessed_at,occurred_at=excluded.occurred_at,
    valid_from=excluded.valid_from,valid_until=excluded.valid_until,status=excluded.status,
    supersedes_record_id=excluded.supersedes_record_id,user_confirmed=excluded.user_confirmed,
    is_locked=excluded.is_locked,is_sensitive=excluded.is_sensitive,metadata_json=excluded.metadata_json,
    rejected=excluded.rejected,updated_at=excluded.updated_at;
END;

CREATE TEMP TRIGGER IF NOT EXISTS memory_records_from_memories_update
AFTER UPDATE ON main.memories BEGIN
  UPDATE memory_records SET
    agent_id=NEW.agent_id,scope_type='user',scope_id=COALESCE(NEW.user_id,'administrator-001'),workspace='',
    content=NEW.content,normalized_content=memory_normalize(NEW.content),
    content_fingerprint=memory_fingerprint(NEW.content),memory_type=NEW.memory_type,title=NEW.title,
    source_type=NEW.source_type,source_conversation_id=NEW.source_conversation_id,
    source_message_id=NEW.source_message_id,confidence=NEW.confidence,importance=NEW.importance,
    emotional_weight=NEW.emotional_weight,access_count=NEW.access_count,last_accessed_at=NEW.last_accessed_at,
    occurred_at=NEW.occurred_at,valid_from=NEW.valid_from,valid_until=NEW.valid_until,status=NEW.status,
    supersedes_record_id=NEW.supersedes_memory_id,user_confirmed=NEW.user_confirmed,is_locked=NEW.is_locked,
    is_sensitive=NEW.is_sensitive,metadata_json=NEW.metadata_json,
    rejected=CASE WHEN NEW.status IN ('deleted','rejected') THEN 1 ELSE 0 END,updated_at=NEW.updated_at
  WHERE legacy_table='memories' AND legacy_id=OLD.id;
END;

CREATE TEMP TRIGGER IF NOT EXISTS memory_records_from_memories_delete
AFTER DELETE ON main.memories BEGIN
  UPDATE memory_records SET status='deleted',rejected=1,updated_at=OLD.updated_at
  WHERE legacy_table='memories' AND legacy_id=OLD.id;
END;
"""
