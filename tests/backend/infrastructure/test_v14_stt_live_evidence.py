from __future__ import annotations

import importlib.util
import io
import json
import sys
import wave
from array import array
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "v14-stt-live-evidence.py"
SPEC = importlib.util.spec_from_file_location("v14_stt_live_evidence", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class _FakeProcess:
    def __init__(self, pid: int, *, alive: bool = True) -> None:
        self.pid = pid
        self._alive = alive
        self.returncode: int | None = None if alive else 0
        self.wait_timeouts: list[float] = []

    def poll(self) -> int | None:
        return None if self._alive else self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None:
            self.wait_timeouts.append(timeout)
        self._alive = False
        self.returncode = 0
        return 0


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload


class _SmallDownloadApi:
    def __init__(self, models_root: Path) -> None:
        self.models_root = models_root
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        self.status_reads = 0

    def get(self, path: str) -> _FakeResponse:
        self.calls.append(("GET", path, None))
        if path.endswith("/download-preview"):
            return _FakeResponse(
                200,
                {
                    "model": "small",
                    "repo_id": "Systran/faster-whisper-small",
                    "estimated_bytes": 462_000_000,
                    "target_directory": str(self.models_root / "small"),
                    "already_installed": False,
                    "available_bytes": 2_000_000_000,
                    "required_bytes": 600_000_000,
                    "fits": True,
                },
            )
        if path.endswith("/download"):
            self.status_reads += 1
            if self.status_reads == 1:
                return _FakeResponse(
                    200,
                    {"model": "small", "status": "DOWNLOADING", "completed": 123, "total": 462_000_000, "error": None},
                )
            target = self.models_root / "small"
            target.mkdir(parents=True, exist_ok=False)
            (target / "config.json").write_bytes(b'{"model_type":"whisper"}')
            (target / "model.bin").write_bytes(b"downloaded-small-model")
            actual = sum(item.stat().st_size for item in target.rglob("*") if item.is_file())
            return _FakeResponse(
                200,
                {"model": "small", "status": "INSTALLED", "completed": actual, "total": actual, "error": None},
            )
        raise AssertionError(path)

    def post(self, path: str, *, json: dict[str, object]) -> _FakeResponse:
        self.calls.append(("POST", path, json))
        if json == {"model_id": "small", "confirmed": False}:
            return _FakeResponse(
                409,
                {"detail": {"code": "STT_DOWNLOAD_CONFIRMATION_REQUIRED", "message": "confirmation required"}},
            )
        if json == {"model_id": "small", "confirmed": True}:
            return _FakeResponse(
                202,
                {"model": "small", "status": "DOWNLOADING", "completed": 0, "total": 462_000_000, "error": None},
            )
        raise AssertionError(json)


def _source_sidecar_identity(pid: int = 7123, *, creation_date: str = "20260811000000.000000+480"):
    return MODULE.SourceSidecarIdentity(
        pid=pid,
        creation_date=creation_date,
        executable_path=str(MODULE.PYTHON),
        command_line=f'"{MODULE.PYTHON}" "{MODULE.SERVER}"',
    )


def test_v14_stt_live_output_cannot_escape_the_evidence_root(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    evidence = root / "build" / "v1400-evidence"
    evidence.mkdir(parents=True)

    assert MODULE.resolve_output_path("build/v1400-evidence/raw/stt.json", repository_root=root) == (
        evidence / "raw" / "stt.json"
    )
    with pytest.raises(MODULE.LiveEvidenceError, match="build/v1400-evidence"):
        MODULE.resolve_output_path("outside.json", repository_root=root)


def test_v14_stt_live_download_mode_requires_the_explicit_cli_switch() -> None:
    default = MODULE.parse_args(["--output", "build/v1400-evidence/raw/default.json"])
    confirmed = MODULE.parse_args(
        [
            "--output",
            "build/v1400-evidence/raw/small.json",
            "--download-small-confirmed",
            "--download-only",
        ]
    )
    installed_small = MODULE.parse_args(
        [
            "--output",
            "build/v1400-evidence/raw/installed-small.json",
            "--model-id",
            "small",
        ]
    )

    assert default.download_small_confirmed is False
    assert confirmed.download_small_confirmed is True
    assert confirmed.download_only is True
    assert installed_small.model_id == "small"
    assert default.download_timeout_seconds == 1800.0


def test_v14_stt_live_download_mode_cannot_mix_a09_with_transcription() -> None:
    with pytest.raises(SystemExit):
        MODULE.parse_args(
            [
                "--output",
                "build/v1400-evidence/raw/mixed-scope.json",
                "--download-small-confirmed",
            ]
        )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"status": "CANCEL_REQUESTED", "cancelled": 1, "settled": False}, True),
        ({"status": "CANCELLED", "cancelled": 1, "settled": True}, True),
        ({"status": "CANCELLED", "cancelled": 0, "settled": True}, False),
        ({"status": "CANCEL_REQUESTED", "cancelled": 1, "settled": True}, False),
        ({"status": "CANCELLED", "cancelled": 1, "settled": False}, False),
    ],
)
def test_v14_stt_live_requires_a_targeted_and_consistent_cancel_response(
    payload: dict[str, object], expected: bool
) -> None:
    assert MODULE.targeted_cancellation_response(payload) is expected


