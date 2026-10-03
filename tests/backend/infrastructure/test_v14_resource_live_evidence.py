from __future__ import annotations

import io
import importlib.util
import json
import sys
import wave
from argparse import Namespace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "v14-resource-live-evidence.py"
SPEC = importlib.util.spec_from_file_location("v14_resource_live_evidence", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _arguments(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "output": "build/v1400-evidence/raw/a20.json",
        "execute_live_resource_run": False,
        "models_root": Path("formal-models"),
        "ollama_models_root": ROOT,
        "ollama_url": "http://127.0.0.1:11435",
        "startup_timeout_seconds": 45.0,
        "controller_timeout_seconds": 240.0,
        "allow_test_owned_ollama_start": False,
        "test_owned_ollama_service_id": None,
        "test_owned_ollama_executable": None,
    }
    values.update(overrides)
    return Namespace(**values)


def test_v14_resource_evidence_output_is_bounded_to_ignored_evidence_root(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    evidence_root = root / "build" / "v1400-evidence"
    evidence_root.mkdir(parents=True)

    assert MODULE.resolve_output_path(
        "build/v1400-evidence/raw/a20.json", repository_root=root
    ) == evidence_root / "raw" / "a20.json"
    with pytest.raises(MODULE.ResourceEvidenceError, match="build/v1400-evidence"):
        MODULE.resolve_output_path("outside.json", repository_root=root)


def test_v14_resource_evidence_refuses_non_loopback_or_non_default_ollama() -> None:
    assert MODULE.validate_ollama_url("http://localhost:11435/") == "http://localhost:11435"
    for bad in (
        "https://127.0.0.1:11434",
        "http://example.test:11434",
        "http://127.0.0.1:11434",
        "http://token@127.0.0.1:11434",
        "http://127.0.0.1:11435/api",
    ):
        with pytest.raises(MODULE.ResourceEvidenceError):
            MODULE.validate_ollama_url(bad)


def test_v14_resource_evidence_does_not_run_without_explicit_live_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "a20.json"
    monkeypatch.setattr(MODULE, "resolve_output_path", lambda value: output)
    # This test isolates the explicit execution switch from preflight
    # ownership/model-store validation, which has dedicated coverage below.
    monkeypatch.setattr(MODULE, "validate_selection", lambda _arguments: None)
    called = False

    def unexpected_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {"status": "PASS", "actual_run": True}

    monkeypatch.setattr(MODULE, "run_live_resource_evidence", unexpected_run)

    assert MODULE.main(["--output", "build/v1400-evidence/raw/a20.json"]) == 2
    assert called is False
    assert not output.exists()
    assert "--execute-live-resource-run" in capsys.readouterr().err


def test_v14_resource_evidence_test_owned_start_requires_strong_identity() -> None:
    with pytest.raises(MODULE.ResourceEvidenceError, match="test-owned managed service"):
        MODULE.validate_selection(_arguments())
    with pytest.raises(MODULE.ResourceEvidenceError, match="test-owned-ollama-service-id"):
        MODULE.validate_selection(
            _arguments(allow_test_owned_ollama_start=True, test_owned_ollama_service_id="too-short")
        )

    MODULE.validate_selection(
        _arguments(
            allow_test_owned_ollama_start=True,
            test_owned_ollama_service_id="a20-test-owned-ollama-identity-001",
        )
    )
    with pytest.raises(MODULE.ResourceEvidenceError, match="test-owned managed service"):
        MODULE.validate_selection(_arguments(test_owned_ollama_service_id="a20-test-owned-ollama-identity-001"))


def test_v14_resource_evidence_requires_product_default_small_without_fallback_or_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # RC basetemp may be under the repository or LOCALAPPDATA. Select the
    # external branch explicitly instead of inheriting the runner's layout.
    monkeypatch.setattr(MODULE, "ROOT", tmp_path / "repository")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    models_root = tmp_path / "models"
    small = models_root / "small"
    small.mkdir(parents=True)
    (small / "config.json").write_text("{}", encoding="utf-8")

    assert MODULE.DEFAULT_STT_MODEL_ID == "small"
    assert MODULE.STT_MODEL == MODULE.DEFAULT_STT_MODEL_ID
    assert MODULE.final_default_stt_model_contract() == {
        "model_id": "small",
        "source": "app.stt.schemas.DEFAULT_STT_MODEL_ID",
        "fallback": "DISALLOWED",
        "download_operation": "NOT_CALLED",
    }
    metadata = MODULE.installed_stt_model_metadata(models_root)
    model_directory = metadata.pop("model_directory")
    assert model_directory == "<external-local-path>/small"
    assert str(tmp_path) not in model_directory
    assert metadata == {
        "id": "small",
        "installed_before_run": True,
        "size_bytes": 2,
        "file_count": 1,
        "download_operation": "NOT_CALLED",
        "delete_operation": "NOT_CALLED",
    }

    monkeypatch.setattr(MODULE, "STT_MODEL", "base")
    with pytest.raises(MODULE.ResourceEvidenceError, match="no base fallback"):
        MODULE.validate_selection(
            _arguments(
                allow_test_owned_ollama_start=True,
                test_owned_ollama_service_id="a20-test-owned-ollama-identity-001",
            )
        )


@pytest.mark.parametrize(
    ("scope", "relative", "expected"),
    [
        ("repository", "models/small", "models/small"),
        ("local_app_data", "AureliusWu/Agent/voice/models/small", "%LOCALAPPDATA%/AureliusWu/Agent/voice/models/small"),
        ("external", "models/small", "<external-local-path>/small"),
    ],
)
def test_v14_resource_evidence_display_path_redacts_each_supported_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scope: str, relative: str, expected: str
) -> None:
    roots = {
        "repository": tmp_path / "repository-private",
        "local_app_data": tmp_path / "local-app-data-private",
        "external": tmp_path / "external-private",
    }
    monkeypatch.setattr(MODULE, "ROOT", roots["repository"])
    monkeypatch.setenv("LOCALAPPDATA", str(roots["local_app_data"]))
    model_directory = roots[scope] / relative
    model_directory.mkdir(parents=True)

    displayed = MODULE._display_path(model_directory)

    assert displayed == expected
    assert not Path(displayed).is_absolute()
    assert str(tmp_path) not in displayed
    assert tmp_path.as_posix() not in displayed
    assert all(root.name not in displayed for root in roots.values())
    assert "\\" not in displayed


def test_v14_resource_evidence_immutable_writer_rejects_replacement(tmp_path: Path) -> None:
    output = tmp_path / "a20.json"
    MODULE.write_json_immutable(output, {"status": "PASS"})

    with pytest.raises(MODULE.ResourceEvidenceError, match="refusing to overwrite"):
        MODULE.write_json_immutable(output, {"status": "FAIL"})
    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "PASS"}


