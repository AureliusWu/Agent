import sqlite3
from contextlib import closing
from pathlib import Path

from app import database as database_module
from app.database import init_db
from app.kernel.adapters import SqliteTaskStore


def _create_v40_stt_database(
    path: Path,
    *,
    windows_path: str,
    posix_path: str,
) -> None:
    """Create a faithful v40 database without changing process-wide settings."""

    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(database_module.SCHEMA)
        connection.execute(
            "INSERT INTO schema_migrations(version,applied_at) VALUES(1,'legacy')"
        )
        for version, migration in database_module.MIGRATIONS:
            if version > 40:
                break
            migration(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(?,'legacy')",
                (version,),
            )
        connection.execute(
            "INSERT INTO stt_models("
            "model_id,provider,status,size_bytes,storage_path,updated_at"
            ") VALUES(?,?,?,?,?,?)",
            ("small", "faster_whisper", "READY", 1, windows_path, "legacy"),
        )
        connection.execute(
            "INSERT INTO stt_download_records("
            "id,model_id,provider,status,confirmed,completed_bytes,total_bytes,"
            "target_directory,started_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "legacy-download",
                "base",
                "faster_whisper",
                "COMPLETED",
                1,
                1,
                1,
                posix_path,
                "legacy",
            ),
        )


def test_existing_database_is_migrated_to_current_schema(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE agent_tasks (
          id TEXT PRIMARY KEY, conversation_id INTEGER NOT NULL, status TEXT NOT NULL,
          prompt TEXT NOT NULL, termination_reason TEXT, model_calls INTEGER NOT NULL DEFAULT 0,
          tool_calls INTEGER NOT NULL DEFAULT 0, files_modified INTEGER NOT NULL DEFAULT 0,
          last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE tool_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL,
          tool TEXT NOT NULL, status TEXT NOT NULL, input TEXT, output TEXT,
          started_at TEXT NOT NULL, finished_at TEXT NOT NULL
        );
        """
    )
    connection.close()
    monkeypatch.setattr("app.database.settings.database_path", database)

    init_db()

    connection = sqlite3.connect(database)
    task_columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_tasks)")}
    conversation_columns = {row[1] for row in connection.execute("PRAGMA table_info(conversations)")}
    tool_columns = {row[1] for row in connection.execute("PRAGMA table_info(tool_runs)")}
    versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    message_columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    model_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='model_runs'").fetchone()
    model_columns = {row[1] for row in connection.execute("PRAGMA table_info(model_runs)")}
    verification_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_verifications'").fetchone()
    plan_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_plans'").fetchone()
    repair_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_repair_runs'").fetchone()
    checkpoint_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_checkpoints'").fetchone()
    operation_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_operations'").fetchone()
    working_memory_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_working_memory'").fetchone()
    data_flow_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='data_flow_events'").fetchone()
    snapshot_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='security_snapshots'").fetchone()
    agent_run_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_runs'").fetchone()
    agent_trace_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_trace_events'").fetchone()
    file_lock_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_file_locks'").fetchone()
    extension_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='extension_packages'").fetchone()
    provider_capability_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='provider_capabilities'").fetchone()
    task_event_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_events'").fetchone()
    task_context_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_context_states'").fetchone()
    decision_ledger_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='task_decision_ledger'").fetchone()
    title_jobs_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_title_jobs'").fetchone()
    memories_fts_table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'").fetchone()
    memories_fts_triggers = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'memories_fts_%'")}
    grant_columns = {row[1] for row in connection.execute("PRAGMA table_info(approval_grants)")}
    context_columns = {row[1] for row in connection.execute("PRAGMA table_info(conversation_context)")}
    memory_columns = {row[1] for row in connection.execute("PRAGMA table_info(workspace_memories)")}
    connection.close()

    assert {
        "total_tokens", "input_tokens", "output_tokens", "phase_tokens", "estimated_cost_usd", "model_route",
        "cache_hits", "cache_misses", "current_step", "completed_steps", "pending_steps", "started_at", "finished_at",
        "repair_attempts", "verification_attempts", "current_phase", "checkpoint_sequence", "resume_count", "resumable", "paused_at",
    } <= task_columns
    assert {"task_id", "source", "risk", "confirmed", "duration_ms", "execution_id"} <= tool_columns
    assert {"agent_profile_id", "agent_profile_snapshot"} <= task_columns
    assert "agent_profile_id" in conversation_columns
    assert {"title_source", "title_locked", "title_generated_at", "title_version", "title_input_hash"} <= conversation_columns
    assert versions == set(range(1, database_module.SCHEMA_VERSION + 1))
    assert {"task_id", "reasoning_content"} <= message_columns
    assert journal_mode == "wal"
    assert model_table is not None
    assert {"context_window_tokens", "reserved_output_tokens", "estimated_input_tokens", "input_estimate", "first_token_ms"} <= model_columns
    assert verification_table is not None
    assert plan_table is not None
    assert repair_table is not None
    assert checkpoint_table is not None
    assert operation_table is not None
    assert working_memory_table is not None
    assert data_flow_table is not None
    assert snapshot_table is not None
    assert agent_run_table is not None
    assert agent_trace_table is not None
    assert file_lock_table is not None
    assert extension_table is not None
    assert provider_capability_table is not None
    assert task_event_table is not None
    assert task_context_table is not None
    assert decision_ledger_table is not None
    assert title_jobs_table is not None
    assert memories_fts_table is not None
    assert memories_fts_triggers == {"memories_fts_insert", "memories_fts_update", "memories_fts_delete"}
    assert {"orchestration_mode", "child_agent_count"} <= task_columns
    assert {"workspace", "capabilities"} <= grant_columns
    assert "structured_state" in context_columns
    assert {"kind", "namespace", "category", "source", "tags", "applicable_version", "project_signature", "confidence", "last_verified_at", "use_count", "success_count", "failure_count", "rejected"} <= memory_columns
    assert list((tmp_path / "backups").glob(f"pre-migration-v0-to-v{database_module.SCHEMA_VERSION}-*.db"))


def test_v42_scrubs_plaintext_tts_idempotency_keys_from_database_and_wal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "agent.db"
    plaintext = (
        "BEGIN_PRIVATE_TTS_IDEMPOTENCY_"
        + "spoken-secret-" * 100
        + "_END"
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE tts_requests (request_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE)"
        )
        connection.execute(
            "INSERT INTO tts_requests(request_id,idempotency_key) VALUES(?,?)",
            ("request", plaintext),
        )
        connection.executemany(
            "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
            ((version, "legacy") for version in range(1, 42)),
        )

    assert plaintext.encode("utf-8") in database.read_bytes()
    monkeypatch.setattr(database_module.settings, "database_path", database)
    database_module.init_db()

    with closing(sqlite3.connect(database)) as connection:
        stored = connection.execute(
            "SELECT idempotency_key FROM tts_requests WHERE request_id='request'"
        ).fetchone()[0]

    assert stored == database_module.tts_idempotency_digest(plaintext)
    marker = plaintext.encode("utf-8")
    migration_backups = list(
        (tmp_path / "backups").glob("pre-migration-v41-to-v42-*.db")
    )
    assert len(migration_backups) == 1
    with closing(sqlite3.connect(migration_backups[0])) as backup:
        backup_value = backup.execute(
            "SELECT idempotency_key FROM tts_requests WHERE request_id='request'"
        ).fetchone()[0]
    assert backup_value == database_module.tts_idempotency_digest(plaintext)
    for candidate in (
        database,
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
        *migration_backups,
    ):
        if candidate.exists():
            assert marker not in candidate.read_bytes(), candidate.name


def test_v42_repairs_existing_plaintext_tts_backup_on_every_startup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "agent.db"
    monkeypatch.setattr(database_module.settings, "database_path", database)
    database_module.init_db()
    legacy = "LEGACY_TTS_BACKUP_PRIVATE_TEXT_" + "sensitive-voice-" * 80
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir(exist_ok=True)
    backup_path = backup_directory / "agent-legacy.db"
    with closing(sqlite3.connect(backup_path)) as backup, backup:
        backup.execute(
            "CREATE TABLE tts_requests (request_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE)"
        )
        backup.execute(
            "INSERT INTO tts_requests(request_id,idempotency_key) VALUES(?,?)",
            ("legacy-request", legacy),
        )

    assert legacy.encode("utf-8") in backup_path.read_bytes()
    database_module.init_db()

    with closing(sqlite3.connect(backup_path)) as backup:
        stored = backup.execute(
            "SELECT idempotency_key FROM tts_requests WHERE request_id='legacy-request'"
        ).fetchone()[0]
    assert stored == database_module.tts_idempotency_digest(legacy)
    assert legacy.encode("utf-8") not in backup_path.read_bytes()


def test_v41_scrubs_stt_paths_from_migration_backup_bytes_and_restores(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "agent.db"
    windows_path = (
        chr(67) + r":\Users\private-user\AppData\Local\AureliusWu\Agent\voice\models\small"
        + "\\WINDOWS_PRIVATE_PATH_"
        + "secret-segment-" * 80
    )
    posix_path = (
        "/home/private-user/.local/share/aureliuswu/agent/voice/models/base/"
        + "POSIX_PRIVATE_PATH_"
        + "secret-segment-" * 80
    )
    _create_v40_stt_database(
        database,
        windows_path=windows_path,
        posix_path=posix_path,
    )
    assert windows_path.encode("utf-8") in database.read_bytes()
    assert posix_path.encode("utf-8") in database.read_bytes()
    monkeypatch.setattr(database_module.settings, "database_path", database)

    database_module.init_db()

    migration_backups = list(
        (tmp_path / "backups").glob("pre-migration-v40-to-v42-*.db")
    )
    assert len(migration_backups) == 1
    for candidate in (database, migration_backups[0]):
        with closing(sqlite3.connect(candidate)) as connection:
            assert connection.execute(
                "SELECT storage_path FROM stt_models WHERE model_id='small'"
            ).fetchone()[0] == "managed:small"
            assert connection.execute(
                "SELECT target_directory FROM stt_download_records "
                "WHERE id='legacy-download'"
            ).fetchone()[0] == "managed:base"
        raw = candidate.read_bytes()
        assert windows_path.encode("utf-8") not in raw
        assert posix_path.encode("utf-8") not in raw

    with closing(sqlite3.connect(migration_backups[0])) as connection:
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 40

    restored = database_module.restore_database(migration_backups[0].name)
    assert restored["restored"] == migration_backups[0].name
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == database_module.SCHEMA_VERSION
        assert connection.execute(
            "SELECT storage_path FROM stt_models WHERE model_id='small'"
        ).fetchone()[0] == "managed:small"
        assert connection.execute(
            "SELECT target_directory FROM stt_download_records "
            "WHERE id='legacy-download'"
        ).fetchone()[0] == "managed:base"


def test_startup_scrubs_legacy_stt_paths_from_user_backup_and_restores(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "agent.db"
    monkeypatch.setattr(database_module.settings, "database_path", database)
    database_module.init_db()

    windows_path = (
        chr(68) + r":\Private\Speech\Models\small"
        + "\\USER_BACKUP_WINDOWS_PATH_"
        + "sensitive-segment-" * 80
    )
    posix_path = (
        "/srv/private/speech/models/base/"
        + "USER_BACKUP_POSIX_PATH_"
        + "sensitive-segment-" * 80
    )
    legacy_source = tmp_path / "legacy-v40.db"
    _create_v40_stt_database(
        legacy_source,
        windows_path=windows_path,
        posix_path=posix_path,
    )
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir(exist_ok=True)
    backup_path = backup_directory / "agent-legacy-stt.db"
    with closing(sqlite3.connect(legacy_source)) as source, closing(
        sqlite3.connect(backup_path)
    ) as destination:
        source.backup(destination)
    legacy_source.unlink()
    assert windows_path.encode("utf-8") in backup_path.read_bytes()
    assert posix_path.encode("utf-8") in backup_path.read_bytes()

    database_module.init_db()

    with closing(sqlite3.connect(backup_path)) as backup:
        assert backup.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 40
        assert backup.execute(
            "SELECT storage_path FROM stt_models WHERE model_id='small'"
        ).fetchone()[0] == "managed:small"
        assert backup.execute(
            "SELECT target_directory FROM stt_download_records "
            "WHERE id='legacy-download'"
        ).fetchone()[0] == "managed:base"
    raw = backup_path.read_bytes()
    assert windows_path.encode("utf-8") not in raw
    assert posix_path.encode("utf-8") not in raw

    restored = database_module.restore_database(backup_path.name)
    assert restored["restored"] == backup_path.name
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == database_module.SCHEMA_VERSION
        assert connection.execute(
            "SELECT storage_path FROM stt_models WHERE model_id='small'"
        ).fetchone()[0] == "managed:small"
        assert connection.execute(
            "SELECT target_directory FROM stt_download_records "
            "WHERE id='legacy-download'"
        ).fetchone()[0] == "managed:base"


def test_new_install_defaults_stt_to_small_without_overwriting_existing_base(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "stt-default.db"
    monkeypatch.setattr(database_module.settings, "database_path", database)

    init_db()

    with closing(sqlite3.connect(database)) as connection, connection:
        new_install_model = connection.execute(
            "SELECT model_id FROM stt_settings WHERE singleton=1"
        ).fetchone()[0]
        column_default = {
            row[1]: row[4] for row in connection.execute("PRAGMA table_info(stt_settings)")
        }["model_id"]
        connection.execute(
            "UPDATE stt_settings SET model_id='base' WHERE singleton=1"
        )

    # Re-running initialization represents an existing installation.  Its
    # stored choice is authoritative, even when that choice is the old base
    # default, because the application cannot infer whether it was explicit.
    init_db()

    with closing(sqlite3.connect(database)) as connection:
        existing_model = connection.execute(
            "SELECT model_id FROM stt_settings WHERE singleton=1"
        ).fetchone()[0]

    assert new_install_model == "small"
    assert column_default == "'small'"
    assert existing_model == "base"


def test_pending_legacy_task_is_backfilled_into_persistent_queue(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "pending.db"
    monkeypatch.setattr("app.database.settings.database_path", database)
    init_db()
    with closing(sqlite3.connect(database)) as connection, connection:
        stamp = database_module.now_iso()
        connection.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("legacy", str(tmp_path), "ask", stamp, stamp),
        )
        conversation_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("a" * 32, conversation_id, "pending", "resume after upgrade", stamp, stamp),
        )

    init_db()

    with closing(sqlite3.connect(database)) as connection, connection:
        queued = connection.execute(
            "SELECT task_id,status,payload_json FROM conversation_queue_items WHERE task_id=?",
            ("a" * 32,),
        ).fetchone()
    assert queued is not None
    assert queued[0:2] == ("a" * 32, "pending")
    assert '"content": "resume after upgrade"' in queued[2]


def test_migrated_partial_execution_index_supports_tool_run_upsert(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "legacy-tool-runs.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE conversations (
              id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
              workspace TEXT NOT NULL, permission_mode TEXT NOT NULL DEFAULT 'confirm',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO conversations(title, workspace, created_at, updated_at)
              VALUES('legacy', 'C' || ':/repo', 'now', 'now');
            CREATE TABLE agent_tasks (
              id TEXT PRIMARY KEY, conversation_id INTEGER NOT NULL, status TEXT NOT NULL,
              prompt TEXT NOT NULL, termination_reason TEXT, model_calls INTEGER NOT NULL DEFAULT 0,
              tool_calls INTEGER NOT NULL DEFAULT 0, files_modified INTEGER NOT NULL DEFAULT 0,
              last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at)
              VALUES('task-1', 1, 'running', 'test', 'now', 'now');
            CREATE TABLE tool_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL,
              tool TEXT NOT NULL, status TEXT NOT NULL, input TEXT, output TEXT,
              started_at TEXT NOT NULL, finished_at TEXT NOT NULL
            );
            """
        )
    monkeypatch.setattr("app.database.settings.database_path", database)
    init_db()

    store = SqliteTaskStore()
    values = dict(
        conversation_id=1, task_id="task-1", tool="read_file", arguments={"path": "README.md"},
        result={"status": "ok", "success": True}, started="now", started_perf=0.0,
        risk="read", confirmed=False, execution_id="execution-1",
    )
    store.record_tool_run(**values)
    store.record_tool_run(**values)

    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM tool_runs WHERE execution_id='execution-1'").fetchone()[0] == 1