def test_v14_stt_live_uses_exactly_one_observed_active_request_for_cancellation() -> None:
    assert MODULE.active_request_id_for_cancellation(
        {"status": "TRANSCRIBING", "active_requests": ["a" * 32]}
    ) == "a" * 32

    with pytest.raises(MODULE.LiveEvidenceError, match="exactly one active request"):
        MODULE.active_request_id_for_cancellation(
            {"status": "TRANSCRIBING", "active_requests": ["a" * 32, "b" * 32]}
        )
    with pytest.raises(MODULE.LiveEvidenceError, match="invalid active request ID"):
        MODULE.active_request_id_for_cancellation(
            {"status": "TRANSCRIBING", "active_requests": [""]}
        )
    with pytest.raises(MODULE.LiveEvidenceError, match="invalid active request ID"):
        MODULE.active_request_id_for_cancellation(
            {"status": "TRANSCRIBING", "active_requests": [None]}
        )


def test_v14_stt_live_cancellation_uses_the_observed_request_and_accepts_terminal_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = "r" * 32
    cancel_calls: list[dict[str, object]] = []

    def fake_upload(
        _endpoint: str,
        _headers: dict[str, str],
        _voice_session_id: str,
        _audio_path: Path,
        sink: dict[str, object],
    ) -> None:
        sink["response"] = {
            "status_code": 409,
            "payload": {"detail": {"code": "STT_ALREADY_CANCELLED"}},
        }

    class _ImmediateThread:
        def __init__(self, *, target: object, args: tuple[object, ...], **_kwargs: object) -> None:
            self.target = target
            self.args = args

        def start(self) -> None:
            self.target(*self.args)  # type: ignore[operator]

        def join(self, timeout: float | None = None) -> None:
            assert timeout == 25.0

        def is_alive(self) -> bool:
            return False

    def fake_api_json(
        _client: object, method: str, path: str, **kwargs: object
    ) -> dict[str, object]:
        if (method, path) == ("POST", "/api/stt/cancel"):
            cancel_calls.append(kwargs["json"])  # type: ignore[arg-type]
            return {"status": "CANCELLED", "cancelled": 1, "settled": True}
        assert (method, path) == ("GET", "/api/stt/status")
        return {"status": "READY", "active_requests": [], "worker_pid": None}

    monkeypatch.setattr(MODULE, "create_voice_session", lambda *_args: "v" * 32)
    monkeypatch.setattr(MODULE, "_upload_in_background", fake_upload)
    monkeypatch.setattr(MODULE.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        MODULE,
        "wait_for_active_transcription",
        lambda *_args, **_kwargs: {"status": "TRANSCRIBING", "active_requests": [request_id]},
    )
    monkeypatch.setattr(MODULE, "api_json", fake_api_json)

    result = MODULE.run_cancellation(
        object(),
        endpoint="http://127.0.0.1:1",
        headers={},
        conversation_id=1,
        sample={"path": "synthetic.wav"},
        cancel_threshold_ms=500.0,
    )

    assert cancel_calls == [{"request_id": request_id}]
    assert result["request_id"] == request_id
    assert result["cancel_response"] == {
        "status": "CANCELLED",
        "cancelled": 1,
        "settled": True,
    }
    assert result["upload_http_status"] == 409
    assert result["upload_error_code"] == "STT_ALREADY_CANCELLED"