def test_v14_resource_writer_uses_exclusive_create_and_fsync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opened_flags: list[int] = []
    fsync_calls: list[int] = []
    real_open = MODULE.os.open
    real_fsync = MODULE.os.fsync

    def recording_open(path: object, flags: int, mode: int = 0o777) -> int:
        opened_flags.append(flags)
        return real_open(path, flags, mode)

    def recording_fsync(descriptor: int) -> None:
        fsync_calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(MODULE.os, "open", recording_open)
    monkeypatch.setattr(MODULE.os, "fsync", recording_fsync)
    MODULE.write_json_immutable(tmp_path / "a20-exclusive.json", {"status": "PASS"})

    assert opened_flags and opened_flags[-1] & MODULE.os.O_EXCL
    assert fsync_calls


def test_v14_resource_source_identity_uses_runner_value_without_local_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = {
        "source_version": "13.0.0",
        "source_commit": "a" * 40,
        "source_tree_fingerprint": "B" * 64,
        "workspace_clean": False,
    }
    monkeypatch.setenv("SIYI_V14_EVIDENCE_SOURCE_IDENTITY", json.dumps(identity))

    serialized = json.dumps(MODULE.source_identity())
    assert json.loads(serialized) == identity
    assert chr(67) + ":\\" not in serialized


