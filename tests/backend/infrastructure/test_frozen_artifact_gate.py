from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-frozen-artifacts.py"
SPEC = importlib.util.spec_from_file_location("frozen_artifact_gate", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_inventory_requires_all_artifact_runtime_groups() -> None:
    listing = "\n".join(
        [
            "app.artifacts.docx",
            "app.artifacts.pdf",
            "app.artifacts.pptx",
            "app.artifacts.render",
            "app.artifacts.service",
            "app.artifacts.validation",
            "docx\\templates\\default.docx",
            "pptx\\templates\\default.pptx",
            "pypdfium2_raw\\pdfium.dll",
            "pypdfium2",
            "lxml.etree",
            "reportlab.pdfgen",
            "PIL.JpegImagePlugin",
            "PIL.PngImagePlugin",
            "PIL.WebPImagePlugin",
            "app.stt.worker",
            "app.stt.providers.faster_whisper",
            "app.stt.pcm_wav",
            "faster_whisper",
            "ctranslate2._ext",
            "ctranslate2.dll",
            "onnxruntime.capi.onnxruntime_pybind11_state",
            "tokenizers.tokenizers",
            "silero_vad_v6.onnx",
        ]
    )
    assert MODULE.inventory_decision(listing)["passed"] is True


def test_inventory_rejects_missing_native_payload_or_node_modules() -> None:
    missing = MODULE.inventory_decision("app.artifacts.docx")
    assert "pdfium_binary" in missing["missing_groups"]
    assert "stt_worker" in missing["missing_groups"]
    assert missing["passed"] is False

    duplicated = MODULE.inventory_decision("node_modules\n" + "\n".join(sum(MODULE.REQUIRED_GROUPS.values(), ())))
    assert duplicated["duplicate_web_payload"] is True
    assert duplicated["passed"] is False

    pyav = MODULE.inventory_decision("av._core\n" + "\n".join(sum(MODULE.REQUIRED_GROUPS.values(), ())))
    assert pyav["pyav_payload"] is True
    assert pyav["passed"] is False

    pyav_package = MODULE.inventory_decision("av/__init__.py\n" + "\n".join(sum(MODULE.REQUIRED_GROUPS.values(), ())))
    assert pyav_package["pyav_payload"] is True
    assert pyav_package["passed"] is False


def test_onedir_inventory_requires_adjacent_internal_payload(tmp_path: Path) -> None:
    binary = tmp_path / "agent-backend.exe"
    binary.write_bytes(b"stub")
    archive = "pyi-contents-directory _internal\n" + "\n".join(
        [
            "app.artifacts.docx",
            "app.artifacts.pdf",
            "app.artifacts.pptx",
            "app.artifacts.render",
            "app.artifacts.service",
            "app.artifacts.validation",
            "docx\\templates\\default.docx",
            "pptx\\templates\\default.pptx",
            "pypdfium2",
            "lxml.etree",
            "reportlab.pdfgen",
            "PIL.JpegImagePlugin",
            "PIL.PngImagePlugin",
            "PIL.WebPImagePlugin",
            "app.stt.worker",
            "app.stt.providers.faster_whisper",
            "app.stt.pcm_wav",
            "faster_whisper",
            "ctranslate2._ext",
            "onnxruntime.capi.onnxruntime_pybind11_state",
            "tokenizers.tokenizers",
            "silero_vad_v6.onnx",
        ]
    )
    missing = MODULE.package_inventory(binary, archive)
    assert missing["package_mode"] == "onedir"
    assert missing["missing_support_directory"] is True
    assert missing["passed"] is False

    support = tmp_path / "_internal"
    support.mkdir()
    for name in ("pdfium.dll", "ctranslate2.dll"):
        (support / name).write_bytes(b"native")
    included = MODULE.package_inventory(binary, archive)
    assert included["missing_support_directory"] is False
    assert included["support_file_count"] == 2
    assert included["passed"] is True


def test_frozen_vad_probe_uses_no_real_settings_and_requires_native_session(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "agent-backend.exe"
    observed = []
    monkeypatch.setenv("AGENT_DEEPSEEK_API_KEY", "synthetic-test-sentinel")

    def run(args, *, cwd, env, capture_output, timeout, check):
        assert args == [str(binary), "--stt-worker", "--mode", "probe"]
        assert Path(cwd).is_relative_to(tmp_path)
        assert env["AGENT_DATA_ROOT"] == cwd
        assert "AGENT_DEEPSEEK_API_KEY" not in env
        assert env["HF_HUB_OFFLINE"] == "1"
        assert capture_output and timeout == 30 and not check
        observed.append(cwd)
        runtime = {**MODULE.EXPECTED_STT_RUNTIME, "vad_session": len(observed) == 1}
        payload = {"event": "probe", "status": "ok", "runtime": runtime}
        return subprocess.CompletedProcess(args, 0, json.dumps(payload).encode(), b"")

    monkeypatch.setattr(MODULE.subprocess, "run", run)
    assert MODULE.probe_stt_runtime(binary, tmp_path)["status"] == "passed"
    assert MODULE.probe_stt_runtime(binary, tmp_path) == {
        "status": "failed", "reason": "missing_runtime_capability"
    }
    assert all(not Path(path).exists() for path in observed)


def test_frozen_vad_probe_fails_on_timeout(tmp_path: Path, monkeypatch) -> None:
    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(MODULE.subprocess, "run", timeout)
    assert MODULE.probe_stt_runtime(tmp_path / "agent-backend.exe", tmp_path) == {
        "status": "failed", "reason": "worker_timeout"
    }


def test_artifact_cli_probes_only_when_opted_in(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "agent-backend.exe"
    binary.write_bytes(b"test stub")
    output = tmp_path / "receipt.json"
    run_calls = []
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(MODULE, "package_inventory", lambda *args: {"passed": True})

    def failed_probe(binary, scratch):
        run_calls.append((binary, scratch))
        return {"status": "failed", "reason": "test_sentinel"}

    monkeypatch.setattr(MODULE, "probe_stt_runtime", failed_probe)
    arguments = [str(SCRIPT), "--binary", str(binary), "--output", str(output)]
    monkeypatch.setattr(sys, "argv", arguments)
    assert MODULE.main() == 0
    assert not run_calls
    assert json.loads(output.read_text(encoding="utf-8"))["stt_runtime_probe"] == {"status": "not_run"}
    monkeypatch.setattr(sys, "argv", [*arguments, "--verify-native-stt"])
    assert MODULE.main() == 1
    assert run_calls == [(binary.resolve(), tmp_path.resolve())]
    assert json.loads(output.read_text(encoding="utf-8"))["stt_runtime_probe"]["status"] == "failed"