def _write_prior_download_receipt(
    repository_root: Path,
    models_root: Path,
) -> tuple[Path, Path]:
    small = models_root / "small"
    small.mkdir(parents=True)
    (small / "config.json").write_bytes(b'{"model_type":"whisper"}')
    (small / "model.bin").write_bytes(b"small-model")
    manifest = MODULE.model_file_manifest(models_root, model_id="small")
    source = {
        "source_version": "13.0.0",
        "source_commit": "a" * 40,
        "source_tree_fingerprint": "B" * 64,
        "workspace_clean": False,
    }
    checks = {
        name: {"passed": True}
        for name in (
            "small_download_target_absent_before_run",
            "confirmed_small_download_via_formal_http_api",
            "cpu_int8_configuration",
            "isolated_sidecar_detects_formal_model",
            "real_cpu_int8_model_load",
            "real_explicit_model_unload",
            "stt_unload_memory_release_observation",
        )
    }
    raw_payload = {
        "schema_version": 1,
        "report_type": "v14_stt_live_evidence",
        "producer": "scripts/v14-stt-live-evidence.py",
        "target_version": "14.0.0",
        "status": "PASS",
        "actual_run": True,
        "source": source,
        "checks": checks,
        "scope": {
            "model_download": "CALLED",
            "user_confirmation": True,
            "download_only": True,
            "model_delete": "NOT_CALLED",
        },
        "model": {
            "id": "small",
            "installed_before_run": False,
            "installed_after_run": True,
            "download_operation": "CALLED",
            "user_confirmation": True,
            "delete_operation": "NOT_CALLED",
        },
        "results": {
            "model_download": {
                "preview": {
                    "model": "small",
                    "repo_id": "Systran/faster-whisper-small",
                    "estimated_bytes": 100,
                    "available_bytes": 1_000,
                    "required_bytes": 200,
                    "fits": True,
                    "already_installed": False,
                },
                "unconfirmed_request": {
                    "confirmed": False,
                    "http_status": 409,
                    "error_code": "STT_DOWNLOAD_CONFIRMATION_REQUIRED",
                    "download_started": False,
                },
                "confirmed_request": {
                    "confirmed": True,
                    "called": True,
                    "http_status": 202,
                    "response": {"status": "DOWNLOADING"},
                },
                "final_state": {
                    "model": "small",
                    "status": "INSTALLED",
                    "completed_bytes": manifest["actual_bytes"],
                    "total_bytes": manifest["actual_bytes"],
                    "error": None,
                },
                "final_model": manifest,
            }
        },
    }
    raw = repository_root / "build" / "v1400-evidence" / "raw" / "prior-a09.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(json.dumps(raw_payload, sort_keys=True) + "\n", encoding="utf-8")
    raw_bytes = raw.read_bytes()
    relative = raw.relative_to(repository_root).as_posix()
    attachment = {
        "path": relative,
        "sha256": MODULE.hashlib.sha256(raw_bytes).hexdigest().upper(),
        "bytes": len(raw_bytes),
        "status": "PASS",
        "actual_run": True,
        "target_version": "14.0.0",
        "report_type": "v14_stt_live_evidence",
        "producer": "scripts/v14-stt-live-evidence.py",
        "source_identity_mode": "raw_report",
        **source,
    }
    envelope_payload = {
        "schema_version": 4,
        "report_type": "v14_execution",
        "producer": "v14-evidence-runner",
        "target_version": "14.0.0",
        "status": "PASS",
        "actual_run": True,
        "case_ids": ["A09"],
        "command": "python scripts/v14-stt-live-evidence.py --download-small-confirmed --download-only",
        "command_contract": {
            "kind": "repository_script",
            "paths": ["scripts/v14-stt-live-evidence.py"],
        },
        "execution": {
            "actual_run": True,
            "status": "PASS",
            "exit_code": 0,
            "timed_out": False,
        },
        "attested_outputs": [attachment],
        **source,
    }
    envelope = repository_root / "build" / "v1400-evidence" / "executions" / "prior-a09.json"
    envelope.parent.mkdir(parents=True)
    envelope.write_text(json.dumps(envelope_payload, sort_keys=True) + "\n", encoding="utf-8")
    return raw, envelope