def test_v14_resource_evidence_compares_full_external_process_identity() -> None:
    before = MODULE.OllamaProcessIdentity(
        pid=1234,
        creation_date="20260811000000.000000+480",
        executable_name="ollama.exe",
        command_sha256="A" * 64,
    )
    assert MODULE.same_ollama_identity(before, before)
    assert not MODULE.same_ollama_identity(
        before,
        MODULE.OllamaProcessIdentity(
            pid=1234,
            creation_date="20260811120000.000000+480",
            executable_name="ollama.exe",
            command_sha256="A" * 64,
        ),
    )


def _audible_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24_000)
        handle.writeframes(b"\x00\x00" * 1_200)
    return buffer.getvalue()


def test_v14_resource_evidence_temp_residual_check_does_not_delete(tmp_path: Path) -> None:
    sidecar = tmp_path / "sidecar"
    temp = sidecar / "temp" / "tts"
    temp.mkdir(parents=True)
    owned = temp / "owned.wav"
    owned.write_bytes(b"test")
    other = tmp_path / "outside.wav"
    other.write_bytes(b"must-survive")

    assert [path.name for path in MODULE._safe_tts_temp_files(sidecar)] == ["owned.wav"]
    assert owned.read_bytes() == b"test"
    assert other.read_bytes() == b"must-survive"
    assert not hasattr(MODULE, "cleanup_test_owned_tts_temp")


def test_v14_resource_evidence_tts_uses_queued_terminal_playback_lifecycle() -> None:
    class AudioResponse:
        status_code = 200
        content = _audible_wav()
        headers = {"cache-control": "no-store, max-age=0"}

    class QueueResponse:
        status_code = 200

        @staticmethod
        def json() -> list[dict[str, object]]:
            return []

    class Client:
        def __init__(self) -> None:
            self.audio_urls: list[str] = []
            self.queue_urls: list[str] = []

        def get(self, path: str) -> object:
            if path == "/api/tts/queue":
                self.queue_urls.append(path)
                return QueueResponse()
            self.audio_urls.append(path)
            return AudioResponse()

    class Support:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, object | None]] = []

        def api_json(self, _client: object, method: str, path: str, **kwargs: object) -> object:
            payload = kwargs.get("json")
            self.calls.append((method, path, payload))
            if path == "/api/tts/settings":
                return {"provider": "windows", "cache_enabled": False}
            if path == "/api/tts/speak":
                assert isinstance(payload, dict)
                request_id = str(payload["request_id"])
                return {
                    "request_id": request_id,
                    "status": "QUEUED",
                    "provider": "windows",
                    "audio_url": f"/api/tts/audio/{request_id}",
                    "duration_ms": 100,
                    "synthesis_ms": 2.0,
                    "cache_hit": False,
                }
            if path.endswith("/start"):
                return {"status": "PLAYING"}
            if path.endswith("/complete"):
                return {"status": "COMPLETED"}
            if path == "/api/tts/status":
                return {"status": "IDLE", "queue_length": 0, "current": None, "active_synthesis": []}
            raise AssertionError(f"unexpected TTS route: {method} {path}")

    client = Client()
    support = Support()
    result = MODULE.exercise_windows_tts_lifecycle(client, support)

    payloads = {path: payload for _method, path, payload in support.calls}
    speak_payload = payloads["/api/tts/speak"]
    assert isinstance(speak_payload, dict)
    request_id = str(speak_payload["request_id"])
    assert speak_payload["idempotency_key"] == request_id
    assert result["request_id"] == request_id
    assert result["task_id"] == speak_payload["task_id"]
    assert client.audio_urls == [f"/api/tts/audio/{request_id}"]
    assert client.queue_urls == ["/api/tts/queue"]
    assert [(method, path) for method, path, _payload in support.calls] == [
        ("PUT", "/api/tts/settings"),
        ("POST", "/api/tts/speak"),
        ("POST", f"/api/tts/playback/{request_id}/start"),
        ("POST", f"/api/tts/playback/{request_id}/complete"),
        ("GET", "/api/tts/status"),
    ]
    assert result["playback"] == {
        "queued_status": "QUEUED",
        "started_status": "PLAYING",
        "completed_status": "COMPLETED",
        "queue_after": 0,
        "status_after": "IDLE",
        "active_synthesis_after": 0,
    }


