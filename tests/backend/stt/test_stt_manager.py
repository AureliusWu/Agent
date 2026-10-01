from __future__ import annotations

import asyncio
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from app import database as database_module
from app.database import init_db, rows
from app.local_runtime.resource_coordinator import ResourceCoordinator, resource_pressure_details
from app.stt.manager import STTManager
from app.stt.providers.faster_whisper import FasterWhisperProvider
from app.stt.schemas import DEFAULT_STT_MODEL_ID, STT_STATUS_VALUES, STTError, TranscriptionRequest
from app.stt.temporary_storage import TemporaryAudioStore


def _inject_resource_headroom(monkeypatch) -> None:
    """Keep child-process tests independent from the developer machine load.

    Production admission reads real telemetry.  These tests exercise worker
    cancellation, not machine capacity, so they supply a deterministic
    16 GB / 6 GB snapshot before a child could be started.
    """
    monkeypatch.setattr(
        "app.local_runtime.resource_coordinator._memory",
        lambda: (16 * 1024 * 1024 * 1024, 8 * 1024 * 1024 * 1024),
    )
    monkeypatch.setattr(
        "app.local_runtime.resource_coordinator._gpu",
        lambda: (6 * 1024 * 1024 * 1024, 4 * 1024 * 1024 * 1024),
    )


def test_stt_model_download_requires_explicit_confirmation_before_any_download(tmp_path: Path) -> None:
    manager = STTManager(audio_store=TemporaryAudioStore(tmp_path / "audio"))
    manager.models_root = tmp_path / "models"
    manager.models_root.mkdir()

    with pytest.raises(STTError) as raised:
        asyncio.run(manager.start_download("base", confirmed=False))

    assert raised.value.code == "STT_DOWNLOAD_CONFIRMATION_REQUIRED"
    assert not manager.model_path("base").exists()
    assert manager._downloads == {}
    assert manager._download_tasks == {}


def test_stt_small_default_does_not_implicitly_download_missing_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "stt-small-default.db"
    monkeypatch.setattr("app.database.settings.database_path", database)
    init_db()
    manager = STTManager(audio_store=TemporaryAudioStore(tmp_path / "audio"))
    manager.models_root = tmp_path / "models"
    manager.models_root.mkdir()

    assert DEFAULT_STT_MODEL_ID == "small"
    assert manager.settings()["model_id"] == "small"
    assert TranscriptionRequest(
        request_id="request",
        voice_session_id="session",
        audio_path="recording.wav",
        audio_sha256="0" * 64,
        audio_duration_ms=1000,
    ).model_id == "small"
    assert manager.model_info("small")["default_for_new_install"] is True
    assert manager.model_info("base")["default_for_new_install"] is False

    with pytest.raises(STTError) as missing:
        asyncio.run(manager.load_model())
    assert missing.value.code == "STT_MODEL_MISSING"
    assert manager._downloads == {}
    assert manager._download_tasks == {}
    assert not manager.model_path("small").exists()

    with pytest.raises(STTError) as unconfirmed:
        asyncio.run(manager.start_download("small", confirmed=False))
    assert unconfirmed.value.code == "STT_DOWNLOAD_CONFIRMATION_REQUIRED"
    assert manager._downloads == {}
    assert manager._download_tasks == {}
    assert not manager.model_path("small").exists()


def test_stt_status_uses_only_the_documented_lifecycle_contract(tmp_path: Path, monkeypatch) -> None:
    manager = STTManager(audio_store=TemporaryAudioStore(tmp_path / "audio"))
    manager.models_root = tmp_path / "models"
    manager.models_root.mkdir()
    configured = {
        "enabled": False,
        "provider": "faster_whisper",
        "model_id": "base",
        "device": "cpu",
        "compute_type": "int8",
        "vad": True,
        "idle_unload_minutes": 0,
        "gpu_experimental": False,
    }
    monkeypatch.setattr(manager, "settings", lambda: dict(configured))

    observed = {manager.status()["status"]}
    assert manager.status()["status"] == "NOT_CONFIGURED"
    assert manager.status()["allowed_statuses"] == sorted(STT_STATUS_VALUES)

    configured["enabled"] = True
    observed.add(manager.status()["status"])
    assert manager.status()["status"] == "MODEL_MISSING"

    model_root = manager.model_path("base")
    model_root.mkdir(parents=True)
    (model_root / "config.json").write_text("{}", encoding="utf-8")
    observed.add(manager.status()["status"])
    assert manager.status()["status"] == "READY"

    class _PendingDownload:
        @staticmethod
        def done() -> bool:
            return False

    manager._downloads["base"] = {"status": "DOWNLOADING"}
    manager._download_tasks["base"] = _PendingDownload()  # type: ignore[assignment]
    observed.add(manager.status()["status"])
    assert manager.status()["status"] == "DOWNLOADING"
    manager._downloads.clear()
    manager._download_tasks.clear()

    manager._starting_worker = object()  # type: ignore[assignment]
    observed.add(manager.status()["status"])
    assert manager.status()["status"] == "LOADING"
    manager._starting_worker = None

    manager._unloading_model_id = "base"
    observed.add(manager.status()["status"])
    assert manager.status()["status"] == "UNLOADING"
    manager._unloading_model_id = None

    manager._active["request"] = None
    observed.add(manager.status()["status"])
    assert manager.status()["status"] == "TRANSCRIBING"
    manager._cancelled_requests.add("request")
    observed.add(manager.status()["status"])
    assert manager.status()["status"] == "CANCEL_REQUESTED"

    assert observed <= STT_STATUS_VALUES
    assert "IDLE" not in observed