def test_v14_stt_live_receipt_mode_is_separate_from_download_mode() -> None:
    parsed = MODULE.parse_args(
        [
            "--output",
            "build/v1400-evidence/raw/current-a09.json",
            "--verify-download-receipt",
            "build/v1400-evidence/raw/prior-a09.json",
            "--receipt-envelope",
            "build/v1400-evidence/executions/prior-a09.json",
        ]
    )

    assert parsed.verify_download_receipt == Path("build/v1400-evidence/raw/prior-a09.json")
    assert parsed.receipt_envelope == Path("build/v1400-evidence/executions/prior-a09.json")
    assert parsed.download_small_confirmed is False
    with pytest.raises(SystemExit):
        MODULE.parse_args(
            [
                "--output",
                "build/v1400-evidence/raw/invalid.json",
                "--verify-download-receipt",
                "build/v1400-evidence/raw/prior-a09.json",
                "--download-small-confirmed",
                "--download-only",
            ]
        )


def test_v14_stt_live_reverifies_prior_runner_binding_and_installed_manifest(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    models_root = tmp_path / "models"
    raw, envelope = _write_prior_download_receipt(repository_root, models_root)

    receipt = MODULE.verify_prior_small_download_receipt(
        raw,
        envelope,
        models_root,
        repository_root=repository_root,
    )

    assert receipt["mode"] == "VERIFIED_PRIOR_ACTUAL"
    assert receipt["runner_binding_verified"] is True
    assert receipt["formal_confirmation_flow_verified"] is True
    assert receipt["model_manifest_exact_match"] is True
    assert receipt["current_model_manifest"]["file_count"] == 2


def test_v14_stt_live_receipt_reverification_rejects_tamper_or_model_drift(tmp_path: Path) -> None:
    first_root = tmp_path / "first-repository"
    first_models = tmp_path / "first-models"
    raw, envelope = _write_prior_download_receipt(first_root, first_models)
    envelope_payload = json.loads(envelope.read_text(encoding="utf-8"))
    envelope_payload["attested_outputs"][0]["sha256"] = "0" * 64
    envelope.write_text(json.dumps(envelope_payload, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(MODULE.LiveEvidenceError, match="no longer binds"):
        MODULE.verify_prior_small_download_receipt(
            raw, envelope, first_models, repository_root=first_root
        )

    second_root = tmp_path / "second-repository"
    second_models = tmp_path / "second-models"
    raw, envelope = _write_prior_download_receipt(second_root, second_models)
    (second_models / "small" / "model.bin").write_bytes(b"changed-model")
    with pytest.raises(MODULE.LiveEvidenceError, match="no longer matches"):
        MODULE.verify_prior_small_download_receipt(
            raw, envelope, second_models, repository_root=second_root
        )


def test_v14_stt_live_download_helper_refuses_without_user_confirmation(tmp_path: Path) -> None:
    models_root = tmp_path / "formal-models"
    models_root.mkdir()

    class _NoApiCalls:
        def get(self, _path: str) -> _FakeResponse:
            raise AssertionError("API must not be called without explicit confirmation")

        def post(self, _path: str, *, json: dict[str, object]) -> _FakeResponse:
            raise AssertionError(json)

    evidence: dict[str, object] = {}
    with pytest.raises(MODULE.LiveEvidenceError, match="explicit CLI authorization"):
        MODULE.download_small_model_via_api(
            _NoApiCalls(),
            models_root,
            user_confirmed=False,
            timeout_seconds=1,
            evidence=evidence,
        )

    assert evidence["user_confirmation"] is False
    assert evidence["download_operation"] == "NOT_CALLED"


def test_v14_stt_live_download_refuses_any_existing_small_path(tmp_path: Path) -> None:
    models_root = tmp_path / "formal-models"
    models_root.mkdir()
    existing = models_root / "small"
    existing.write_bytes(b"do-not-overwrite")

    with pytest.raises(MODULE.LiveEvidenceError, match="already exists"):
        MODULE.require_absent_download_target(models_root)

    assert existing.read_bytes() == b"do-not-overwrite"


@pytest.mark.parametrize(
    "name",
    [".small.downloading", f".small.{'b' * 32}.downloading"],
)
def test_v14_stt_live_download_refuses_any_existing_small_staging_path(
    tmp_path: Path,
    name: str,
) -> None:
    models_root = tmp_path / "formal-models"
    models_root.mkdir()
    staging = models_root / name
    staging.mkdir()

    with pytest.raises(MODULE.LiveEvidenceError, match="staging path already exists"):
        MODULE.require_absent_download_target(models_root)

    assert staging.is_dir()


def test_v14_stt_live_rejects_download_preview_outside_formal_models_root(tmp_path: Path) -> None:
    models_root = tmp_path / "formal-models"
    models_root.mkdir()

    class _EscapingPreview(_SmallDownloadApi):
        def get(self, path: str) -> _FakeResponse:
            response = super().get(path)
            if path.endswith("/download-preview"):
                response._payload["target_directory"] = str(tmp_path / "outside" / "small")
            return response

    client = _EscapingPreview(models_root)
    evidence: dict[str, object] = {}

    with pytest.raises(MODULE.LiveEvidenceError, match="expected managed target"):
        MODULE.download_small_model_via_api(
            client,
            models_root,
            user_confirmed=True,
            timeout_seconds=1,
            evidence=evidence,
        )

    assert evidence["download_operation"] == "NOT_CALLED"
    assert all(method != "POST" for method, _path, _payload in client.calls)


def test_v14_stt_live_calls_formal_small_download_routes_and_hashes_final_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    models_root = tmp_path / "formal-models"
    models_root.mkdir()
    client = _SmallDownloadApi(models_root)
    evidence: dict[str, object] = {}
    monkeypatch.setattr(MODULE.time, "sleep", lambda _seconds: None)

    result = MODULE.download_small_model_via_api(
        client,
        models_root,
        user_confirmed=True,
        timeout_seconds=1,
        evidence=evidence,
    )

    assert result is evidence
    assert evidence["user_confirmation"] is True
    assert evidence["download_operation"] == "CALLED"
    assert evidence["preview"]["estimated_bytes"] == 462_000_000
    assert evidence["unconfirmed_request"] == {
        "confirmed": False,
        "http_status": 409,
        "error_code": "STT_DOWNLOAD_CONFIRMATION_REQUIRED",
        "download_started": False,
    }
    assert evidence["confirmed_request"]["http_status"] == 202
    assert [item[0:2] for item in client.calls] == [
        ("GET", "/api/stt/models/small/download-preview"),
        ("POST", "/api/stt/models/download"),
        ("POST", "/api/stt/models/download"),
        ("GET", "/api/stt/models/small/download"),
        ("GET", "/api/stt/models/small/download"),
    ]
    assert [item["status"] for item in evidence["progress"]] == ["DOWNLOADING", "INSTALLED"]
    final = evidence["final_model"]
    assert final["file_count"] == 2
    assert final["actual_bytes"] == evidence["final_state"]["completed_bytes"]
    assert {item["path"] for item in final["files"]} == {"config.json", "model.bin"}
    assert all(len(item["sha256"]) == 64 for item in final["files"])
    assert (models_root / "small" / "model.bin").read_bytes() == b"downloaded-small-model"


def test_v14_stt_live_settles_an_inflight_download_before_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    models_root = tmp_path / "formal-models"
    models_root.mkdir()
    staging = models_root / f".small.{'a' * 32}.downloading"
    staging.mkdir()

    class _SettlementApi:
        def __init__(self) -> None:
            self.states = iter(("CANCEL_REQUESTED", "CANCELLED"))
            self.cancel_calls = 0

        def post(self, path: str, *, json: dict[str, object]) -> _FakeResponse:
            assert path == "/api/stt/models/download/cancel"
            assert json == {"model_id": "small"}
            self.cancel_calls += 1
            return _FakeResponse(200, {"model": "small", "status": "CANCEL_REQUESTED"})

        def get(self, path: str) -> _FakeResponse:
            assert path == "/api/stt/models/small/download"
            status = next(self.states)
            if status == "CANCELLED":
                staging.rmdir()
            return _FakeResponse(
                200,
                {"model": "small", "status": status, "completed": 0, "total": 1, "error": None},
            )

    monkeypatch.setattr(MODULE.time, "sleep", lambda _seconds: None)
    client = _SettlementApi()
    result = MODULE.settle_small_download_before_shutdown(
        client,
        models_root,
        {"download_operation": "CALLED"},
        timeout_seconds=1,
    )

    assert client.cancel_calls == 1
    assert result["terminal_status"] == "CANCELLED"
    assert result["settled"] is True
    assert result["owned_staging_paths_remaining"] == []


def test_v14_stt_live_preserves_an_installed_small_model_during_settlement(tmp_path: Path) -> None:
    models_root = tmp_path / "formal-models"
    installed = models_root / "small"
    installed.mkdir(parents=True)
    (installed / "config.json").write_text("{}", encoding="utf-8")

    class _NoCalls:
        def post(self, *_args: object, **_kwargs: object) -> _FakeResponse:
            raise AssertionError("installed model must not be cancelled")

        def get(self, *_args: object, **_kwargs: object) -> _FakeResponse:
            raise AssertionError("installed model must not be polled")

    result = MODULE.settle_small_download_before_shutdown(
        _NoCalls(),
        models_root,
        {"download_operation": "CALLED", "final_state": {"status": "INSTALLED"}},
    )

    assert result["required"] is False
    assert result["terminal_status"] == "INSTALLED"
    assert (installed / "config.json").is_file()


def test_v14_stt_live_model_link_never_deletes_the_formal_model(tmp_path: Path) -> None:
    formal_models = tmp_path / "formal-models"
    base = formal_models / "base"
    base.mkdir(parents=True)
    (base / "config.json").write_text(json.dumps({"model_type": "whisper"}), encoding="utf-8")
    (base / "model.bin").write_bytes(b"test-model")
    runtime = tmp_path / "isolated-runtime"

    link, _kind = MODULE.create_models_link(runtime, formal_models)
    assert (link / "base" / "config.json").is_file()

    MODULE.remove_models_link(link, formal_models)

    assert not link.exists()
    assert (base / "config.json").is_file()
    assert (base / "model.bin").read_bytes() == b"test-model"


def test_v14_stt_live_normalizes_synthetic_wav_to_fixed_pcm_duration(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8_000)
        handle.writeframes((b"\x01\x00\xfe\xff") * 4_000)
    output = tmp_path / "normalized.wav"

    metadata = MODULE.write_fixed_duration_wav(source, output, seconds=3)

    with wave.open(str(output), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16_000
        assert handle.getnframes() == 48_000
    assert metadata["duration_ms"] == 3_000
    assert metadata["sample_rate"] == 16_000


def test_v14_stt_live_accuracy_review_reports_real_character_edits() -> None:
    review = MODULE.character_accuracy_review(
        "司忆 API 日志。",
        "私意 API 日志。",
    )

    assert review["machine_assisted_only"] is True
    assert review["manual_accuracy_review_required"] is True
    assert review["edit_distance"] == 2
    assert review["cer"] > 0
    assert any(item["operation"] == "replace" for item in review["character_differences"])


def test_v14_stt_live_noise_overlay_is_deterministic_and_bounded(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(array("h", [1200] * 1600).tobytes())
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"

    first_meta = MODULE.write_deterministic_noise_overlay(source, first)
    second_meta = MODULE.write_deterministic_noise_overlay(source, second)

    assert first.read_bytes() == second.read_bytes()
    assert first_meta == second_meta
    with wave.open(str(first), "rb") as handle:
        samples = array("h", handle.readframes(handle.getnframes()))
    assert max(abs(value - 1200) for value in samples) <= 192


def test_v14_stt_live_creates_test_owned_corpus_before_sapi_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    synthesized_parent_states: list[bool] = []

    def fake_synthesize(_text: str, output: Path, _voice_name: str) -> None:
        synthesized_parent_states.append(output.parent.is_dir())
        output.write_bytes(b"synthetic-source")

    def fake_normalize(source: Path, output: Path, *, seconds: int) -> dict[str, int]:
        assert source.is_file()
        output.write_bytes(b"normalized")
        return {"duration_ms": seconds * 1_000, "sample_rate": 16_000, "channels": 1, "sample_width": 2, "bytes": 10}

    def fake_silence(output: Path) -> dict[str, int]:
        output.write_bytes(b"silence")
        return {"duration_ms": 5_000, "sample_rate": 16_000, "channels": 1, "sample_width": 2, "bytes": 7}

    def fake_noise(source: Path, output: Path) -> dict[str, int]:
        assert source.is_file()
        output.write_bytes(b"deterministic-noise")
        return {
            "duration_ms": 8_000,
            "sample_rate": 16_000,
            "channels": 1,
            "sample_width": 2,
            "bytes": 19,
            "deterministic_noise_peak": 192,
        }

    monkeypatch.setattr(MODULE, "chinese_windows_voice", lambda: "test-zh-cn-voice")
    monkeypatch.setattr(MODULE, "_powershell_synthesize", fake_synthesize)
    monkeypatch.setattr(MODULE, "write_fixed_duration_wav", fake_normalize)
    monkeypatch.setattr(MODULE, "write_silence_wav", fake_silence)
    monkeypatch.setattr(MODULE, "write_deterministic_noise_overlay", fake_noise)

    samples, voice = MODULE.build_synthetic_corpus(tmp_path / "isolated-runtime")

    assert voice == "test-zh-cn-voice"
    assert synthesized_parent_states == [True, True, True, True]
    assert set(samples) == {
        "chinese_5s",
        "chinese_15s",
        "chinese_30s",
        "mixed_5s",
        "proper_nouns_8s",
        "noise_pause_8s",
        "blank_5s",
    }
    categories = {
        category
        for sample in samples.values()
        for category in sample.get("acceptance_categories", [])
    }
    assert {"siyi", "natsume", "background_noise", "pause", "file_name"} <= categories


def test_v14_stt_live_evidence_output_is_immutable(tmp_path: Path) -> None:
    output = tmp_path / "raw.json"
    MODULE.write_json_once(output, {"status": "PASS"})

    with pytest.raises(MODULE.LiveEvidenceError, match="refusing to overwrite"):
        MODULE.write_json_once(output, {"status": "FAIL"})
    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "PASS"}


def test_v14_stt_live_writer_uses_exclusive_create_and_fsync(
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
    MODULE.write_json_once(tmp_path / "exclusive.json", {"status": "PASS"})

    assert opened_flags and opened_flags[-1] & MODULE.os.O_EXCL
    assert fsync_calls


def test_v14_stt_live_source_identity_uses_runner_value_without_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = {
        "source_version": "13.0.0",
        "source_commit": "a" * 40,
        "source_tree_fingerprint": "B" * 64,
        "workspace_clean": False,
    }
    monkeypatch.setenv("SIYI_V14_EVIDENCE_SOURCE_IDENTITY", json.dumps(identity))

    assert MODULE.source_identity() == identity
    assert chr(67) + ":\\" not in json.dumps(MODULE.source_identity())


def test_v14_stt_live_process_identity_decodes_windows_ansi_json_without_replacement() -> None:
    """Regression: WMI may emit Chinese source paths in the active ANSI code page."""

    prefix = chr(68) + r":\AI项目\Agent\siyi"
    expected = {
        "ProcessId": 7123,
        "CreationDate": "20260811000000.000000+480",
        "ExecutablePath": prefix + r"\.venv\Scripts\python.exe",
        "CommandLine": f'"{prefix}\\.venv\\Scripts\\python.exe" "{prefix}\\run_server.py"',
    }
    raw = json.dumps(expected, ensure_ascii=False).encode("gbk")

    decoded = MODULE._decode_windows_process_json(raw, ansi_encoding="gbk")

    assert decoded == expected
    assert "\ufffd" not in json.dumps(decoded, ensure_ascii=False)


def test_v14_stt_live_process_identity_rejects_lossy_or_invalid_json_bytes() -> None:
    assert MODULE._decode_windows_process_json(b'{"ExecutablePath":"\xff"}', ansi_encoding="gbk") is None
    assert MODULE._decode_windows_process_json('{"ExecutablePath":"\ufffd"}') is None
    assert MODULE._decode_windows_process_json(b'{"ExecutablePath":"\\ufffd"}') is None
    assert MODULE._decode_windows_process_json(b'{"ExecutablePath":"\\ud800"}') is None


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "gbk", "cp1252"])
def test_v14_process_json_uses_the_declared_code_page_not_the_test_host(encoding: str) -> None:
    expected = {"ExecutablePath": "synthetic-café" if encoding == "cp1252" else "synthetic-中文"}
    raw = json.dumps(expected, ensure_ascii=False).encode(encoding)
    assert MODULE._decode_windows_process_json(raw, ansi_encoding=encoding) == expected


def test_v14_current_process_json_is_strict_utf8_without_ansi_fallback() -> None:
    raw = json.dumps({"ExecutablePath": "synthetic-中文"}, ensure_ascii=False).encode("gbk")
    assert MODULE._decode_windows_process_json(raw, ansi_encoding=None) is None


def test_v14_process_identity_capture_emits_and_requires_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    if MODULE.os.name != "nt":
        pytest.skip("actual Windows identity reader protocol")
    expected = _source_sidecar_identity(7123)
    payload = {"ProcessId": expected.pid, "CreationDate": expected.creation_date,
               "ExecutablePath": expected.executable_path, "CommandLine": expected.command_line}
    captured: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> object:
        captured.append(command[-1])
        assert kwargs["text"] is False
        return type("Completed", (), {"returncode": 0, "stdout": json.dumps(payload, ensure_ascii=False).encode("utf-8")})()

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)
    assert MODULE._read_windows_process_identity(expected.pid) == expected
    assert "[Text.Encoding]::UTF8.GetBytes($json)" in captured[0]
    assert "OpenStandardOutput().Write" in captured[0]


def test_v14_stt_live_captures_only_the_expected_source_sidecar_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess(7123)
    identity = _source_sidecar_identity(process.pid)
    monkeypatch.setattr(MODULE, "_read_windows_process_identity", lambda pid: identity)

    captured = MODULE.capture_source_sidecar_identity(process, attempts=1)

    assert captured == identity

    wrong_entrypoint = MODULE.SourceSidecarIdentity(
        pid=process.pid,
        creation_date=identity.creation_date,
        executable_path=identity.executable_path,
        command_line=f'"{MODULE.PYTHON}" -m unrelated.module',
    )
    monkeypatch.setattr(MODULE, "_read_windows_process_identity", lambda pid: wrong_entrypoint)
    assert MODULE.capture_source_sidecar_identity(process, attempts=1) is None


def test_v14_stt_live_tree_cleanup_rechecks_identity_before_taskkill(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess(7123)
    identity = _source_sidecar_identity(process.pid)
    monkeypatch.setattr(MODULE, "_read_windows_process_identity", lambda pid: identity)
    taskkill_pids: list[int] = []
    monkeypatch.setattr(
        MODULE,
        "_run_taskkill_process_tree",
        lambda pid: taskkill_pids.append(pid) or 0,
    )
    stderr_handle = io.BytesIO()

    status = MODULE.stop_source_sidecar(process, stderr_handle, identity)

    assert taskkill_pids == [process.pid]
    assert status["stopped"] is True
    assert status["tree_cleanup"] == {
        "attempted": True,
        "identity_confirmed": True,
        "tree_terminated": True,
        "taskkill_exit_code": 0,
    }
    assert process.wait_timeouts == [8]
    assert stderr_handle.closed is True


def test_v14_stt_live_tree_cleanup_refuses_pid_reuse_or_identity_change(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess(7123)
    captured = _source_sidecar_identity(process.pid, creation_date="20260811000000.000000+480")
    reused = _source_sidecar_identity(process.pid, creation_date="20260811120000.000000+480")
    monkeypatch.setattr(MODULE, "_read_windows_process_identity", lambda pid: reused)
    taskkill_pids: list[int] = []
    monkeypatch.setattr(
        MODULE,
        "_run_taskkill_process_tree",
        lambda pid: taskkill_pids.append(pid) or 0,
    )

    result = MODULE.terminate_confirmed_source_sidecar_tree(process, captured)

    assert taskkill_pids == []
    assert result == {
        "attempted": False,
        "identity_confirmed": False,
        "tree_terminated": False,
        "reason": "source_sidecar_identity_mismatch",
    }
    assert process.poll() is None


@pytest.mark.skipif(MODULE.os.name != "nt", reason="taskkill process-tree behavior is Windows-specific")
def test_v14_stt_live_taskkill_uses_only_confirmed_pid_tree_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class _Completed:
        returncode = 0

    def fake_run(argv: list[str], **kwargs: object) -> _Completed:
        calls.append((argv, kwargs))
        return _Completed()

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)

    assert MODULE._run_taskkill_process_tree(7123) == 0
    assert calls[0][0] == ["taskkill.exe", "/PID", "7123", "/T", "/F"]
    assert calls[0][1]["check"] is False