def test_v14_resource_evidence_tts_queue_reader_accepts_only_object_arrays() -> None:
    class ArrayResponse:
        status_code = 200

        @staticmethod
        def json() -> list[dict[str, str]]:
            return [{"request_id": "owned-request"}]

    class ObjectResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, str]:
            return {"request_id": "not-a-queue"}

    class Client:
        def __init__(self, response: object) -> None:
            self.response = response
            self.paths: list[str] = []

        def get(self, path: str) -> object:
            self.paths.append(path)
            return self.response

    array_client = Client(ArrayResponse())
    assert MODULE.read_tts_queue(array_client) == [{"request_id": "owned-request"}]
    assert array_client.paths == ["/api/tts/queue"]
    with pytest.raises(MODULE.ResourceEvidenceError, match="non-array JSON payload"):
        MODULE.read_tts_queue(Client(ObjectResponse()))


def test_v14_resource_evidence_tts_failure_interrupts_only_its_exact_task() -> None:
    class Response:
        status_code = 500
        content = b""
        headers: dict[str, str] = {}

    class Client:
        def get(self, _path: str) -> Response:
            return Response()

    class Support:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, object | None]] = []

        def api_json(self, _client: object, method: str, path: str, **kwargs: object) -> object:
            payload = kwargs.get("json")
            self.calls.append((method, path, payload))
            if path == "/api/tts/settings":
                return {"provider": "windows"}
            if path == "/api/tts/speak":
                assert isinstance(payload, dict)
                request_id = str(payload["request_id"])
                return {
                    "request_id": request_id,
                    "status": "QUEUED",
                    "provider": "windows",
                    "audio_url": f"/api/tts/audio/{request_id}",
                    "duration_ms": 100,
                    "synthesis_ms": 2.0,
                    "cache_hit": False,
                }
            if path == "/api/tts/interrupt":
                return {"status": "CANCELLED", "cleared_queue": 1}
            raise AssertionError(f"unexpected TTS route: {method} {path}")

    support = Support()
    with pytest.raises(MODULE.ResourceEvidenceError, match="audible WAV data"):
        MODULE.exercise_windows_tts_lifecycle(Client(), support)

    payloads = {path: payload for _method, path, payload in support.calls}
    speak_payload = payloads["/api/tts/speak"]
    interrupt_payload = payloads["/api/tts/interrupt"]
    assert isinstance(speak_payload, dict)
    assert interrupt_payload == {"task_id": speak_payload["task_id"]}
    assert all(path != "/api/tts/queue/clear" for _method, path, _payload in support.calls)


def test_v14_resource_evidence_controller_allows_only_narrow_actions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(MODULE.ResourceEvidenceError, match="unknown action"):
        MODULE.run_model_controller(
            tmp_path,
            action="download_qwen",
            ollama_url="http://127.0.0.1:11435",
            timeout_seconds=1,
            service_state_path=tmp_path / "state.json",
            test_owned_service_id="a20-test-owned-ollama-identity-001",
            model_store=tmp_path,
        )

    captured: dict[str, object] = {}

    class _Completed:
        returncode = 0
        stdout = '{"action":"preflight","ok":true}\n'

    def fake_run(command: list[str], **kwargs: object) -> _Completed:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return _Completed()

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)
    # This is a controller protocol fixture, not a repository-venv admission.
    # Hosted CI uses setup-python instead of creating the local desktop venv.
    monkeypatch.setattr(MODULE, "PYTHON", Path(sys.executable))
    result = MODULE.run_model_controller(
        tmp_path,
        action="preflight",
        ollama_url="http://127.0.0.1:11435",
        timeout_seconds=1,
        service_state_path=tmp_path / "state.json",
        test_owned_service_id="a20-test-owned-ollama-identity-001",
        model_store=tmp_path,
    )

    assert result == {"action": "preflight", "ok": True}
    payload = json.loads(captured["environment"]["SIYI_V14_RESOURCE_CONTROLLER"])
    assert payload["action"] == "preflight"
    assert payload["base_url"] == "http://127.0.0.1:11435"
    assert payload["owner_token"] == "a20-test-owned-ollama-identity-001"
    assert payload["owner_sha256"] == MODULE.owner_sha256(payload["owner_token"])


