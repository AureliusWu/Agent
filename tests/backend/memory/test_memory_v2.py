from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.admin_action_grants import consume_admin_action_grant, issue_admin_action_grant
from app import database
from app.database_modules.migrations import migration_v45
from app.main import app
from app.memory.catalog import authoritative_user_memories
from app.memory.long_term import create_memory, delete_memory, list_memories, update_memory
from app.memory.service import (
    delete_workspace_memory,
    list_workspace_memories,
    memory_feedback,
    retrieve_memories,
    update_workspace_memory,
    upsert_workspace_memory,
)


def _fresh_database(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "memory-v2.db"
    monkeypatch.setattr(database.settings, "database_path", path)
    database.init_db()
    return path


def _conversation_and_task(workspace: Path) -> tuple[int, str]:
    stamp = database.now_iso()
    with database.connect() as db:
        conversation_id = int(
            db.execute(
                "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("scope", str(workspace), "ask", stamp, stamp),
            ).lastrowid
        )
        task_id = "memory-v2-task"
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "completed", "scope", stamp, stamp),
        )
    return conversation_id, task_id


def _authorization(operation: str, target_id: str, payload: dict):
    ui_session_id = f"memory-v2-{operation}"
    grant = issue_admin_action_grant(
        operation=operation,
        target_id=target_id,
        payload=payload,
        ui_session_id=ui_session_id,
    )
    return consume_admin_action_grant(
        grant["grant_token"],
        operation=operation,
        target_id=target_id,
        payload=payload,
        ui_session_id=ui_session_id,
    )


def test_v45_migrates_legacy_memory_without_deleting_history(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "legacy-v44.db"
    with sqlite3.connect(path) as db:
        db.executescript(database.SCHEMA)
        db.executemany(
            "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
            [(version, "legacy") for version in range(1, 45)],
        )
        db.execute(
            "INSERT INTO workspace_memories(workspace,key,content,source,namespace,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(tmp_path), "build.command", "pytest -q", "user", "project", "old", "old"),
        )
    monkeypatch.setattr(database.settings, "database_path", path)

    database.init_db()

    with sqlite3.connect(path) as db:
        legacy = db.execute("SELECT id,key,content FROM workspace_memories").fetchall()
        migrated = db.execute(
            "SELECT id,scope_type,scope_id,key,content_fingerprint,source_metadata_json,legacy_table,legacy_id "
            "FROM memory_records"
        ).fetchone()
        version = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    assert legacy == [(1, "build.command", "pytest -q")]
    assert migrated[0] == 1
    assert migrated[1:4] == ("workspace", str(tmp_path), "build.command")
    assert len(migrated[4]) == 64
    assert json.loads(migrated[5])["migrated_from"] == "workspace_memories"
    assert migrated[6:] == ("workspace_memories", "1")
    assert version == database.SCHEMA_VERSION
    assert database.rows("SELECT version FROM schema_migrations WHERE version=45") == [{"version": 45}]
    assert list_workspace_memories(str(tmp_path))[0]["id"] == 1


def test_schema42_dual_sources_migrate_to_one_deduplicated_authoritative_read(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "dual-source-v42.db"
    shared = "用户偏好使用中文"
    with sqlite3.connect(path) as db:
        db.executescript(database.SCHEMA)
        db.executemany(
            "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
            [(version, "legacy") for version in range(1, 43)],
        )
        db.execute(
            "INSERT INTO workspace_memories(workspace,key,content,source,namespace,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("__personal__", "preference.language", shared, "user", "personal", "old", "old"),
        )
        db.execute(
            "INSERT INTO memories(id,agent_id,user_id,memory_type,content,normalized_content,source_type,"
            "confidence,importance,created_at,updated_at,status,metadata_json,user_confirmed) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "mem_legacy_shared",
                "natsume-kokoro-001",
                "administrator-001",
                "semantic",
                shared,
                shared.casefold(),
                "user_confirmed",
                1.0,
                1.0,
                "old",
                "newer",
                "active",
                "{}",
                1,
            ),
        )
    monkeypatch.setattr(database.settings, "database_path", path)

    database.init_db()

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM workspace_memories").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_records WHERE scope_type='user'").fetchone()[0] == 2
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == database.SCHEMA_VERSION
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'memory_records_from_memories_%'"
        ).fetchall() == []
    authoritative = authoritative_user_memories()
    assert [item["id"] for item in authoritative] == ["mem_legacy_shared"]
    assert authoritative[0]["dedup_aliases"] == ["workspace-memory:1"]
    assert authoritative[0]["provenance"]["legacy_table"] == "memories"
    assert [item["id"] for item in list_memories()] == ["mem_legacy_shared"]
    alias_changes = {"content": "must not update the long-term winner"}
    with pytest.raises(ValueError, match="Compatibility memory must be edited"):
        update_memory(
            "workspace-memory:1",
            alias_changes,
            authorization=_authorization("memory.update", "workspace-memory:1", alias_changes),
        )
    with pytest.raises(ValueError, match="Compatibility memory must be deleted"):
        delete_memory(
            "workspace-memory:1",
            authorization=_authorization("memory.delete", "workspace-memory:1", {}),
        )
    assert database.rows("SELECT content,status FROM memories WHERE id='mem_legacy_shared'") == [
        {"content": shared, "status": "active"}
    ]
    assert list((tmp_path / "backups").glob(f"pre-migration-v42-to-v{database.SCHEMA_VERSION}-*.db"))


