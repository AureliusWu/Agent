from __future__ import annotations

import importlib.util
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
            "onnxruntime.dll",
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
    for name in ("pdfium.dll", "ctranslate2.dll", "onnxruntime.dll"):
        (support / name).write_bytes(b"native")
    included = MODULE.package_inventory(binary, archive)
    assert included["missing_support_directory"] is False
    assert included["support_file_count"] == 3
    assert included["passed"] is True
