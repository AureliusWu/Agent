from __future__ import annotations

import importlib.util
import io
import json
import sys
from types import SimpleNamespace
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "v14-stt-privacy-live-evidence.py"
SPEC = importlib.util.spec_from_file_location("v14_stt_privacy_live_evidence", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class _FakeProcess:
    def __init__(self, pid: int = 7123, *, alive: bool = True) -> None:
        self.pid = pid
        self._alive = alive
        self.returncode: int | None = None if alive else 0
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_timeouts: list[float] = []

    def poll(self) -> int | None:
        return None if self._alive else self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None:
            self.wait_timeouts.append(timeout)
        self._alive = False
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._alive = False
        self.returncode = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self._alive = False
        self.returncode = 0


def _source_sidecar_identity(support: object, pid: int = 7123, *, creation_date: str = "20260811000000.000000+480"):
    return support.SourceSidecarIdentity(
        pid=pid,
        creation_date=creation_date,
        executable_path=str(support.PYTHON),
        command_line=f'"{support.PYTHON}" "{support.SERVER}"',
    )


def test_v14_a24_output_cannot_escape_evidence_root(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / "build" / "v1400-evidence").mkdir(parents=True)

    output = MODULE.resolve_output_path(
        "build/v1400-evidence/raw/a24.json",
        repository_root=repository,
    )

    assert output == repository / "build" / "v1400-evidence" / "raw" / "a24.json"
    with pytest.raises(MODULE.PrivacyEvidenceError, match="OUTPUT_OUTSIDE_EVIDENCE_ROOT"):
        MODULE.resolve_output_path("outside.json", repository_root=repository)


def test_v14_a24_scanner_detects_text_audio_and_absolute_path_without_disclosing_values(tmp_path: Path) -> None:
    runtime = tmp_path / "isolated-runtime"
    target = runtime / "logs" / "agent.log"
    target.parent.mkdir(parents=True)
    raw_text = "公开合成中文隐私测试".encode("utf-8")
    raw_audio = b"\x01\x7f\x11\x88" * 24
    absolute_path = bytes((67,)) + b":" + br"\Users\private\voice.wav"
    target.write_bytes(b"prefix " + raw_text + b" middle " + raw_audio + b" " + absolute_path)

    findings = MODULE.scan_regular_file(
        target,
        source="log",
        text_probes=[raw_text],
        audio_markers=[raw_audio],
    )
    safe = MODULE.redacted_findings(findings, runtime)
    serialized = json.dumps(safe, ensure_ascii=False)

    assert {item["kind"] for item in safe} == {"raw_text", "raw_audio", "absolute_path"}
    assert "<isolated-runtime>/logs/agent.log" in serialized
    assert "公开合成中文隐私测试" not in serialized
    assert chr(67) + r":\Users\private" not in serialized
    assert raw_audio.hex() not in serialized
    assert all(len(item["sha256"]) == 64 for item in safe)


def test_v14_a24_scans_uncompressed_diagnostic_entries(tmp_path: Path) -> None:
    runtime = tmp_path / "isolated-runtime"
    archive = runtime / "data" / "diagnostics" / "agent-diagnostics.zip"
    archive.parent.mkdir(parents=True)
    raw_text = "公开合成中文隐私测试".encode("utf-8")
    marker = b"\x10\x20\x30\x40" * 24
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("audit-recent.json", b"safe=" + raw_text + b" audio=" + marker)

    findings, entry_count = MODULE.scan_diagnostic_zip(
        archive,
        text_probes=[raw_text],
        audio_markers=[marker],
    )

    assert entry_count == 1
    assert {item.kind for item in findings} == {"raw_text", "raw_audio"}
    assert all(item.source == "diagnostic_zip_entry" for item in findings)


def test_v14_a24_audio_markers_are_nonempty_fixed_size_and_do_not_expose_audio() -> None:
    header = b"RIFF" + (b"\x00" * 40)
    pcm = bytes(range(1, 193)) * 10

    markers = MODULE.fixed_audio_markers(header + pcm, marker_size=64, count=3)

    assert len(markers) == 3
    assert all(len(marker) == 64 and any(marker) for marker in markers)
    assert all(len(MODULE.sha256_bytes(marker)) == 64 for marker in markers)


def test_v14_a24_audio_markers_skip_leading_and_trailing_silence() -> None:
    header = b"RIFF" + (b"\x00" * 40)
    silent = b"\x00" * 96
    speech_blocks = tuple(bytes([index]) * 96 for index in range(1, 7))

    markers = MODULE.fixed_audio_markers(
        header + silent + b"".join(speech_blocks) + silent,
        marker_size=96,
        count=4,
    )

    assert len(markers) == 4
    assert all(len(marker) == 96 and any(marker) for marker in markers)
    assert markers[0] == speech_blocks[0]
    assert markers[-1] == speech_blocks[-1]


def test_v14_a24_audio_markers_fail_closed_when_speech_is_insufficient() -> None:
    header = b"RIFF" + (b"\x00" * 40)
    pcm = (b"\x00" * 96) + (b"\x01" * 96) + (b"\x00" * 96)

    with pytest.raises(MODULE.PrivacyEvidenceError, match="SYNTHETIC_AUDIO_MARKER_INVALID"):
        MODULE.fixed_audio_markers(header + pcm, marker_size=96, count=2)


def test_v14_a24_report_write_is_immutable(tmp_path: Path) -> None:
    output = tmp_path / "a24.json"
    MODULE.write_json_once(output, {"status": "PASS", "transcript_sha256": "A" * 64})

    with pytest.raises(MODULE.PrivacyEvidenceError, match="OUTPUT_ALREADY_EXISTS"):
        MODULE.write_json_once(output, {"status": "FAIL"})
    assert json.loads(output.read_text(encoding="utf-8"))["transcript_sha256"] == "A" * 64


def test_v14_a24_writer_uses_exclusive_create_and_fsync(
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
    MODULE.write_json_once(tmp_path / "a24-exclusive.json", {"status": "PASS"})

    assert opened_flags and opened_flags[-1] & MODULE.os.O_EXCL
    assert fsync_calls


def test_v14_a24_source_identity_uses_runner_value_and_remains_path_free(
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


def test_v14_a24_sidecar_cleanup_rechecks_exact_identity_before_tree_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = MODULE.load_sidecar_cleanup_support()
    process = _FakeProcess()
    identity = _source_sidecar_identity(support, process.pid)
    killed: list[int] = []
    monkeypatch.setattr(support, "_read_windows_process_identity", lambda pid: identity)
    monkeypatch.setattr(support, "_run_taskkill_process_tree", lambda pid: killed.append(pid) or 0)
    stdout = io.BytesIO()
    stderr = io.BytesIO()

    result = MODULE.stop_sidecar(process, stdout, stderr, identity)

    assert killed == [process.pid]
    assert result["stop_requested"] is True
    assert result["forced"] is True
    assert result["stopped"] is True
    assert result["tree_cleanup"]["identity_confirmed"] is True
    assert result["tree_cleanup"]["tree_terminated"] is True
    assert stdout.closed is True
    assert stderr.closed is True


def test_v14_a24_sidecar_cleanup_refuses_pid_reuse_or_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = MODULE.load_sidecar_cleanup_support()
    process = _FakeProcess()
    captured = _source_sidecar_identity(support, process.pid, creation_date="20260811000000.000000+480")
    reused = _source_sidecar_identity(support, process.pid, creation_date="20260811000001.000000+480")
    killed: list[int] = []
    monkeypatch.setattr(support, "_read_windows_process_identity", lambda pid: reused)
    monkeypatch.setattr(support, "_run_taskkill_process_tree", lambda pid: killed.append(pid) or 0)
    stdout = io.BytesIO()
    stderr = io.BytesIO()

    result = MODULE.stop_sidecar(process, stdout, stderr, captured)

    assert killed == []
    assert result["stop_requested"] is False
    assert result["forced"] is False
    assert result["stopped"] is False
    assert result["tree_cleanup"]["identity_confirmed"] is False
    assert result["tree_cleanup"]["tree_terminated"] is False
    assert result["tree_cleanup"]["reason"] == "source_sidecar_identity_mismatch"
    assert process.poll() is None
    assert stdout.closed is True
    assert stderr.closed is True


def test_v14_a24_sidecar_cleanup_refuses_missing_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = MODULE.load_sidecar_cleanup_support()
    process = _FakeProcess()
    killed: list[int] = []
    monkeypatch.setattr(support, "_run_taskkill_process_tree", lambda pid: killed.append(pid) or 0)

    result = MODULE.stop_sidecar(process, io.BytesIO(), io.BytesIO(), None)

    assert killed == []
    assert result["stop_requested"] is False
    assert result["stopped"] is False
    assert result["tree_cleanup"]["reason"] == "source_sidecar_identity_missing"
    assert MODULE.sidecar_tree_cleanup_succeeded(result) is False


def test_v14_a24_start_fails_closed_without_captured_sidecar_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    process = _FakeProcess()
    # Exercise identity failure independently of the host's venv layout.
    monkeypatch.setattr(MODULE, "PYTHON", Path(sys.executable))
    monkeypatch.setattr(MODULE.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        MODULE,
        "load_sidecar_cleanup_support",
        lambda: SimpleNamespace(capture_source_sidecar_identity=lambda value: None),
    )

    with pytest.raises(MODULE.PrivacyEvidenceError, match="SOURCE_SIDECAR_IDENTITY_UNAVAILABLE"):
        MODULE.start_sidecar(runtime, port=39001, token="test-token")

    assert process.terminate_calls == 1
    assert process.kill_calls == 0


def test_v14_a24_missing_local_runtime_still_blocks_before_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(MODULE, "PYTHON", tmp_path / "missing-python.exe")

    def unexpected_launch(*args: object, **kwargs: object) -> object:
        raise AssertionError("missing runtime must not launch a sidecar")

    monkeypatch.setattr(MODULE.subprocess, "Popen", unexpected_launch)
    with pytest.raises(MODULE.PrivacyEvidenceError, match="SOURCE_SIDECAR_RUNTIME_MISSING"):
        MODULE.start_sidecar(tmp_path, port=39001, token="synthetic-token")


def test_v14_a24_cleanup_success_requires_confirmed_tree_termination() -> None:
    assert MODULE.sidecar_tree_cleanup_succeeded({"tree_cleanup": {"tree_terminated": False}}) is False
    assert MODULE.sidecar_tree_cleanup_succeeded({"tree_cleanup": {"tree_terminated": True}}) is True