def test_v45_migrates_memories_when_workspace_memories_table_is_absent(tmp_path: Path) -> None:
    path = tmp_path / "legacy-long-term-only.db"
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.executescript(database.SCHEMA)
        db.execute("DROP TABLE workspace_memories")
        db.execute(
            "INSERT INTO memories(id,agent_id,user_id,memory_type,content,normalized_content,source_type,"
            "confidence,importance,created_at,updated_at,status,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "mem_without_workspace_table",
                "natsume-kokoro-001",
                "administrator-001",
                "semantic",
                "legacy long-term survives",
                "legacy long-term survives",
                "manual_entry",
                0.8,
                0.7,
                "old",
                "old",
                "active",
                "{}",
            ),
        )

        migration_v45(db)

        migrated = db.execute(
            "SELECT record_id,legacy_table,legacy_id,content FROM memory_records"
        ).fetchall()
        temp_triggers = {
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_temp_master "
                "WHERE type='trigger' AND name LIKE 'memory_records_from_memories_%'"
            )
        }

    assert [tuple(row) for row in migrated] == [
        (
            "mem_without_workspace_table",
            "memories",
            "mem_without_workspace_table",
            "legacy long-term survives",
        )
    ]
    assert temp_triggers == {
        "memory_records_from_memories_insert",
        "memory_records_from_memories_update",
        "memory_records_from_memories_delete",
    }


def test_every_operational_connection_installs_temporary_legacy_mirror_triggers(
    tmp_path: Path, monkeypatch
) -> None:
    path = _fresh_database(tmp_path, monkeypatch)
    expected = {
        "memory_records_from_memories_insert",
        "memory_records_from_memories_update",
        "memory_records_from_memories_delete",
    }

    for connection_number in (1, 2):
        with database.connect() as db:
            actual = {
                str(row[0])
                for row in db.execute(
                    "SELECT name FROM sqlite_temp_master "
                    "WHERE type='trigger' AND name LIKE 'memory_records_from_memories_%'"
                )
            }
            assert actual == expected
            memory_id = f"mem_connection_{connection_number}"
            db.execute(
                "INSERT INTO memories(id,agent_id,user_id,memory_type,content,normalized_content,source_type,"
                "confidence,importance,created_at,updated_at,status,metadata_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    memory_id,
                    "natsume-kokoro-001",
                    "administrator-001",
                    "semantic",
                    f"connection {connection_number}",
                    f"connection {connection_number}",
                    "manual_entry",
                    0.8,
                    0.5,
                    "now",
                    "now",
                    "active",
                    "{}",
                ),
            )
            assert db.execute(
                "SELECT content FROM memory_records WHERE record_id=?", (memory_id,)
            ).fetchone()[0] == f"connection {connection_number}"

    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='trigger' AND name LIKE 'memory_records_from_memories_%'"
        ).fetchall() == []


def test_long_term_trigger_stays_in_sync_and_runtime_retrieval_remains_workspace_only(
    tmp_path: Path, monkeypatch
) -> None:
    _fresh_database(tmp_path, monkeypatch)
    marker = "scope-isolation-marker"
    personal = create_memory(memory_type="semantic", content=f"personal {marker}")
    project = upsert_workspace_memory(
        str(tmp_path), key="project.marker", content=f"project {marker}", verified=True
    )

    mirrored = database.rows(
        "SELECT record_id,content,legacy_table,legacy_id FROM memory_records WHERE record_id=?",
        (personal["id"],),
    )
    with database.connect() as db:
        db.execute(
            "UPDATE memories SET content=?,normalized_content=?,updated_at=? WHERE id=?",
            (f"personal updated {marker}", f"personal updated {marker}", database.now_iso(), personal["id"]),
        )
    mirrored_after_update = database.rows(
        "SELECT content,normalized_content,length(content_fingerprint) AS fingerprint_length "
        "FROM memory_records WHERE record_id=?",
        (personal["id"],),
    )[0]
    runtime = retrieve_memories(str(tmp_path), marker)

    assert mirrored == [
        {
            "record_id": personal["id"],
            "content": f"personal {marker}",
            "legacy_table": "memories",
            "legacy_id": personal["id"],
        }
    ]
    assert mirrored_after_update == {
        "content": f"personal updated {marker}",
        "normalized_content": f"personal updated {marker}",
        "fingerprint_length": 64,
    }
    assert [item["id"] for item in runtime["items"]] == [project["id"]]
    assert f"personal updated {marker}" not in runtime["context"]