def test_stt_model_delete_verifies_the_directory_is_really_gone(tmp_path: Path, monkeypatch) -> None:
    manager = STTManager(audio_store=TemporaryAudioStore(tmp_path / "audio"))
    manager.models_root = tmp_path / "models"
    model_root = manager.model_path("base")
    model_root.mkdir(parents=True)
    (model_root / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("app.stt.manager.shutil.rmtree", lambda _path: None)

    with pytest.raises(STTError) as raised:
        asyncio.run(manager.delete_model("base", confirmed=True))

    assert raised.value.code == "STT_MODEL_DELETE_FAILED"
    assert model_root.exists()


def test_stt_model_delete_reports_success_only_after_real_removal(tmp_path: Path) -> None:
    manager = STTManager(audio_store=TemporaryAudioStore(tmp_path / "audio"))
    manager.models_root = tmp_path / "models"
    model_root = manager.model_path("base")
    model_root.mkdir(parents=True)
    (model_root / "config.json").write_text("{}", encoding="utf-8")

    result = asyncio.run(manager.delete_model("base", confirmed=True))

    assert result == {"model": "base", "status": "DELETED"}
    assert not model_root.exists()


def test_stt_model_metadata_persists_only_a_logical_managed_location(tmp_path: Path, monkeypatch) -> None:
    """The UI may receive a local path, but SQLite metadata must not retain it."""
    database = tmp_path / "stt-metadata.db"
    monkeypatch.setattr("app.database.settings.database_path", database)
    init_db()
    manager = STTManager(audio_store=TemporaryAudioStore(tmp_path / "audio"))
    manager.models_root = tmp_path / "models"
    manager.models_root.mkdir()

    info = manager.model_info("base")
    manager._persist_model(info)

    with sqlite3.connect(database) as connection:
        persisted = connection.execute(
            "SELECT storage_path FROM stt_models WHERE model_id='base'"
        ).fetchone()[0]

    # ``storage_path`` remains part of the direct local UI/API response so the
    # delete confirmation can name the actual directory.  Its persistent
    # counterpart is deliberately a logical managed-model reference.
    assert info["storage_path"] == str(manager.model_path("base"))
    assert persisted == "managed:base"


def test_stt_download_record_persists_only_a_logical_managed_location(tmp_path: Path, monkeypatch) -> None:
    """Starting a confirmed download must not write its target directory to SQLite."""
    database = tmp_path / "stt-download.db"
    monkeypatch.setattr("app.database.settings.database_path", database)
    init_db()
    manager = STTManager(audio_store=TemporaryAudioStore(tmp_path / "audio"))
    manager.models_root = tmp_path / "models"
    manager.models_root.mkdir()

    async def no_network_download(_model_id: str) -> None:
        return None

    monkeypatch.setattr(FasterWhisperProvider, "package_available", staticmethod(lambda: True))
    monkeypatch.setattr("app.stt.manager.importlib.util.find_spec", lambda _name: object())
    monkeypatch.setattr(manager, "_download", no_network_download)
    asyncio.run(manager.start_download("base", confirmed=True))

    with sqlite3.connect(database) as connection:
        persisted = connection.execute(
            "SELECT target_directory FROM stt_download_records WHERE model_id='base'"
        ).fetchone()[0]

    assert persisted == "managed:base"


class _CompletedDownloadOutput:
    def __init__(self, before_read=None) -> None:
        self.before_read = before_read

    async def read(self) -> bytes:
        if self.before_read is not None:
            self.before_read()
        return b'{"status":"ok"}'


class _CompletedDownloadProcess:
    def __init__(self, pid: int, *, before_read=None) -> None:
        self.pid = pid
        self.returncode = 0
        self.stdout = _CompletedDownloadOutput(before_read)


class _RunningDownloadProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = _CompletedDownloadOutput()

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode


class _DelayedExitDownloadProcess:
    """Fake a child that exits only after an asynchronous reap delay."""

    def __init__(self, pid: int, events: list[str]) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = _CompletedDownloadOutput()
        self.events = events

    def terminate(self) -> None:
        self.events.append("terminate")

    def kill(self) -> None:
        self.events.append("kill")
        self.returncode = -9

    async def wait(self) -> int:
        self.events.append("wait_started")
        await asyncio.sleep(0.02)
        if self.returncode is None:
            self.returncode = -15
        self.events.append("waited")
        return self.returncode


def _prepare_download_manager(tmp_path: Path, monkeypatch) -> STTManager:
    manager = STTManager(audio_store=TemporaryAudioStore(tmp_path / "audio"))
    manager.models_root = tmp_path / "models"
    manager.models_root.mkdir()
    monkeypatch.setattr(manager, "_finish_download", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(manager, "_persist_model", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.stt.manager.register_process", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.stt.manager.unregister_process", lambda *_args, **_kwargs: None)
    return manager


def test_stt_download_uses_record_owned_unique_staging_and_preserves_residue(tmp_path: Path, monkeypatch) -> None:
    manager = _prepare_download_manager(tmp_path, monkeypatch)
    stale_record = "0" * 32
    stale = manager._download_temporary_path("base", stale_record)
    stale.mkdir()
    (stale / "marker.txt").write_text("not this download", encoding="utf-8")
    worker_targets: list[Path] = []

    async def completed_worker(*arguments, **_kwargs):
        target = Path(arguments[arguments.index("--target") + 1])
        worker_targets.append(target)
        (target / "config.json").write_text("{}", encoding="utf-8")
        return _CompletedDownloadProcess(4100 + len(worker_targets))

    monkeypatch.setattr("app.stt.manager.asyncio.create_subprocess_exec", completed_worker)

    async def scenario() -> None:
        for record_id in ("1" * 32, "2" * 32):
            manager._downloads["base"] = {
                "model": "base",
                "status": "DOWNLOADING",
                "completed": 0,
                "total": 1,
                "error": None,
                "record_id": record_id,
                "cancel_requested": False,
            }
            await manager._download("base")
            assert manager.download_state("base")["status"] == "INSTALLED"
            shutil.rmtree(manager._download_target_path("base"))

    asyncio.run(scenario())

    assert worker_targets == [
        manager._download_temporary_path("base", "1" * 32),
        manager._download_temporary_path("base", "2" * 32),
    ]
    assert worker_targets[0] != worker_targets[1]
    assert stale.is_dir()
    assert (stale / "marker.txt").read_text(encoding="utf-8") == "not this download"


def test_stt_download_target_race_fails_closed_without_deleting_marker(tmp_path: Path, monkeypatch) -> None:
    manager = _prepare_download_manager(tmp_path, monkeypatch)
    record_id = "3" * 32
    manager._downloads["small"] = {
        "model": "small",
        "status": "DOWNLOADING",
        "completed": 0,
        "total": 1,
        "error": None,
        "record_id": record_id,
        "cancel_requested": False,
    }
    target = manager._download_target_path("small")

    async def racing_worker(*arguments, **_kwargs):
        temporary = Path(arguments[arguments.index("--target") + 1])
        (temporary / "config.json").write_text("{}", encoding="utf-8")

        def create_competing_target() -> None:
            target.mkdir()
            (target / "marker.txt").write_text("belongs to another actor", encoding="utf-8")

        return _CompletedDownloadProcess(4200, before_read=create_competing_target)

    monkeypatch.setattr("app.stt.manager.asyncio.create_subprocess_exec", racing_worker)

    asyncio.run(manager._download("small"))

    assert manager.download_state("small")["status"] == "ERROR"
    assert manager.download_state("small")["error"] == "STT_DOWNLOAD_FAILED"
    assert (target / "marker.txt").read_text(encoding="utf-8") == "belongs to another actor"
    assert not manager._download_temporary_path("small", record_id).exists()


def test_stt_download_cancel_removes_only_its_owned_staging_directory(tmp_path: Path, monkeypatch) -> None:
    manager = _prepare_download_manager(tmp_path, monkeypatch)
    owned_record = "4" * 32
    other_record = "5" * 32
    other = manager._download_temporary_path("base", other_record)
    other.mkdir()
    (other / "marker.txt").write_text("preserve", encoding="utf-8")
    manager._downloads["base"] = {
        "model": "base",
        "status": "DOWNLOADING",
        "completed": 0,
        "total": 1,
        "error": None,
        "record_id": owned_record,
        "cancel_requested": False,
    }
    process = _RunningDownloadProcess(4300)

    async def running_worker(*_args, **_kwargs):
        return process

    monkeypatch.setattr("app.stt.manager.asyncio.create_subprocess_exec", running_worker)

    async def scenario() -> None:
        task = asyncio.create_task(manager._download("base"))
        manager._download_tasks["base"] = task
        for _ in range(100):
            if "base" in manager._download_processes:
                break
            await asyncio.sleep(0.01)
        assert "base" in manager._download_processes
        assert (manager._download_temporary_path("base", owned_record)).is_dir()
        assert (await manager.cancel_download("base"))["status"] == "CANCEL_REQUESTED"
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())

    assert manager.download_state("base")["status"] == "CANCELLED"
    assert not manager._download_temporary_path("base", owned_record).exists()
    assert (other / "marker.txt").read_text(encoding="utf-8") == "preserve"


def test_stt_download_exception_reaps_delayed_owned_child_before_staging_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _prepare_download_manager(tmp_path, monkeypatch)
    record_id = "7" * 32
    manager._downloads["base"] = {
        "model": "base",
        "status": "DOWNLOADING",
        "completed": 0,
        "total": 1,
        "error": None,
        "record_id": record_id,
        "cancel_requested": False,
    }
    events: list[str] = []
    process = _DelayedExitDownloadProcess(4500, events)

    async def delayed_worker(*_args, **_kwargs):
        return process

    def fail_progress_read(_path: Path) -> int:
        events.append("progress_error")
        raise RuntimeError("synthetic progress failure")

    original_cleanup = manager._cleanup_download_directory

    def observed_cleanup(path: Path, *, model_id: str, record_id: str) -> bool:
        assert process.returncode is not None
        events.append("cleanup")
        return original_cleanup(path, model_id=model_id, record_id=record_id)

    monkeypatch.setattr("app.stt.manager.asyncio.create_subprocess_exec", delayed_worker)
    monkeypatch.setattr(manager, "_directory_size", fail_progress_read)
    monkeypatch.setattr(manager, "_cleanup_download_directory", observed_cleanup)
    monkeypatch.setattr(
        "app.stt.manager.unregister_process",
        lambda pid, _reason="exited": events.append(f"unregister:{pid}"),
    )

    asyncio.run(manager._download("base"))

    assert manager.download_state("base")["status"] == "ERROR"
    assert manager.download_state("base")["error"] == "STT_DOWNLOAD_FAILED"
    assert events.index("terminate") < events.index("waited") < events.index("cleanup")
    assert events.index("cleanup") < events.index("unregister:4500")
    assert not manager._download_temporary_path("base", record_id).exists()
    assert "base" not in manager._download_processes
    assert process not in manager._owned_download_processes


def test_stt_download_task_cancel_reaps_delayed_owned_child_before_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _prepare_download_manager(tmp_path, monkeypatch)
    record_id = "8" * 32
    manager._downloads["small"] = {
        "model": "small",
        "status": "DOWNLOADING",
        "completed": 0,
        "total": 1,
        "error": None,
        "record_id": record_id,
        "cancel_requested": False,
    }
    events: list[str] = []
    process = _DelayedExitDownloadProcess(4600, events)

    async def delayed_worker(*_args, **_kwargs):
        return process

    original_cleanup = manager._cleanup_download_directory

    def observed_cleanup(path: Path, *, model_id: str, record_id: str) -> bool:
        assert process.returncode is not None
        events.append("cleanup")
        return original_cleanup(path, model_id=model_id, record_id=record_id)

    monkeypatch.setattr("app.stt.manager.asyncio.create_subprocess_exec", delayed_worker)
    monkeypatch.setattr(manager, "_cleanup_download_directory", observed_cleanup)
    monkeypatch.setattr(
        "app.stt.manager.unregister_process",
        lambda pid, _reason="exited": events.append(f"unregister:{pid}"),
    )

    async def scenario() -> None:
        task = asyncio.create_task(manager._download("small"))
        for _ in range(100):
            if manager._download_processes.get("small") is process:
                break
            await asyncio.sleep(0.01)
        assert manager._download_processes.get("small") is process
        task.cancel()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())

    assert manager.download_state("small")["status"] == "CANCELLED"
    assert events.index("terminate") < events.index("waited") < events.index("cleanup")
    assert events.index("cleanup") < events.index("unregister:4600")
    assert not manager._download_temporary_path("small", record_id).exists()
    assert "small" not in manager._download_processes
    assert process not in manager._owned_download_processes