def test_failed_migration_restores_automatic_backup(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "failed-migration.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations(version, applied_at) VALUES(1, 'before');
            CREATE TABLE sentinel(value TEXT NOT NULL);
            INSERT INTO sentinel(value) VALUES('preserved');
            """
        )

    def fail_migration(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE partial_change(value TEXT)")
        raise RuntimeError("migration failed")

    monkeypatch.setattr(database_module.settings, "database_path", database)
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(database_module, "MIGRATIONS", ((2, fail_migration),))

    try:
        init_db()
    except RuntimeError as exc:
        assert str(exc) == "migration failed"
    else:
        raise AssertionError("migration failure was not propagated")

    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == "preserved"
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='partial_change'").fetchone() is None
    assert list((tmp_path / "backups").glob("pre-migration-v1-to-v2-*.db"))


def test_v14_migrates_legacy_memories_into_project_categories(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "memory-v13.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE workspace_memories (
          id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL, key TEXT NOT NULL,
          content TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'project', source TEXT NOT NULL DEFAULT 'user',
          source_task_id TEXT, tags TEXT NOT NULL DEFAULT '[]', applicable_version TEXT,
          project_signature TEXT NOT NULL DEFAULT '{}', confidence REAL NOT NULL DEFAULT 0.7,
          last_verified_at TEXT, last_used_at TEXT, use_count INTEGER NOT NULL DEFAULT 0,
          success_count INTEGER NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0,
          rejected INTEGER NOT NULL DEFAULT 0, invalidated_reason TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(workspace, key)
        );
        INSERT INTO workspace_memories(workspace,key,content,kind,source,created_at,updated_at)
          VALUES('C' || ':/repo','build.command','npm run build','project','user','now','now');
        """
    )
    connection.executemany("INSERT INTO schema_migrations(version, applied_at) VALUES(?, 'now')", [(version,) for version in range(1, 14)])
    connection.commit()
    connection.close()
    monkeypatch.setattr("app.database.settings.database_path", database)

    init_db()

    connection = sqlite3.connect(database)
    memory = connection.execute("SELECT namespace, category, key FROM workspace_memories").fetchone()
    unique_columns = [
        tuple(row[2] for row in connection.execute(f"PRAGMA index_info('{index[1]}')"))
        for index in connection.execute("PRAGMA index_list('workspace_memories')")
        if index[2]
    ]
    connection.close()
    assert memory == ("project", "build_command", "build.command")
    assert ("workspace", "namespace", "key") in unique_columns