def test_compatibility_records_cannot_be_mutated_through_the_wrong_api(
    tmp_path: Path, monkeypatch
) -> None:
    _fresh_database(tmp_path, monkeypatch)
    long_term = create_memory(memory_type="semantic", content="owned by long-term API")
    long_term_row_id = int(
        database.rows(
            "SELECT id FROM memory_records WHERE legacy_table='memories' AND legacy_id=?",
            (long_term["id"],),
        )[0]["id"]
    )
    long_term_compatibility = next(
        item
        for item in list_workspace_memories("", namespace="personal")
        if item["record_id"] == long_term["id"]
    )
    assert long_term_compatibility["owner_api"] == "long_term"
    assert long_term_compatibility["editable_via_current_api"] is False
    assert long_term_compatibility["read_only_compatibility"] is True

    with pytest.raises(ValueError, match="长期记忆必须通过长期记忆接口修改"):
        update_workspace_memory("", long_term_row_id, {"content": "wrong owner"})
    with pytest.raises(ValueError, match="长期记忆必须通过长期记忆接口删除"):
        delete_workspace_memory("", long_term_row_id)
    with pytest.raises(ValueError, match="长期记忆反馈必须通过长期记忆接口处理"):
        memory_feedback("", long_term_row_id, "success")

    scoped = upsert_workspace_memory(
        "",
        namespace="personal",
        key="preference.owner",
        content="owned by scoped API",
        source="user",
    )
    scoped_record_id = str(scoped["record_id"])
    assert scoped["owner_api"] == "scoped"
    assert scoped["editable_via_current_api"] is True
    assert scoped["read_only_compatibility"] is False
    scoped_in_long_term_api = next(
        item for item in list_memories() if item["id"] == scoped_record_id
    )
    assert scoped_in_long_term_api["owner_api"] == "scoped"
    assert scoped_in_long_term_api["editable_via_current_api"] is False
    assert scoped_in_long_term_api["read_only_compatibility"] is True
    changes = {"content": "wrong owner"}
    with pytest.raises(ValueError, match="Compatibility memory must be edited"):
        update_memory(
            scoped_record_id,
            changes,
            authorization=_authorization("memory.update", scoped_record_id, changes),
        )
    with pytest.raises(ValueError, match="Compatibility memory must be deleted"):
        delete_memory(
            scoped_record_id,
            authorization=_authorization("memory.delete", scoped_record_id, {}),
        )


def test_active_compatibility_memory_wins_over_deleted_duplicate_and_rejected_is_filtered(
    tmp_path: Path, monkeypatch
) -> None:
    _fresh_database(tmp_path, monkeypatch)
    content = "shared lifecycle content"
    old = create_memory(memory_type="semantic", content=content)
    mirrored = database.rows(
        "SELECT normalized_content,content_fingerprint FROM memory_records WHERE record_id=?",
        (old["id"],),
    )[0]
    stamp = database.now_iso()
    with database.connect() as db:
        db.execute(
            "INSERT INTO memory_records(record_id,agent_id,scope_type,scope_id,workspace,key,content,"
            "normalized_content,content_fingerprint,kind,category,memory_type,source_type,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "memory_active_compatibility",
                "natsume-kokoro-001",
                "user",
                "administrator-001",
                "",
                "preference.lifecycle",
                content,
                mirrored["normalized_content"],
                mirrored["content_fingerprint"],
                "project",
                "decision",
                "semantic",
                "user",
                stamp,
                stamp,
            ),
        )
        db.execute("UPDATE memories SET status='deleted',updated_at=? WHERE id=?", (stamp, old["id"]))

    active = authoritative_user_memories(statuses={"active"})
    assert [item["id"] for item in active if item["content"] == content] == [
        "memory_active_compatibility"
    ]
    assert list_memories(status="active")[0]["id"] == "memory_active_compatibility"
    assert list_memories(status="deleted")[0]["id"] == old["id"]

    personal = upsert_workspace_memory(
        "",
        namespace="personal",
        key="preference.rejected",
        content="reject this scoped record",
        source="user",
    )
    memory_feedback("", int(personal["id"]), "reject")
    visible_ids = {
        int(item["id"])
        for item in list_workspace_memories("", namespace="personal", include_rejected=False)
    }
    assert int(personal["id"]) not in visible_ids