def test_stt_download_termination_never_signals_unknown_process(tmp_path: Path, monkeypatch) -> None:
    manager = _prepare_download_manager(tmp_path, monkeypatch)
    events: list[str] = []
    unknown = _DelayedExitDownloadProcess(4700, events)
    manager._download_processes["base"] = unknown

    assert asyncio.run(manager._terminate_download_process("base", reason="synthetic")) is False
    assert events == []
    assert unknown.returncode is None


def test_stt_download_normal_success_atomically_installs_model(tmp_path: Path, monkeypatch) -> None:
    manager = _prepare_download_manager(tmp_path, monkeypatch)
    record_id = "6" * 32
    manager._downloads["small"] = {
        "model": "small",
        "status": "DOWNLOADING",
        "completed": 0,
        "total": 1,
        "error": None,
        "record_id": record_id,
        "cancel_requested": False,
    }

    async def completed_worker(*arguments, **_kwargs):
        temporary = Path(arguments[arguments.index("--target") + 1])
        (temporary / "config.json").write_text("{}", encoding="utf-8")
        (temporary / "model.bin").write_bytes(b"weights")
        return _CompletedDownloadProcess(4400)

    monkeypatch.setattr("app.stt.manager.asyncio.create_subprocess_exec", completed_worker)

    asyncio.run(manager._download("small"))

    target = manager._download_target_path("small")
    assert manager.download_state("small")["status"] == "INSTALLED"
    assert (target / "config.json").is_file()
    assert (target / "model.bin").read_bytes() == b"weights"
    assert not manager._download_temporary_path("small", record_id).exists()