def test_v14_resource_controller_missing_local_runtime_still_blocks_before_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(MODULE, "PYTHON", tmp_path / "missing-python.exe")

    def unexpected_run(*args: object, **kwargs: object) -> object:
        raise AssertionError("missing runtime must not launch a subprocess")

    monkeypatch.setattr(MODULE.subprocess, "run", unexpected_run)
    with pytest.raises(MODULE.ResourceEvidenceError, match="repository Python runtime is unavailable"):
        MODULE.run_model_controller(
            tmp_path, action="preflight", ollama_url="http://127.0.0.1:11435",
            timeout_seconds=1, service_state_path=tmp_path / "state.json",
            test_owned_service_id="a20-test-owned-ollama-identity-001", model_store=tmp_path,
        )


def test_v14_resource_controller_initializes_its_isolated_database_before_model_registry_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The standalone controller has no FastAPI lifespan to initialize SQLite.

    Use a tiny isolated ``app`` package instead of an Ollama process.  Its
    model registry refuses access until the fake ``init_db`` records an
    initialization marker, proving the controller's ordering without talking
    to any system-owned listener or model store.
    """

    fake_siyi_root = tmp_path / "siyi"
    files = {
        "app/__init__.py": "",
        "app/database.py": '''from pathlib import Path
_MARKER = Path(__file__).with_name("database.initialized")
def init_db():
    _MARKER.write_text("initialized", encoding="utf-8")
''',
        "app/local_runtime/__init__.py": "",
        "app/local_runtime/model_manager.py": '''from pathlib import Path
class ModelManagerError(RuntimeError):
    pass
class ModelManager:
    def __init__(self, _base_url):
        pass
    async def running_models(self):
        return []
    async def list_models(self):
        if not Path(__file__).parents[1].joinpath("database.initialized").is_file():
            raise RuntimeError("isolated database was not initialized")
        return [{"name": "qwen3:4b", "size": 1, "loaded": False}]
''',
        "app/local_runtime/ollama_service_manager.py": '''import hashlib
class OllamaServiceError(RuntimeError):
    pass
class OllamaServiceManager:
    def __init__(self, *, base_url, state_path, owner_token, require_owner):
        self.owner_token = owner_token
    async def status(self):
        return {
            "status": "MANAGED_RUNNING",
            "mode": "managed",
            "owner_sha256": hashlib.sha256(self.owner_token.encode("utf-8")).hexdigest(),
            "managed_pid": 4321,
            "managed_port": 11435,
            "listener_pid": 4321,
        }
''',
        "app/local_runtime/resource_coordinator.py": '''class ResourceCoordinator:
    def snapshot(self, *, ollama_pid):
        return {"ollama_pid": ollama_pid}
    def admission_status(self, *, snapshot):
        return {"model_preload": {"allowed": True}}
''',
    }
    for relative, content in files.items():
        destination = fake_siyi_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")

    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    monkeypatch.setattr(MODULE, "PYTHON", Path(sys.executable))
    result = MODULE.run_model_controller(
        tmp_path / "runtime",
        action="preflight",
        ollama_url="http://127.0.0.1:11435",
        timeout_seconds=5,
        service_state_path=tmp_path / "service-state.json",
        test_owned_service_id="a20-test-owned-ollama-identity-001",
        model_store=tmp_path,
    )

    assert result["ok"] is True
    assert result["installed"] == [{"name": "qwen3:4b", "size": 1, "loaded": False}]
    assert (fake_siyi_root / "app" / "database.initialized").is_file()


def test_v14_resource_evidence_controller_refuses_unknown_model_during_owned_cleanup() -> None:
    # The controller is intentionally a subprocess so its app imports observe
    # an isolated runtime.  Keep this guard explicit: the only unload action
    # must reject a new non-qwen resident model rather than swapping it out.
    assert 'action == "unload_owned_qwen"' in MODULE._MODEL_CONTROLLER
    assert "OLLAMA_RUNTIME_CHANGED_UNKNOWN_MODEL" in MODULE._MODEL_CONTROLLER
    assert "await manager.unload(MODEL)" in MODULE._MODEL_CONTROLLER
    assert "download" not in MODULE._MODEL_CONTROLLER.casefold()


def test_v14_resource_evidence_never_serializes_service_paths_or_raw_owner() -> None:
    service = {
        "status": "MANAGED_RUNNING",
        "mode": "managed",
        "owner_sha256": "a" * 64,
        "managed_pid": 101,
        "listener_pid": 101,
        "managed_port": 11435,
        "stopped": True,
        "stopped_pid": 101,
        "managed_executable": chr(67) + r":\Users\admin\AppData\Local\Programs\Ollama\ollama.exe",
        "log_path": chr(67) + r":\Users\admin\logs\ollama.log",
        "executable": chr(67) + r":\Users\admin\AppData\Local\Programs\Ollama\ollama.exe",
        "owner_token": "must-not-escape",
    }
    safe = MODULE.safe_service_summary(service)
    encoded = json.dumps(safe)
    assert safe["owner_sha256"] == "a" * 64
    assert "Users" not in encoded
    assert "must-not-escape" not in encoded
    assert "log_path" not in safe
    assert safe["stopped"] is True
    assert "stopped_pid" not in safe


def test_v14_resource_evidence_requires_complete_test_owned_stop_contract() -> None:
    service = {
        "stopped": True,
        "status": "INSTALLED_STOPPED",
        "api_healthy": False,
        "mode": None,
        "managed_pid": None,
        "listener_pid": None,
        "managed_port": None,
        "managed_started_at": None,
        "owner_sha256": None,
        "managed_state_unowned": False,
    }
    successful = {
        "ok": True,
        "action": "stop_test_owned_service",
        "service": service,
    }

    safe = MODULE.safe_controller_result(successful)
    assert safe["service"]["stopped"] is True
    assert MODULE.successful_test_owned_stop(safe) is True

    assert MODULE.successful_test_owned_stop({**safe, "ok": False}) is False
    assert MODULE.successful_test_owned_stop({**safe, "action": "preflight"}) is False
    assert MODULE.successful_test_owned_stop(
        {**safe, "service": {**safe["service"], "stopped": False}}
    ) is False
    assert MODULE.successful_test_owned_stop(
        {**safe, "service": {**safe["service"], "status": "MANAGED_RUNNING"}}
    ) is False
    assert MODULE.successful_test_owned_stop(
        {**safe, "service": {**safe["service"], "listener_pid": 991}}
    ) is False
    assert MODULE.successful_test_owned_stop(
        {**safe, "service": {**safe["service"], "api_healthy": True}}
    ) is False
    missing_stopped = dict(safe["service"])
    missing_stopped.pop("stopped")
    assert MODULE.successful_test_owned_stop({**safe, "service": missing_stopped}) is False


def test_v14_resource_evidence_model_store_fingerprint_is_path_free(tmp_path: Path) -> None:
    manifest = tmp_path / "manifests" / "registry.ollama.ai" / "library" / "qwen3" / "4b"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("fixture", encoding="utf-8")
    blob = tmp_path / "blobs" / "sha256-fixture"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"model-blob")

    fingerprint = MODULE.ollama_model_store_fingerprint(tmp_path)

    assert fingerprint["mode"] == "non_mutating_api_intent"
    assert fingerprint["regular_file_count"] == 2
    assert fingerprint["regular_file_bytes"] == len(b"fixturemodel-blob")
    assert fingerprint["links_followed"] is False
    assert str(tmp_path) not in json.dumps(fingerprint)

    blob.write_bytes(b"changed-model-blob")
    changed = MODULE.ollama_model_store_fingerprint(tmp_path)
    assert changed["whole_tree_sha256"] != fingerprint["whole_tree_sha256"]


def test_v14_resource_evidence_requires_exactly_one_test_owned_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = MODULE.OllamaProcessIdentity(1, "created", "ollama.exe", "b" * 64)
    report: dict[str, object] = {}
    monkeypatch.setattr(MODULE, "list_ollama_processes", lambda: [expected])
    MODULE.require_only_test_owned_ollama(report, expected=expected, phase="fixture")
    assert report["checks"]["only_test_owned_ollama_fixture"]["passed"] is True

    monkeypatch.setattr(MODULE, "list_ollama_processes", lambda: [expected, MODULE.OllamaProcessIdentity(2, "later", "ollama.exe", "c" * 64)])
    with pytest.raises(MODULE.ResourceEvidenceError, match="only_test_owned_ollama_fixture"):
        MODULE.require_only_test_owned_ollama(report, expected=expected, phase="fixture")


def test_v14_resource_evidence_start_attempt_always_uses_owner_bound_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Support:
        @staticmethod
        def stop_source_sidecar(*_args: object) -> dict[str, object]:
            return {"tree_cleanup": {"tree_terminated": True}}

        @staticmethod
        def safe_remove_isolated_audio(_runtime: Path) -> list[str]:
            return []

    model_store = tmp_path / "ollama-models"
    (model_store / "manifests").mkdir(parents=True)
    (model_store / "manifests" / "fixture").write_text("fixture", encoding="utf-8")
    models_root = tmp_path / "stt-models"
    models_root.mkdir()
    output = tmp_path / "build" / "v1400-evidence" / "raw" / "a20.json"
    actions: list[str] = []

    def controller(_runtime: Path, *, action: str, **_kwargs: object) -> dict[str, object]:
        actions.append(action)
        if action == "start_test_owned_service":
            raise MODULE.ResourceEvidenceError("malformed controller response after start")
        assert action == "stop_test_owned_service"
        return {
            "ok": True,
            "action": action,
            "service": {
                "stopped": True,
                "status": "INSTALLED_STOPPED",
                "api_healthy": False,
                "mode": None,
                "managed_pid": None,
                "listener_pid": None,
                "managed_port": None,
                "managed_started_at": None,
                "owner_sha256": None,
                "managed_state_unowned": False,
            },
        }

    monkeypatch.setattr(MODULE, "ROOT", tmp_path)
    monkeypatch.setattr(MODULE, "load_stt_support", lambda: Support())
    monkeypatch.setattr(MODULE, "list_ollama_processes", lambda: [])
    monkeypatch.setattr(MODULE, "run_model_controller", controller)
    monkeypatch.setattr(
        MODULE,
        "installed_stt_model_metadata",
        lambda _root: {
            "id": "small", "installed_before_run": True, "file_count": 1,
            "size_bytes": 1, "download_operation": "NOT_CALLED",
            "delete_operation": "NOT_CALLED",
        },
    )
    monkeypatch.setattr(
        MODULE,
        "source_identity",
        lambda: {
            "source_version": "13.0.0", "source_commit": "a" * 40,
            "source_tree_fingerprint": "B" * 64, "workspace_clean": False,
        },
    )
    arguments = _arguments(
        allow_test_owned_ollama_start=True,
        test_owned_ollama_service_id="a20-test-owned-ollama-identity-001",
        ollama_models_root=model_store,
        models_root=models_root,
    )

    report = MODULE.run_live_resource_evidence(arguments, output)

    assert report["status"] == "FAIL"
    assert report["actual_run"] is True
    assert actions == ["start_test_owned_service", "stop_test_owned_service"]
    assert report["cleanup"]["test_owned_ollama_start_attempted"] is True
    assert MODULE.successful_test_owned_stop(report["cleanup"]["test_owned_ollama_stop"])