def test_sensitive_long_term_memory_is_not_exposed_by_compatibility_api(
    tmp_path: Path, monkeypatch
) -> None:
    _fresh_database(tmp_path, monkeypatch)
    marker = "sensitive-compatibility-boundary"
    create_memory(memory_type="semantic", content=marker, is_sensitive=True)

    with TestClient(app) as client:
        listed = client.get("/api/memories", params={"workspace": "", "namespace": "personal"})
        searched = client.get("/api/memories/search", params={"q": marker, "workspace": ""})

    assert listed.status_code == 200
    assert marker not in listed.text
    assert searched.status_code == 200
    assert all(marker not in str(item) for item in searched.json()["items"])


def test_deleted_long_term_memory_is_not_listed_or_exported_by_compatibility_api(
    tmp_path: Path, monkeypatch
) -> None:
    _fresh_database(tmp_path, monkeypatch)
    marker = "deleted-compatibility-tombstone"
    item = create_memory(memory_type="semantic", content=marker)
    delete_memory(
        item["id"],
        authorization=_authorization("memory.delete", item["id"], {}),
    )

    assert marker not in str(list_workspace_memories("", namespace="personal"))
    with TestClient(app) as client:
        listed = client.get("/api/memories", params={"workspace": "", "namespace": "personal"})
        exported = client.get(
            "/api/memories/export", params={"workspace": "", "namespace": "personal"}
        )

    assert listed.status_code == 200
    assert marker not in listed.text
    assert exported.status_code == 200
    assert marker not in exported.text


def test_scopes_are_persisted_and_isolated_by_authoritative_owner(tmp_path: Path, monkeypatch) -> None:
    _fresh_database(tmp_path, monkeypatch)
    conversation_id, task_id = _conversation_and_task(tmp_path)
    common = {"key": "same.key", "content": "same content", "verified": True}

    user = upsert_workspace_memory("", namespace="personal", source="user", **common)
    workspace = upsert_workspace_memory(str(tmp_path), **common)
    conversation = upsert_workspace_memory(
        str(tmp_path),
        scope_type="conversation",
        conversation_id=conversation_id,
        source_message_id="message-7",
        source_metadata={
            "origin": "chat",
            "api_key": "must-not-be-stored",
            "note": "api_key=must-not-be-stored-either",
        },
        **common,
    )
    task = upsert_workspace_memory(str(tmp_path), scope_type="task", task_id=task_id, **common)

    assert {user["scope_type"], workspace["scope_type"], conversation["scope_type"], task["scope_type"]} == {
        "user",
        "workspace",
        "conversation",
        "task",
    }
    assert len({user["id"], workspace["id"], conversation["id"], task["id"]}) == 4
    assert conversation["source_conversation_id"] == str(conversation_id)
    assert conversation["source_message_id"] == "message-7"
    assert conversation["source_metadata"] == {"note": "***REDACTED***", "origin": "chat"}
    assert [item["id"] for item in list_workspace_memories("", namespace="personal")] == [user["id"]]
    assert [item["id"] for item in list_workspace_memories(str(tmp_path))] == [workspace["id"]]
    assert [
        item["id"]
        for item in list_workspace_memories(
            str(tmp_path), scope_type="conversation", conversation_id=conversation_id
        )
    ] == [conversation["id"]]
    assert [
        item["id"]
        for item in list_workspace_memories(str(tmp_path), scope_type="task", task_id=task_id)
    ] == [task["id"]]

    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="不属于当前工作区"):
        list_workspace_memories(str(other), scope_type="conversation", conversation_id=conversation_id)


def test_scope_local_dedup_and_writes_are_audited(tmp_path: Path, monkeypatch) -> None:
    _fresh_database(tmp_path, monkeypatch)
    first = upsert_workspace_memory(
        str(tmp_path),
        key="decision.one",
        content="Use SQLite WAL",
        source="agent",
        source_task_id="task-source",
        source_metadata={"reason": "verified output"},
    )
    duplicate = upsert_workspace_memory(
        str(tmp_path),
        key="decision.two",
        content="  use sqlite wal  ",
        source="agent",
    )

    assert duplicate["id"] == first["id"]
    assert duplicate["key"] == "decision.one"
    actions = database.rows(
        "SELECT action,target,details FROM audit_logs WHERE action IN ('memory_upserted','memory_deduplicated') ORDER BY id"
    )
    assert [item["action"] for item in actions] == ["memory_upserted", "memory_deduplicated"]
    assert all(item["target"] == str(first["id"]) for item in actions)
    assert all("Use SQLite WAL" not in str(item["details"]) for item in actions)