def test_stt_path_migration_scrubs_legacy_windows_and_posix_locations(tmp_path: Path, monkeypatch) -> None:
    """The next schema migration must remove already-persisted local paths."""
    database = tmp_path / "stt-legacy-paths.db"
    monkeypatch.setattr("app.database.settings.database_path", database)
    current_schema_version = database_module.SCHEMA_VERSION
    current_migrations = database_module.MIGRATIONS
    # Build an actual v40 database first, then upgrade it using the current
    # migration list.  This keeps the test on the same backup-and-migrate path
    # used for an installed application's existing SQLite file.
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 40)
    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        tuple(item for item in current_migrations if item[0] <= 40),
    )
    init_db()
    stamp = database_module.now_iso()
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO stt_models(model_id,provider,status,size_bytes,storage_path,device,compute_type,loaded_at,last_used_at,error_code,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("base", "faster_whisper", "INSTALLED", 1, r"C:\\Users\\private\\voice\\models\\base", "cpu", "int8", None, None, None, stamp),
                ("small", "faster_whisper", "INSTALLED", 1, "/home/private/voice/models/small", "cpu", "int8", None, None, None, stamp),
            ],
        )
        connection.executemany(
            "INSERT INTO stt_download_records(id,model_id,provider,status,confirmed,completed_bytes,total_bytes,target_directory,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
            [
                ("download-base", "base", "faster_whisper", "INSTALLED", 1, 1, 1, r"C:\\Users\\private\\voice\\models\\base", stamp),
                ("download-small", "small", "faster_whisper", "INSTALLED", 1, 1, 1, "/home/private/voice/models/small", stamp),
            ],
        )

    monkeypatch.setattr(database_module, "SCHEMA_VERSION", current_schema_version)
    monkeypatch.setattr(database_module, "MIGRATIONS", current_migrations)
    init_db()

    with sqlite3.connect(database) as connection:
        model_locations = dict(connection.execute("SELECT model_id,storage_path FROM stt_models"))
        download_locations = dict(connection.execute("SELECT model_id,target_directory FROM stt_download_records"))

    assert model_locations == {"base": "managed:base", "small": "managed:small"}
    assert download_locations == {"base": "managed:base", "small": "managed:small"}


