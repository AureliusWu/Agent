from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from app.database import connect, now_iso, rows
from app.local_runtime.resource_coordinator import resource_coordinator, resource_pressure_details
from app.process_supervisor import register_process, unregister_process
from app.runtime_paths import ensure_runtime_layout, runtime_layout

from .providers.faster_whisper import FasterWhisperProvider
from .schemas import (
    DEFAULT_STT_MODEL_ID,
    STT_STATUS_VALUES,
    STTError,
    STTModel,
    STTStatus,
    TranscriptionRequest,
    TranscriptionResult,
)
from .temporary_storage import TemporaryAudioStore


MODELS: dict[str, STTModel] = {
    "base": STTModel("base", "faster_whisper", "Systran/faster-whisper-base", 141_000_000, "速度优先的多语言基础模型"),
    "small": STTModel("small", "faster_whisper", "Systran/faster-whisper-small", 462_000_000, "中文准确度优先的多语言模型"),
}


class STTManager:
    """Coordinates opt-in local STT without implicit model downloads.

    Faster-whisper runs in one supervised child process.  That makes an active
    native transcription genuinely stoppable: the parent terminates the worker
    instead of labelling a still-running thread as cancelled.
    """

    def __init__(self, *, audio_store: TemporaryAudioStore | None = None) -> None:
        layout = ensure_runtime_layout(runtime_layout())
        self.voice_root = (layout.root / "voice").resolve()
        self.models_root = (self.voice_root / "models").resolve()
        self.models_root.mkdir(parents=True, exist_ok=True)
        self.audio_store = audio_store or TemporaryAudioStore()
        self.providers = {"faster_whisper": FasterWhisperProvider()}
        self._active: dict[str, asyncio.Task[Any] | None] = {}
        self._cancelled_requests: set[str] = set()
        self._downloads: dict[str, dict[str, Any]] = {}
        self._download_tasks: dict[str, asyncio.Task[None]] = {}
        self._download_processes: dict[str, asyncio.subprocess.Process] = {}
        # Only process objects created by this manager may be signalled.  The
        # object identity also protects against PID reuse and an unexpected
        # entry being placed in ``_download_processes``.
        self._owned_download_processes: set[asyncio.subprocess.Process] = set()
        self._download_process_lock = asyncio.Lock()
        self._worker: asyncio.subprocess.Process | None = None
        # A Faster-Whisper child can spend a long time loading native weights
        # before it writes its ready line.  Keep that child supervised from
        # the instant ``create_subprocess_exec`` returns, rather than only
        # after the ready handshake succeeds.  Otherwise a voice/global stop
        # during startup cannot see or terminate it.
        self._starting_worker: asyncio.subprocess.Process | None = None
        self._supervised_worker_pids: set[int] = set()
        self._worker_config: tuple[str, str, str, str] | None = None
        self._loaded_model_id: str | None = None
        self._unloading_model_id: str | None = None
        self._worker_lock = asyncio.Lock()
        self._inference_lock = asyncio.Lock()
        self._idle_unload_task: asyncio.Task[None] | None = None
        self._metrics: dict[str, Any] = {
            "requests": 0,
            "failures": 0,
            "cancelled": 0,
            "last_transcription_ms": None,
            "last_load_ms": None,
        }

    def settings(self) -> dict[str, Any]:
        record = rows("SELECT * FROM stt_settings WHERE singleton=1")
        value = record[0] if record else {}
        idle_unload_minutes = value.get("idle_unload_minutes")
        return {
            "enabled": bool(value.get("enabled")),
            "provider": str(value.get("provider") or "faster_whisper"),
            "model_id": str(value.get("model_id") or DEFAULT_STT_MODEL_ID),
            "device": str(value.get("device") or "cpu"),
            "compute_type": str(value.get("compute_type") or "int8"),
            "vad": bool(value.get("vad", 1)),
            # Zero is a valid explicit setting: it disables automatic idle
            # unloading while the user is actively configuring/testing STT.
            # Do not collapse it into the five-minute default through ``or``.
            "idle_unload_minutes": 5 if idle_unload_minutes is None else int(idle_unload_minutes),
            "gpu_experimental": bool(value.get("gpu_experimental")),
        }

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = {**self.settings(), **payload}
        if current["provider"] not in self.providers or current["model_id"] not in MODELS:
            raise STTError("Unknown local STT provider or model", "STT_PROVIDER_UNAVAILABLE")
        if current["device"] != "cpu" and (current["device"] != "cuda" or not current["gpu_experimental"]):
            raise STTError("GPU STT must be explicitly enabled as experimental", "STT_RESOURCE_LIMIT")
        if current["compute_type"] not in {"int8", "int8_float16", "float16", "float32"}:
            raise STTError("Unsupported STT compute type", "STT_MODEL_LOAD_FAILED")
        if not 0 <= int(current["idle_unload_minutes"]) <= 60:
            raise STTError("STT idle unload must be between 0 and 60 minutes", "STT_MODEL_LOAD_FAILED")
        with connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO stt_settings(singleton,enabled,provider,model_id,device,compute_type,vad,idle_unload_minutes,gpu_experimental,updated_at) VALUES(1,?,?,?,?,?,?,?,?,?)",
                (
                    int(bool(current["enabled"])),
                    current["provider"],
                    current["model_id"],
                    current["device"],
                    current["compute_type"],
                    int(bool(current["vad"])),
                    int(current["idle_unload_minutes"]),
                    int(bool(current["gpu_experimental"])),
                    now_iso(),
                ),
            )
        return self.settings()

    def model_path(self, model_id: str) -> Path:
        if model_id not in MODELS:
            raise STTError("Unknown STT model", "STT_MODEL_MISSING")
        target = (self.models_root / model_id).resolve()
        if target.parent != self.models_root:
            raise STTError("Invalid STT model path", "STT_PERMISSION_DENIED")
        return target

    def model_info(self, model_id: str) -> dict[str, Any]:
        model = MODELS.get(model_id)
        if model is None:
            raise STTError("Unknown STT model", "STT_MODEL_MISSING")
        path = self.model_path(model_id)
        installed = (path / "config.json").is_file()
        size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) if path.exists() else 0
        loaded = self._loaded_model_id == model_id and self._worker is not None and self._worker.returncode is None
        return {
            **model.as_dict(),
            "default_for_new_install": model_id == DEFAULT_STT_MODEL_ID,
            "installed": installed,
            "status": "LOADED" if loaded else ("INSTALLED" if installed else "MODEL_MISSING"),
            "size_bytes": size,
            "storage_path": str(path),
            "loaded": loaded,
        }

    @staticmethod
    def _managed_model_reference(model_id: str) -> str:
        """Return the non-sensitive SQLite reference for a managed STT model.

        The direct local UI still receives ``model_info()['storage_path']`` so
        an explicit delete/download confirmation can name the affected folder.
        SQLite only needs to remember which managed model the metadata belongs
        to; retaining the resolved Windows user path there would turn normal
        model status polling into a local-path persistence channel.
        """
        if model_id not in MODELS:
            raise STTError("Unknown STT model", "STT_MODEL_MISSING")
        return f"managed:{model_id}"

    def _persist_model(self, item: dict[str, Any], *, error_code: str | None = None) -> None:
        stamp = now_iso()
        with connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO stt_models(model_id,provider,status,size_bytes,storage_path,device,compute_type,loaded_at,last_used_at,error_code,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item["id"],
                    item["provider"],
                    item["status"],
                    item["size_bytes"],
                    self._managed_model_reference(str(item["id"])),
                    self.settings()["device"],
                    self.settings()["compute_type"],
                    stamp if item["loaded"] else None,
                    stamp,
                    error_code,
                    stamp,
                ),
            )

    def list_models(self) -> list[dict[str, Any]]:
        values = [self.model_info(model_id) for model_id in MODELS]
        for item in values:
            self._persist_model(item)
        return values

    def download_preview(self, model_id: str) -> dict[str, Any]:
        info = self.model_info(model_id)
        free = shutil.disk_usage(self.models_root).free
        required = int(info["estimated_bytes"]) + 128 * 1024 * 1024
        return {
            "model": model_id,
            "repo_id": info["repo_id"],
            "estimated_bytes": info["estimated_bytes"],
            "target_directory": info["storage_path"],
            "already_installed": info["installed"],
            "available_bytes": free,
            "required_bytes": required,
            "fits": free >= required,
        }

    def _worker_command(self, *args: str) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--stt-worker", *args]
        return [sys.executable, "-m", "app.stt.worker", *args]

    async def _terminate_worker_process(self, process: asyncio.subprocess.Process, *, reason: str) -> None:
        """Terminate one supervised worker and release its process record once.

        This helper deliberately does not take ``_worker_lock``.  A model load
        holds that lock while it waits for the child ready handshake, but a
        cancellation must still be able to interrupt that wait immediately.
        """
        try:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.5)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
        finally:
            if process.pid in self._supervised_worker_pids:
                self._supervised_worker_pids.discard(process.pid)
                unregister_process(process.pid, reason)

    async def _stop_worker(self, *, reason: str = "stopped") -> None:
        # Detach both references before the first await.  This is the critical
        # handoff: the startup coroutine will see that it no longer owns its
        # child and cannot publish it as READY after a cancel/shutdown.
        processes: list[asyncio.subprocess.Process] = []
        for process in (self._worker, self._starting_worker):
            if process is not None and process not in processes:
                processes.append(process)
        self._worker = None
        self._starting_worker = None
        self._worker_config = None
        self._loaded_model_id = None
        for process in processes:
            await self._terminate_worker_process(process, reason=reason)

    async def _start_worker(self, *, model_id: str, path: Path, device: str, compute_type: str) -> dict[str, Any]:
        if not FasterWhisperProvider.package_available():
            raise STTError("Faster-whisper runtime is not installed", "STT_PROVIDER_UNAVAILABLE")
        arguments = ["--mode", "infer", "--model-id", model_id, "--model-path", str(path), "--device", device, "--compute-type", compute_type]
        started = time.perf_counter()
        process = await asyncio.create_subprocess_exec(
            *self._worker_command(*arguments),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0),
        )
        # Publish the child before any await (including process-supervisor or
        # pipe I/O) so cancel()/shutdown() can reclaim a worker that has not
        # yet emitted ready.  The startup task remains the owner only while
        # this reference still points at its own process.
        self._starting_worker = process
        try:
            register_process(process.pid, None, "stt-worker", arguments, process)
            self._supervised_worker_pids.add(process.pid)
            assert process.stdout is not None
            line = await asyncio.wait_for(process.stdout.readline(), timeout=120)
            payload = json.loads(line.decode("utf-8")) if line else {}
            if process.returncode is not None or payload.get("event") != "ready" or payload.get("status") != "ok":
                raise STTError("STT model worker failed to start", str(payload.get("code") or "STT_MODEL_LOAD_FAILED"))
            # A concurrent cancel/shutdown clears ``_starting_worker`` before
            # it terminates the child.  Never publish that detached process as
            # a loaded model if a late ready line races the stop request.
            if self._starting_worker is not process:
                raise STTError("STT model worker was stopped during startup", "STT_ALREADY_CANCELLED")
        except BaseException:
            # CancelledError derives from BaseException.  Treat task
            # cancellation exactly like an invalid/timeout ready handshake so
            # a child cannot outlive its cancelled parent coroutine.
            if self._starting_worker is process:
                self._starting_worker = None
                await self._terminate_worker_process(process, reason="stt_start_failed")
            raise
        self._starting_worker = None
        self._worker = process
        self._worker_config = (model_id, str(path), device, compute_type)
        self._loaded_model_id = model_id
        load_ms = round((time.perf_counter() - started) * 1000, 3)
        self._metrics["last_load_ms"] = load_ms
        return {"status": "READY", "model": model_id, "load_ms": load_ms, "device": device, "compute_type": compute_type, "worker_pid": process.pid}

    async def load_model(self, model_id: str | None = None) -> dict[str, Any]:
        settings = self.settings()
        selected = model_id or settings["model_id"]
        model = MODELS.get(selected)
        if model is None:
            raise STTError("Unknown STT model", "STT_MODEL_MISSING")
        path = self.model_path(selected)
        if not (path / "config.json").is_file():
            raise STTError("STT model is not installed locally", "STT_MODEL_MISSING")
        expected = (selected, str(path), settings["device"], settings["compute_type"])
        async with self._worker_lock:
            if self._worker is not None and self._worker.returncode is None and self._worker_config == expected:
                return {"status": "READY", "model": selected, "load_ms": 0, "device": settings["device"], "compute_type": settings["compute_type"], "worker_pid": self._worker.pid}
            # Admission is deliberately assessed before changing any existing
            # worker.  A low-resource request must leave a usable loaded model
            # alone and must never solve pressure by terminating a process.
            admission = resource_coordinator.assess_admission(
                "stt",
                requires_gpu=settings["device"] == "cuda",
            )
            if not admission["allowed"]:
                raise STTError(
                    str(admission["reason"] or "STT model load was refused due to resource pressure"),
                    str(admission["reason_code"] or "STT_RESOURCE_LIMIT"),
                    resource_details=resource_pressure_details(admission),
                )
            # A settings switch must not silently kill a live transcription.
            # The explicit cancel endpoint owns that stop semantic.
            if self._active and self._worker is not None and self._worker.returncode is None:
                raise STTError("Cannot switch STT model while transcription is active", "STT_RESOURCE_LIMIT")
            await self._stop_worker(reason="model_switched")
            result = await self._start_worker(model_id=selected, path=path, device=settings["device"], compute_type=settings["compute_type"])
        self._persist_model(self.model_info(selected))
        return result

    async def unload_model(self, model_id: str | None = None) -> dict[str, Any]:
        selected = model_id or self.settings()["model_id"]
        if selected not in MODELS:
            raise STTError("Unknown STT model", "STT_MODEL_MISSING")
        if self._active:
            raise STTError("Cannot unload STT model while transcription is active", "STT_RESOURCE_LIMIT")
        self._unloading_model_id = selected
        try:
            if self._loaded_model_id == selected:
                async with self._worker_lock:
                    await self._stop_worker(reason="model_unloaded")
            item = self.model_info(selected)
            self._persist_model(item)
            return {"status": "UNLOADED", "model": selected, "device": self.settings()["device"]}
        finally:
            self._unloading_model_id = None

    async def _terminate_download_process(
        self,
        model_id: str,
        *,
        reason: str,
        expected_process: asyncio.subprocess.Process | None = None,
    ) -> bool:
        """Stop and reap one process that this manager created.

        ``expected_process`` lets the download coroutine retain ownership even
        if a concurrent cancellation changes the lookup table.  Calls without
        it (the public cancel/shutdown paths) may only act on the exact object
        currently registered for ``model_id``.  Unknown objects are never
        signalled.
        """

        process = expected_process or self._download_processes.get(model_id)
        if process is None or process not in self._owned_download_processes:
            return False
        async with self._download_process_lock:
            if process.returncode is None:
                try:
                    process.terminate()
                except (OSError, ProcessLookupError):
                    # The child may have exited between the return-code check
                    # and the signal.  ``wait`` below remains authoritative.
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.5)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except (OSError, ProcessLookupError):
                        pass
                    await process.wait()
            return process.returncode is not None

    async def start_download(self, model_id: str, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise STTError("Model download requires explicit confirmation", "STT_DOWNLOAD_CONFIRMATION_REQUIRED")
        model = MODELS.get(model_id)
        if model is None:
            raise STTError("Unknown STT model", "STT_MODEL_MISSING")
        preview = self.download_preview(model_id)
        if not preview["fits"]:
            raise STTError("Insufficient disk space for the selected STT model", "STT_RESOURCE_LIMIT")
        if not FasterWhisperProvider.package_available() or importlib.util.find_spec("huggingface_hub") is None:
            raise STTError("STT model download support is unavailable", "STT_PROVIDER_UNAVAILABLE")
        async with self._worker_lock:
            if model_id in self._download_tasks and not self._download_tasks[model_id].done():
                return self.download_state(model_id)
            # Re-evaluate under the manager lock.  The preview may have been
            # produced just before another local action completed the model.
            current = self.model_info(model_id)
            if current["installed"]:
                state = {"model": model_id, "status": "INSTALLED", "completed": preview["estimated_bytes"], "total": preview["estimated_bytes"], "error": None}
                self._downloads[model_id] = state
                return state
            # An incomplete directory, dangling link, or other unexpected
            # target is not ours to overwrite.  A later target race is checked
            # again immediately before the atomic rename in ``_download``.
            if os.path.lexists(self._download_target_path(model_id)):
                raise STTError("STT model target already exists", "STT_DOWNLOAD_FAILED")
            record_id = uuid.uuid4().hex
            state = {"model": model_id, "status": "DOWNLOADING", "completed": 0, "total": model.estimated_bytes, "error": None, "record_id": record_id, "cancel_requested": False}
            self._downloads[model_id] = state
            with connect() as db:
                db.execute(
                    "INSERT INTO stt_download_records(id,model_id,provider,status,confirmed,completed_bytes,total_bytes,target_directory,started_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (record_id, model_id, model.provider, "DOWNLOADING", 1, 0, model.estimated_bytes, self._managed_model_reference(model_id), now_iso()),
                )
            self._download_tasks[model_id] = asyncio.create_task(self._download(model_id), name=f"stt-download-{model_id}")
        return self.download_state(model_id)

    @staticmethod
    def _directory_size(path: Path) -> int:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) if path.exists() else 0

    def _download_target_path(self, model_id: str) -> Path:
        if model_id not in MODELS:
            raise STTError("Unknown STT model", "STT_MODEL_MISSING")
        root = self.models_root.resolve(strict=True)
        target = root / model_id
        if target.parent != root:
            raise STTError("STT model path escaped its managed root", "STT_PERMISSION_DENIED")
        return target

    def _download_temporary_path(self, model_id: str, record_id: str) -> Path:
        """Return the unique staging directory owned by one download record."""

        if model_id not in MODELS or len(record_id) != 32 or any(character not in "0123456789abcdef" for character in record_id):
            raise STTError("Invalid managed STT download path", "STT_PERMISSION_DENIED")
        root = self.models_root.resolve(strict=True)
        temporary = root / f".{model_id}.{record_id}.downloading"
        if temporary.parent != root:
            raise STTError("STT download path escaped its managed root", "STT_PERMISSION_DENIED")
        return temporary

    def _owned_download_directory(self, path: Path, *, model_id: str, record_id: str) -> Path | None:
        """Return a direct, real staging directory owned by this exact record.

        Cleanup paths are checked with ``lstat`` so a junction, symlink, or
        other Windows reparse point can never turn cancellation into a
        recursive delete outside the managed model root.
        """

        expected = self._download_temporary_path(model_id, record_id)
        if path != expected:
            return None
        try:
            metadata = path.lstat()
        except OSError:
            return None
        reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
        if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_point) or not stat.S_ISDIR(metadata.st_mode):
            return None
        try:
            resolved = path.resolve(strict=True)
            root = self.models_root.resolve(strict=True)
        except OSError:
            return None
        if resolved.parent != root:
            return None
        return resolved

    def _cleanup_download_directory(self, path: Path, *, model_id: str, record_id: str) -> bool:
        owned = self._owned_download_directory(path, model_id=model_id, record_id=record_id)
        if owned is None:
            return False
        try:
            shutil.rmtree(owned)
        except OSError:
            return False
        return True

    async def _download(self, model_id: str) -> None:
        model = MODELS[model_id]
        state = self._downloads[model_id]
        record_id = str(state["record_id"])
        target = self._download_target_path(model_id)
        temporary = self._download_temporary_path(model_id, record_id)
        process: asyncio.subprocess.Process | None = None
        process_registered = False
        staging_created = False
        terminal_status = "ERROR"
        terminal_error: str | None = "STT_DOWNLOAD_FAILED"
        try:
            if state.get("cancel_requested"):
                raise asyncio.CancelledError()
            # A record owns exactly one unpredictable staging directory.  Do
            # not reuse or clean a path left by another record: collision is a
            # fail-closed error, even though UUID collisions are improbable.
            if os.path.lexists(temporary):
                raise STTError("STT download staging path already exists", "STT_DOWNLOAD_FAILED")
            temporary.mkdir(parents=False, exist_ok=False)
            staging_created = True
            if self._owned_download_directory(temporary, model_id=model_id, record_id=record_id) is None:
                raise STTError("STT download staging path is not a managed directory", "STT_PERMISSION_DENIED")
            if state.get("cancel_requested"):
                raise asyncio.CancelledError()
            arguments = ["--mode", "download", "--repo-id", model.repo_id, "--target", str(temporary)]
            process = await asyncio.create_subprocess_exec(
                *self._worker_command(*arguments),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0),
            )
            self._owned_download_processes.add(process)
            self._download_processes[model_id] = process
            register_process(process.pid, None, "stt-download", arguments, process)
            process_registered = True
            while process.returncode is None:
                if state.get("cancel_requested"):
                    raise asyncio.CancelledError()
                owned_temporary = self._owned_download_directory(temporary, model_id=model_id, record_id=record_id)
                if owned_temporary is None:
                    raise STTError("STT download staging directory changed during download", "STT_PERMISSION_DENIED")
                state["completed"] = min(self._directory_size(owned_temporary), int(state["total"]))
                await asyncio.sleep(0.35)
            assert process.stdout is not None
            response = json.loads((await process.stdout.read()).decode("utf-8") or "{}")
            if state.get("cancel_requested"):
                raise asyncio.CancelledError()
            owned_temporary = self._owned_download_directory(temporary, model_id=model_id, record_id=record_id)
            if owned_temporary is None:
                raise STTError("STT download staging directory changed during download", "STT_PERMISSION_DENIED")
            if process.returncode != 0 or response.get("status") != "ok" or not (owned_temporary / "config.json").is_file():
                raise STTError("Downloaded STT model is incomplete", str(response.get("code") or "STT_DOWNLOAD_FAILED"))
            # The final target was absent when the confirmed download began.
            # If any entry appears meanwhile (including a dangling link), it
            # belongs to somebody else and must never be deleted or replaced.
            if os.path.lexists(target):
                raise STTError("STT model target appeared during download", "STT_DOWNLOAD_FAILED")
            try:
                # ``rename`` preserves the same-volume atomic handoff and, on
                # the supported Windows runtime, has no-overwrite semantics.
                # It intentionally replaces the old ``Path.replace`` call.
                owned_temporary.rename(target)
            except OSError as exc:
                # Treat a destination race as a controlled download failure;
                # never remove the competing target in recovery.
                if os.path.lexists(target):
                    raise STTError("STT model target appeared during download", "STT_DOWNLOAD_FAILED") from exc
                raise
            staging_created = False
            size = self._directory_size(target)
            state.update({"status": "INSTALLED", "completed": size, "total": size})
            self._finish_download(model_id, "INSTALLED", completed=size)
            self._persist_model(self.model_info(model_id))
            terminal_status = "INSTALLED"
            terminal_error = None
        except asyncio.CancelledError:
            terminal_status = "CANCELLED"
            terminal_error = None
        except STTError as exc:
            terminal_error = exc.code
        except Exception:
            terminal_error = "STT_DOWNLOAD_FAILED"
        finally:
            try:
                process_reaped = process is None or await self._terminate_download_process(
                    model_id,
                    reason=("download_cancelled" if terminal_status == "CANCELLED" else "download_finished"),
                    expected_process=process,
                )
                # Never remove a directory while its child may still write to
                # it.  Leaving a uniquely owned staging directory behind is
                # safer than creating an orphan that writes through deletion.
                if terminal_status != "INSTALLED" and not process_reaped:
                    terminal_status = "ERROR"
                    terminal_error = "STT_DOWNLOAD_PROCESS_STILL_RUNNING"
                if terminal_status != "INSTALLED" and staging_created and process_reaped:
                    self._cleanup_download_directory(temporary, model_id=model_id, record_id=record_id)
                if terminal_status == "CANCELLED":
                    state.update({"status": "CANCELLED", "error": None})
                    self._finish_download(model_id, "CANCELLED")
                elif terminal_status != "INSTALLED":
                    state.update({"status": "ERROR", "error": terminal_error})
                    self._finish_download(model_id, "ERROR", error=terminal_error)
            finally:
                if process is not None:
                    try:
                        if process_registered:
                            unregister_process(process.pid, "download_finished")
                    finally:
                        if self._download_processes.get(model_id) is process:
                            self._download_processes.pop(model_id, None)
                        self._owned_download_processes.discard(process)

    def _finish_download(self, model_id: str, status: str, *, completed: int | None = None, error: str | None = None) -> None:
        state = self._downloads[model_id]
        with connect() as db:
            db.execute(
                "UPDATE stt_download_records SET status=?,completed_bytes=?,error_code=?,finished_at=? WHERE id=?",
                (status, int(completed if completed is not None else state.get("completed") or 0), error, now_iso(), state["record_id"]),
            )

    def download_state(self, model_id: str) -> dict[str, Any]:
        if model_id not in MODELS:
            raise STTError("Unknown STT model", "STT_MODEL_MISSING")
        state = self._downloads.get(model_id)
        if state:
            return {key: value for key, value in state.items() if key not in {"record_id", "cancel_requested"}}
        info = self.model_info(model_id)
        return {"model": model_id, "status": "INSTALLED" if info["installed"] else "NOT_STARTED", "completed": info["size_bytes"], "total": info["size_bytes"], "error": None}

    async def cancel_download(self, model_id: str) -> dict[str, Any]:
        state = self._downloads.get(model_id)
        task = self._download_tasks.get(model_id)
        if not state or not task or task.done():
            raise STTError("STT download is not active", "STT_ALREADY_CANCELLED")
        state["cancel_requested"] = True
        await self._terminate_download_process(model_id, reason="download_cancel_requested")
        return {**self.download_state(model_id), "status": "CANCEL_REQUESTED"}

    async def delete_model(self, model_id: str, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise STTError("Deleting an STT model requires confirmation", "STT_DELETE_CONFIRMATION_REQUIRED")
        path = self.model_path(model_id)
        download = self._download_tasks.get(model_id)
        if download is not None and not download.done():
            # Deletion is an explicit request to stop an in-flight local model
            # download.  Wait for its child process cleanup before removing the
            # final target so it cannot reappear after this endpoint returns.
            await self.cancel_download(model_id)
            await asyncio.gather(download, return_exceptions=True)
        if self._loaded_model_id == model_id:
            await self.unload_model(model_id)
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise STTError(
                f"STT model directory could not be deleted: {type(exc).__name__}",
                "STT_MODEL_DELETE_FAILED",
            ) from exc
        # A filesystem filter, lock, or monkeypatched remover must never turn
        # a failed destructive operation into a successful API response.
        if os.path.lexists(path):
            raise STTError("STT model directory still exists after deletion", "STT_MODEL_DELETE_FAILED")
        self._persist_model(self.model_info(model_id))
        return {"model": model_id, "status": "DELETED"}

    async def _request_worker(self, request: TranscriptionRequest) -> TranscriptionResult:
        process = self._worker
        if process is None or process.returncode is not None or process.stdin is None or process.stdout is None:
            raise STTError("STT worker is unavailable", "STT_MODEL_LOAD_FAILED")
        payload = {
            "operation": "transcribe",
            "request_id": request.request_id,
            "voice_session_id": request.voice_session_id,
            "audio_path": request.audio_path,
            "audio_sha256": request.audio_sha256,
            "audio_duration_ms": request.audio_duration_ms,
            "language": request.language,
            "model_id": request.model_id,
            "device": request.device,
            "compute_type": request.compute_type,
            "vad": request.vad,
            "timestamps": request.timestamps,
        }
        try:
            process.stdin.write((json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8"))
            await process.stdin.drain()
            line = await asyncio.wait_for(process.stdout.readline(), timeout=120)
        except asyncio.CancelledError:
            self._cancelled_requests.add(request.request_id)
            await self._stop_worker(reason="inference_cancelled")
            raise
        except Exception as exc:
            if request.request_id in self._cancelled_requests:
                raise STTError("STT request was cancelled", "STT_ALREADY_CANCELLED") from exc
            raise STTError("STT worker pipe failed", "STT_TRANSCRIPTION_FAILED") from exc
        if request.request_id in self._cancelled_requests:
            raise STTError("STT request was cancelled", "STT_ALREADY_CANCELLED")
        try:
            response = json.loads(line.decode("utf-8")) if line else {}
        except ValueError as exc:
            raise STTError("STT worker returned invalid data", "STT_TRANSCRIPTION_FAILED") from exc
        if response.get("status") != "ok" or response.get("request_id") != request.request_id:
            raise STTError("STT worker transcription failed", str(response.get("code") or "STT_TRANSCRIPTION_FAILED"))
        value = response.get("result")
        if not isinstance(value, dict):
            raise STTError("STT worker returned no result", "STT_TRANSCRIPTION_FAILED")
        return TranscriptionResult(
            request_id=str(value["request_id"]),
            provider=str(value["provider"]),
            model=str(value["model"]),
            language=str(value["language"]),
            text=str(value["text"]),
            segments=list(value.get("segments") or []),
            duration_ms=int(value["duration_ms"]),
            transcription_ms=float(value["transcription_ms"]),
            real_time_factor=float(value["real_time_factor"]) if value.get("real_time_factor") is not None else None,
            confidence=float(value["confidence"]) if value.get("confidence") is not None else None,
            reliable=value.get("reliable") is True,
            reliability_reason=str(value.get("reliability_reason") or "CONFIDENCE_UNAVAILABLE"),
        )

    async def transcribe(self, *, voice_session_id: str, audio_path: Path, audio_sha256: str, audio_duration_ms: int) -> TranscriptionResult:
        settings = self.settings()
        if not settings["enabled"]:
            raise STTError("Local speech input is disabled", "STT_PROVIDER_UNAVAILABLE")
        model = MODELS[settings["model_id"]]
        root = self.audio_store.root.resolve()
        resolved_audio = audio_path.resolve()
        if root not in resolved_audio.parents or resolved_audio.suffix.lower() != ".wav":
            raise STTError("Audio escaped the managed voice directory", "STT_PERMISSION_DENIED")
        request = TranscriptionRequest(
            request_id=uuid.uuid4().hex,
            voice_session_id=voice_session_id,
            audio_path=str(resolved_audio),
            audio_sha256=audio_sha256,
            audio_duration_ms=audio_duration_ms,
            language="zh",
            model_id=model.id,
            device=settings["device"],
            compute_type=settings["compute_type"],
            vad=settings["vad"],
        )
        stamp = now_iso()
        with connect() as db:
            db.execute(
                "INSERT INTO stt_requests(request_id,voice_session_id,provider,model_id,status,audio_sha256,audio_duration_ms,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (request.request_id, voice_session_id, model.provider, model.id, "TRANSCRIBING", audio_sha256, audio_duration_ms, stamp, stamp),
            )
        self._active[request.request_id] = asyncio.current_task()
        try:
            # Register the request before model loading.  A user cancellation
            # during a slow native model load now targets a real request and
            # cannot later be relabelled as an unrelated load failure.
            await self.load_model(model.id)
            if request.request_id in self._cancelled_requests:
                raise STTError("STT request was cancelled", "STT_ALREADY_CANCELLED")
            async with self._inference_lock:
                if request.request_id in self._cancelled_requests:
                    raise STTError("STT request was cancelled", "STT_ALREADY_CANCELLED")
                result = await self._request_worker(request)
            text_hash = hashlib.sha256(result.text.encode("utf-8")).hexdigest()
            with connect() as db:
                db.execute("UPDATE stt_requests SET status='COMPLETED',transcription_ms=?,result_text_hash=?,updated_at=? WHERE request_id=?", (result.transcription_ms, text_hash, now_iso(), request.request_id))
                db.execute(
                    "INSERT INTO stt_metrics(id,request_id,metric_name,value,unit,metadata_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, request.request_id, "transcription", result.transcription_ms, "ms", json.dumps({"rtf": result.real_time_factor}, ensure_ascii=True), now_iso()),
                )
            self._metrics.update({"requests": self._metrics["requests"] + 1, "last_transcription_ms": result.transcription_ms})
            return result
        except asyncio.CancelledError:
            self._cancelled_requests.add(request.request_id)
            await self._stop_worker(reason="inference_cancelled")
            with connect() as db:
                db.execute("UPDATE stt_requests SET status='CANCELLED',error_code='STT_ALREADY_CANCELLED',updated_at=? WHERE request_id=?", (now_iso(), request.request_id))
            self._metrics["cancelled"] += 1
            raise STTError("STT request was cancelled", "STT_ALREADY_CANCELLED")
        except STTError as exc:
            cancelled = exc.code == "STT_ALREADY_CANCELLED" or request.request_id in self._cancelled_requests
            error_code = "STT_ALREADY_CANCELLED" if cancelled else exc.code
            with connect() as db:
                db.execute(
                    "UPDATE stt_requests SET status=?,error_code=?,updated_at=? WHERE request_id=?",
                    ("CANCELLED" if cancelled else "FAILED", error_code, now_iso(), request.request_id),
                )
            self._metrics["cancelled" if cancelled else "failures"] += 1
            if cancelled and exc.code != "STT_ALREADY_CANCELLED":
                raise STTError("STT request was cancelled", "STT_ALREADY_CANCELLED") from exc
            raise
        finally:
            self._active.pop(request.request_id, None)
            self._cancelled_requests.discard(request.request_id)
            self._schedule_idle_unload()

    def _schedule_idle_unload(self) -> None:
        minutes = self.settings()["idle_unload_minutes"]
        if self._idle_unload_task is not None:
            self._idle_unload_task.cancel()
            self._idle_unload_task = None
        if minutes <= 0 or not self._loaded_model_id or self._active:
            return

        async def unload_when_idle() -> None:
            try:
                await asyncio.sleep(minutes * 60)
                if not self._active and self._loaded_model_id:
                    await self.unload_model(self._loaded_model_id)
            except asyncio.CancelledError:
                return

        self._idle_unload_task = asyncio.create_task(unload_when_idle(), name="stt-idle-unload")

    async def cancel(self, *, request_id: str | None = None, voice_session_id: str | None = None) -> dict[str, Any]:
        targets = [request_id] if request_id else [
            key for key in self._active
            if not voice_session_id or any(
                row.get("voice_session_id") == voice_session_id
                for row in rows("SELECT voice_session_id FROM stt_requests WHERE request_id=?", (key,))
            )
        ]
        targets = [str(key) for key in targets if key and key in self._active]
        if not targets:
            return {"status": "CANCELLED", "cancelled": 0, "settled": True}
        deadline = time.monotonic() + 0.5
        self._cancelled_requests.update(targets)
        # Cancel the Python owner as well as the native worker.  This covers
        # the model-load window where no ready worker exists yet and makes the
        # ``_active`` registry, not a signal acknowledgement, authoritative.
        current = asyncio.current_task()
        for key in targets:
            task = self._active.get(key)
            if task is not None and task is not current and not task.done():
                task.cancel()
        await self._stop_worker(reason="inference_cancel_requested")
        # Terminating the native worker wakes the owning transcribe coroutine,
        # which must persist CANCELLED and remove itself from ``_active``.
        # Observe that convergence for at most the public 500 ms cancel gate;
        # never claim terminal cancellation while a target is still active.
        remaining = [key for key in targets if key in self._active]
        while remaining and time.monotonic() < deadline:
            await asyncio.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            remaining = [key for key in targets if key in self._active]
        settled = not remaining
        return {
            "status": "CANCELLED" if settled else "CANCEL_REQUESTED",
            "cancelled": len(targets),
            "settled": settled,
        }

    async def shutdown(self) -> None:
        if self._idle_unload_task is not None:
            self._idle_unload_task.cancel()
            await asyncio.gather(self._idle_unload_task, return_exceptions=True)
        await self.cancel()
        await self._stop_worker(reason="shutdown")
        for model_id, task in tuple(self._download_tasks.items()):
            if not task.done():
                state = self._downloads.get(model_id)
                if state:
                    state["cancel_requested"] = True
                await self._terminate_download_process(model_id, reason="shutdown")
        await asyncio.gather(*(task for task in self._download_tasks.values()), return_exceptions=True)
        self.audio_store.cleanup_expired(0)

    def health(self) -> dict[str, Any]:
        checks = [provider.health_check() for provider in self.providers.values()]
        return {
            "status": "ok" if any(item["status"] == "ok" for item in checks) else "unavailable",
            "providers": checks,
            "settings": self.settings(),
            "worker": {"loaded_model": self._loaded_model_id, "pid": self._worker.pid if self._worker and self._worker.returncode is None else None},
        }

    def status(self) -> dict[str, Any]:
        settings = self.settings()
        selected_model = str(settings.get("model_id") or DEFAULT_STT_MODEL_ID)
        downloading = any(
            state.get("status") == "DOWNLOADING"
            and (task := self._download_tasks.get(model_id)) is not None
            and not task.done()
            for model_id, state in self._downloads.items()
        )
        if self._active:
            lifecycle: STTStatus = (
                "CANCEL_REQUESTED"
                if any(request_id in self._cancelled_requests for request_id in self._active)
                else "TRANSCRIBING"
            )
        elif self._starting_worker is not None:
            lifecycle = "LOADING"
        elif self._unloading_model_id is not None:
            lifecycle = "UNLOADING"
        elif downloading:
            lifecycle = "DOWNLOADING"
        elif not settings.get("enabled"):
            lifecycle = "NOT_CONFIGURED"
        elif selected_model not in MODELS or not (self.model_path(selected_model) / "config.json").is_file():
            lifecycle = "MODEL_MISSING"
        else:
            # Installed CPU-first models are ready for the manager's lazy load
            # even when no resident worker is currently consuming memory.
            lifecycle = "READY"
        return {
            "status": lifecycle,
            "allowed_statuses": sorted(STT_STATUS_VALUES),
            "active_requests": list(self._active),
            "providers": [provider.status() for provider in self.providers.values()],
            "loaded_model": self._loaded_model_id,
            "worker_pid": self._worker.pid if self._worker and self._worker.returncode is None else None,
        }

    def metrics(self) -> dict[str, Any]:
        return {**self._metrics, "active_requests": len(self._active), "downloads": [self.download_state(model_id) for model_id in MODELS]}


stt_manager = STTManager()