def test_stt_settings_preserve_explicit_zero_idle_unload_minutes(tmp_path: Path) -> None:
    manager = STTManager(audio_store=TemporaryAudioStore(tmp_path / "audio"))

    updated = manager.update_settings(
        {
            "enabled": True,
            "provider": "faster_whisper",
            "model_id": "base",
            "device": "cpu",
            "compute_type": "int8",
            "vad": True,
            "idle_unload_minutes": 0,
            "gpu_experimental": False,
        }
    )

    assert updated["idle_unload_minutes"] == 0
    assert manager.settings()["idle_unload_minutes"] == 0


def test_stt_cancel_during_model_load_marks_the_real_request_cancelled(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> str:
        store = TemporaryAudioStore(tmp_path / "voice")
        manager = STTManager(audio_store=store)
        manager.models_root = tmp_path / "models"
        manager.models_root.mkdir()
        monkeypatch.setattr(
            manager,
            "settings",
            lambda: {
                "enabled": True,
                "provider": "faster_whisper",
                "model_id": "base",
                "device": "cpu",
                "compute_type": "int8",
                "vad": True,
                "idle_unload_minutes": 0,
                "gpu_experimental": False,
            },
        )
        load_started = asyncio.Event()
        release_load = asyncio.Event()

        async def slow_load(_model_id: str | None = None) -> dict[str, object]:
            load_started.set()
            await release_load.wait()
            return {"status": "READY"}

        monkeypatch.setattr(manager, "load_model", slow_load)
        session_id = "c" * 32
        audio_path = store.root / session_id / "recording.wav"
        audio_path.parent.mkdir(parents=True)
        audio_path.write_bytes(b"RIFF" + b"\x00" * 128)
        running = asyncio.create_task(
            manager.transcribe(
                voice_session_id=session_id,
                audio_path=audio_path,
                audio_sha256="a" * 64,
                audio_duration_ms=400,
            )
        )
        await asyncio.wait_for(load_started.wait(), timeout=2)
        cancel_started = asyncio.get_running_loop().time()
        cancelled = await manager.cancel(voice_session_id=session_id)
        assert asyncio.get_running_loop().time() - cancel_started <= 0.5
        assert cancelled == {"status": "CANCELLED", "cancelled": 1, "settled": True}
        release_load.set()
        with pytest.raises(STTError) as raised:
            await running
        assert raised.value.code == "STT_ALREADY_CANCELLED"
        return session_id

    session_id = asyncio.run(scenario())

    request = rows("SELECT status,error_code FROM stt_requests WHERE voice_session_id=? ORDER BY created_at DESC LIMIT 1", (session_id,))[0]
    assert request == {"status": "CANCELLED", "error_code": "STT_ALREADY_CANCELLED"}


def test_stt_cancel_reclaims_delayed_ready_child_and_allows_next_transcription(tmp_path: Path, monkeypatch) -> None:
    """A real child blocked before ready must not survive a voice cancel.

    This uses small Python worker scripts instead of a mocked subprocess: the
    first child deliberately waits thirty seconds before its ready line, while
    the second accepts a real pipe request.  It exercises the precise window
    where native Faster-Whisper model loading is slow but cancellation must
    still reclaim the child immediately.
    """
    _inject_resource_headroom(monkeypatch)
    delayed_worker = tmp_path / "delayed-worker.py"
    delayed_worker.write_text(
        "import time\n"
        "time.sleep(30)\n"
        "print('{\\\"event\\\":\\\"ready\\\",\\\"status\\\":\\\"ok\\\"}', flush=True)\n",
        encoding="utf-8",
    )
    ready_worker = tmp_path / "ready-worker.py"
    ready_worker.write_text(
        """import json
import sys

print(json.dumps({"event": "ready", "status": "ok"}), flush=True)
for line in sys.stdin:
    payload = json.loads(line)
    if payload.get("operation") == "shutdown":
        print(json.dumps({"event": "shutdown", "status": "ok"}), flush=True)
        break
    print(json.dumps({
        "event": "result",
        "status": "ok",
        "request_id": payload["request_id"],
        "result": {
            "request_id": payload["request_id"],
            "provider": "faster_whisper",
            "model": "base",
            "language": "zh",
            "text": "后续可用",
            "segments": [],
            "duration_ms": payload["audio_duration_ms"],
            "transcription_ms": 1.0,
            "real_time_factor": 0.0025,
        },
    }), flush=True)
""",
        encoding="utf-8",
    )

    async def scenario() -> None:
        store = TemporaryAudioStore(tmp_path / "audio")
        manager = STTManager(audio_store=store)
        manager.models_root = tmp_path / "models"
        model_root = manager.model_path("base")
        model_root.mkdir(parents=True)
        (model_root / "config.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            manager,
            "settings",
            lambda: {
                "enabled": True,
                "provider": "faster_whisper",
                "model_id": "base",
                "device": "cpu",
                "compute_type": "int8",
                "vad": True,
                "idle_unload_minutes": 0,
                "gpu_experimental": False,
            },
        )
        monkeypatch.setattr(FasterWhisperProvider, "package_available", staticmethod(lambda: True))
        registered: list[int] = []
        unregistered: list[tuple[int, str]] = []
        monkeypatch.setattr("app.stt.manager.register_process", lambda pid, *_args: registered.append(pid))
        monkeypatch.setattr("app.stt.manager.unregister_process", lambda pid, status="exited": unregistered.append((pid, status)))
        worker_scripts = [delayed_worker, ready_worker]
        monkeypatch.setattr(manager, "_worker_command", lambda *_args: [sys.executable, "-u", str(worker_scripts.pop(0))])

        session_id = "s" * 32
        audio_path = store.root / session_id / "recording.wav"
        audio_path.parent.mkdir(parents=True)
        audio_path.write_bytes(b"RIFF" + b"\x00" * 128)
        running = asyncio.create_task(
            manager.transcribe(
                voice_session_id=session_id,
                audio_path=audio_path,
                audio_sha256="a" * 64,
                audio_duration_ms=400,
            )
        )
        for _ in range(200):
            if manager._starting_worker is not None:
                break
            await asyncio.sleep(0.01)
        starting = manager._starting_worker
        assert starting is not None
        first_pid = starting.pid

        try:
            assert await manager.cancel(voice_session_id=session_id) == {"status": "CANCELLED", "cancelled": 1, "settled": True}
            await asyncio.wait_for(starting.wait(), timeout=2)
            assert starting.returncode is not None
            with pytest.raises(STTError) as raised:
                await running
            assert raised.value.code == "STT_ALREADY_CANCELLED"
            assert manager.status()["active_requests"] == []
            assert manager.status()["worker_pid"] is None
            assert manager._starting_worker is None
            assert manager._worker is None
            assert manager._loaded_model_id is None
            assert unregistered == [(first_pid, "inference_cancel_requested")]

            next_session_id = "n" * 32
            next_audio = store.root / next_session_id / "recording.wav"
            next_audio.parent.mkdir(parents=True)
            next_audio.write_bytes(b"RIFF" + b"\x00" * 128)
            result = await manager.transcribe(
                voice_session_id=next_session_id,
                audio_path=next_audio,
                audio_sha256="b" * 64,
                audio_duration_ms=400,
            )
            assert result.text == "后续可用"
            assert manager.status()["worker_pid"] is not None
            assert len(registered) == 2
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_stt_shutdown_reclaims_delayed_ready_child_before_it_can_become_ready(tmp_path: Path, monkeypatch) -> None:
    _inject_resource_headroom(monkeypatch)
    delayed_worker = tmp_path / "delayed-worker.py"
    delayed_worker.write_text(
        "import time\n"
        "time.sleep(30)\n"
        "print('{\\\"event\\\":\\\"ready\\\",\\\"status\\\":\\\"ok\\\"}', flush=True)\n",
        encoding="utf-8",
    )

    async def scenario() -> None:
        store = TemporaryAudioStore(tmp_path / "audio")
        manager = STTManager(audio_store=store)
        manager.models_root = tmp_path / "models"
        model_root = manager.model_path("base")
        model_root.mkdir(parents=True)
        (model_root / "config.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            manager,
            "settings",
            lambda: {
                "enabled": True,
                "provider": "faster_whisper",
                "model_id": "base",
                "device": "cpu",
                "compute_type": "int8",
                "vad": True,
                "idle_unload_minutes": 0,
                "gpu_experimental": False,
            },
        )
        monkeypatch.setattr(FasterWhisperProvider, "package_available", staticmethod(lambda: True))
        unregistered: list[tuple[int, str]] = []
        monkeypatch.setattr("app.stt.manager.unregister_process", lambda pid, status="exited": unregistered.append((pid, status)))
        monkeypatch.setattr(manager, "_worker_command", lambda *_args: [sys.executable, "-u", str(delayed_worker)])

        session_id = "h" * 32
        audio_path = store.root / session_id / "recording.wav"
        audio_path.parent.mkdir(parents=True)
        audio_path.write_bytes(b"RIFF" + b"\x00" * 128)
        running = asyncio.create_task(
            manager.transcribe(
                voice_session_id=session_id,
                audio_path=audio_path,
                audio_sha256="c" * 64,
                audio_duration_ms=400,
            )
        )
        for _ in range(200):
            if manager._starting_worker is not None:
                break
            await asyncio.sleep(0.01)
        starting = manager._starting_worker
        assert starting is not None
        pid = starting.pid

        await manager.shutdown()
        await asyncio.wait_for(starting.wait(), timeout=2)
        assert starting.returncode is not None
        with pytest.raises(STTError) as raised:
            await running
        assert raised.value.code == "STT_ALREADY_CANCELLED"
        assert manager.status()["active_requests"] == []
        assert manager.status()["worker_pid"] is None
        assert manager._starting_worker is None
        assert manager._worker is None
        assert manager._loaded_model_id is None
        assert unregistered == [(pid, "inference_cancel_requested")]

    asyncio.run(scenario())


def test_resource_coordinator_uses_idempotent_reservations_and_real_worker_pid(monkeypatch) -> None:
    monkeypatch.setattr("app.local_runtime.resource_coordinator._memory", lambda: (16, 8))
    monkeypatch.setattr("app.local_runtime.resource_coordinator._gpu", lambda: (6, 4))
    monkeypatch.setattr("app.local_runtime.resource_coordinator._process_rss", lambda _name: 3)
    seen: list[int | None] = []

    def worker_rss(pid: int | None) -> int | None:
        seen.append(pid)
        return 7 if pid == 4242 else None

    monkeypatch.setattr("app.local_runtime.resource_coordinator._process_rss_by_pid", worker_rss)
    coordinator = ResourceCoordinator()

    assert coordinator.acquire_voice_session("voice-a") is True
    assert coordinator.acquire_voice_session("voice-a") is True
    assert coordinator.acquire_voice_session("voice-b") is False
    assert coordinator.acquire_stt("request-a") is True
    assert coordinator.acquire_stt("request-a") is True
    coordinator.release_voice_session("unknown")
    coordinator.release_stt("unknown")

    snapshot = coordinator.snapshot(stt_provider="faster_whisper", stt_worker_pid=4242)

    assert snapshot["active_voice_sessions"] == 1
    assert snapshot["active_stt_requests"] == 1
    assert snapshot["recording_active"] is True
    assert snapshot["stt_worker_pid"] == 4242
    assert snapshot["stt_rss_bytes"] == 7
    assert seen == [4242]

    coordinator.release_voice_session("voice-a")
    coordinator.release_voice_session("voice-a")
    coordinator.release_stt("request-a")
    assert coordinator.snapshot()["active_voice_sessions"] == 0
    assert coordinator.snapshot()["active_stt_requests"] == 0


def test_resource_admission_reports_ram_and_optional_vram_pressure(monkeypatch) -> None:
    """CPU voice/STT remain GPU-agnostic; GPU work has an observable refusal."""
    coordinator = ResourceCoordinator(minimum_available_ram_bytes=100, minimum_free_vram_bytes=50)
    monkeypatch.setattr("app.local_runtime.resource_coordinator._memory", lambda: (1_000, 99))
    monkeypatch.setattr("app.local_runtime.resource_coordinator._gpu", lambda: (1_000, 49))

    voice = coordinator.assess_admission("voice")
    assert voice["allowed"] is False
    assert voice["reason_code"] == "RESOURCE_RAM_PRESSURE"
    assert voice["reason_codes"] == ("RESOURCE_RAM_PRESSURE",)
    assert voice["system_available_bytes"] == 99
    assert voice["gpu_free_bytes"] == 49
    assert voice["minimum_free_vram_bytes"] is None

    monkeypatch.setattr("app.local_runtime.resource_coordinator._memory", lambda: (1_000, 100))
    cpu_stt = coordinator.assess_admission("stt")
    gpu_stt = coordinator.assess_admission("stt", requires_gpu=True)
    preload = coordinator.assess_admission("model_preload", requires_gpu=True)

    assert cpu_stt["allowed"] is True
    assert cpu_stt["gpu_memory_observed"] is True
    assert gpu_stt["allowed"] is False
    assert gpu_stt["reason_code"] == "RESOURCE_VRAM_PRESSURE"
    assert preload["reason_codes"] == ("RESOURCE_VRAM_PRESSURE",)
    assert coordinator.policy()["text_input_available_under_pressure"] is True
    assert coordinator.policy()["unknown_processes_are_never_terminated"] is True


def test_resource_admission_thresholds_are_configurable_by_environment(monkeypatch) -> None:
    monkeypatch.setenv("SIYI_RESOURCE_MIN_AVAILABLE_RAM_BYTES", "123")
    monkeypatch.setenv("SIYI_RESOURCE_MIN_FREE_VRAM_BYTES", "456")

    coordinator = ResourceCoordinator()

    assert coordinator.policy()["minimum_available_ram_bytes"] == 123
    assert coordinator.policy()["minimum_free_vram_bytes"] == 456


@pytest.mark.parametrize("offset", [-1, 0, 1])
@pytest.mark.parametrize("workload", ["voice", "stt"])
def test_resource_default_ram_threshold_remains_two_gib_at_exact_boundary(
    monkeypatch: pytest.MonkeyPatch,
    offset: int,
    workload: str,
) -> None:
    monkeypatch.delenv("SIYI_RESOURCE_MIN_AVAILABLE_RAM_BYTES", raising=False)
    coordinator = ResourceCoordinator()
    minimum = 2_147_483_648
    admission = coordinator.assess_admission(
        workload,
        snapshot={"system_available_bytes": minimum + offset},
    )

    assert coordinator.policy()["minimum_available_ram_bytes"] == minimum
    assert admission["allowed"] is (offset >= 0)
    if offset < 0:
        assert resource_pressure_details(admission) == {
            "kind": "ram",
            "available_bytes": minimum - 1,
            "minimum_available_bytes": minimum,
        }
    else:
        assert admission["reason_code"] is None
        assert resource_pressure_details(admission) is None


def test_resource_vram_details_exclude_runtime_metadata() -> None:
    coordinator = ResourceCoordinator(minimum_available_ram_bytes=100, minimum_free_vram_bytes=50)
    admission = coordinator.assess_admission(
        "stt",
        requires_gpu=True,
        snapshot={"system_available_bytes": 100, "gpu_free_bytes": 49},
    )

    assert resource_pressure_details(admission) == {
        "kind": "vram",
        "available_bytes": 49,
        "minimum_available_bytes": 50,
    }


def test_stt_model_load_refuses_ram_pressure_before_touching_existing_worker(tmp_path: Path, monkeypatch) -> None:
    manager = STTManager(audio_store=TemporaryAudioStore(tmp_path / "audio"))
    manager.models_root = tmp_path / "models"
    model_path = manager.models_root / "base"
    model_path.mkdir(parents=True)
    (model_path / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        manager,
        "settings",
        lambda: {
            "enabled": True,
            "provider": "faster_whisper",
            "model_id": "base",
            "device": "cpu",
            "compute_type": "int8",
            "vad": True,
            "idle_unload_minutes": 5,
            "gpu_experimental": False,
        },
    )
    coordinator = ResourceCoordinator(minimum_available_ram_bytes=100, minimum_free_vram_bytes=0)
    monkeypatch.setattr("app.stt.manager.resource_coordinator", coordinator)
    monkeypatch.setattr("app.local_runtime.resource_coordinator._memory", lambda: (1_000, 99))
    monkeypatch.setattr("app.local_runtime.resource_coordinator._gpu", lambda: (None, None))
    touched: list[str] = []

    async def unexpected_stop(*_args, **_kwargs) -> None:
        touched.append("stop")

    async def unexpected_start(*_args, **_kwargs) -> dict[str, object]:
        touched.append("start")
        return {"status": "READY"}

    monkeypatch.setattr(manager, "_stop_worker", unexpected_stop)
    monkeypatch.setattr(manager, "_start_worker", unexpected_start)

    with pytest.raises(STTError) as raised:
        asyncio.run(manager.load_model("base"))

    assert raised.value.code == "RESOURCE_RAM_PRESSURE"
    assert raised.value.resource_details == {
        "kind": "ram",
        "available_bytes": 99,
        "minimum_available_bytes": 100,
    }
    assert touched == []
    assert (model_path / "config.json").read_text(encoding="utf-8") == "{}"
