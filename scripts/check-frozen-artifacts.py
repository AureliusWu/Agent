from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


REQUIRED_GROUPS = {
    "artifact_engine": (
        "app.artifacts.docx",
        "app.artifacts.pdf",
        "app.artifacts.pptx",
        "app.artifacts.render",
        "app.artifacts.service",
        "app.artifacts.validation",
    ),
    "docx_template": ("docx\\templates\\default.docx", "docx/templates/default.docx"),
    "pptx_template": ("pptx\\templates\\default.pptx", "pptx/templates/default.pptx"),
    "pdfium_binary": ("pdfium.dll",),
    "pdfium_binding": ("pypdfium2",),
    "lxml": ("lxml.etree", "lxml\\etree", "lxml/etree"),
    "reportlab": ("reportlab",),
    "pillow_plugins": ("PIL.JpegImagePlugin", "PIL.PngImagePlugin", "PIL.WebPImagePlugin"),
}

# Each tuple is an alternative spelling emitted by archive_viewer on different
# PyInstaller and Windows path versions.  A frozen STT worker needs at least
# one spelling from each group; checking only Python source imports would miss
# a one-file package that fails after extraction.
REQUIRED_ANY_GROUPS = {
    "stt_worker": ("app.stt.worker", "app\\stt\\worker", "app/stt/worker"),
    "stt_provider": (
        "app.stt.providers.faster_whisper",
        "app\\stt\\providers\\faster_whisper",
        "app/stt/providers/faster_whisper",
    ),
    "pcm_wav_decoder": ("app.stt.pcm_wav", "app\\stt\\pcm_wav", "app/stt/pcm_wav"),
    "faster_whisper": ("faster_whisper", "faster-whisper"),
    "ctranslate2_native": (
        "ctranslate2._ext",
        "ctranslate2\\_ext",
        "ctranslate2/_ext",
    ),
    "ctranslate2_runtime_dll": ("ctranslate2.dll",),
    "onnxruntime_native": (
        "onnxruntime.capi.onnxruntime_pybind11_state",
        "onnxruntime\\capi\\onnxruntime_pybind11_state",
        "onnxruntime/capi/onnxruntime_pybind11_state",
    ),
    "onnxruntime_dll": ("onnxruntime.dll",),
    "tokenizers_native": ("tokenizers.tokenizers", "tokenizers\\tokenizers", "tokenizers/tokenizers"),
    "silero_vad_asset": ("silero_vad_v6.onnx",),
}


def inventory_decision(listing: str) -> dict[str, object]:
    folded = listing.casefold()
    matches: dict[str, list[str]] = {}
    missing: list[str] = []
    for group, needles in REQUIRED_GROUPS.items():
        found = [needle for needle in needles if needle.casefold() in folded]
        matches[group] = found
        if group in {"docx_template", "pptx_template", "lxml"}:
            if not found:
                missing.append(group)
        elif len(found) != len(needles):
            missing.append(group)
    for group, alternatives in REQUIRED_ANY_GROUPS.items():
        found = [needle for needle in alternatives if needle.casefold() in folded]
        matches[group] = found
        if not found:
            missing.append(group)
    duplicate_web_payload = "node_modules" in folded
    pyav_payload = bool(
        re.search(r"(?im)^\s*av(?:[\\/]|\._core\b|\.libs[\\/])", listing)
    )
    return {
        "matches": matches,
        "missing_groups": missing,
        "duplicate_web_payload": duplicate_web_payload,
        "pyav_payload": pyav_payload,
        "passed": not missing and not duplicate_web_payload and not pyav_payload,
    }


def package_inventory(binary: Path, archive_listing: str) -> dict[str, object]:
    """Combine the executable archive and its required onedir support payload.

    The desktop sidecar intentionally uses PyInstaller's onedir layout.  A
    onefile bundle extracts every native STT dependency at every launch and
    pushed cold startup beyond the desktop budget.  Checking only the small
    executable would miss the native dependencies that the bootloader loads
    from its adjacent ``_internal`` directory.
    """
    onedir = "pyi-contents-directory _internal" in archive_listing.casefold()
    support_directory = binary.parent / "_internal"
    support_entries: list[str] = []
    unsafe_entries: list[str] = []
    if support_directory.exists():
        if not support_directory.is_dir():
            unsafe_entries.append(str(support_directory))
        else:
            for entry in support_directory.rglob("*"):
                if entry.is_symlink():
                    unsafe_entries.append(str(entry.relative_to(binary.parent)))
                elif entry.is_file():
                    support_entries.append(str(entry.relative_to(binary.parent)))

    decision = inventory_decision("\n".join((archive_listing, *support_entries)))
    missing_support_directory = onedir and not support_directory.is_dir()
    unsupported_packaging = not onedir
    decision["package_mode"] = "onedir" if onedir else "onefile_or_unknown"
    decision["support_directory"] = str(support_directory)
    decision["support_file_count"] = len(support_entries)
    decision["missing_support_directory"] = missing_support_directory
    decision["unsafe_support_entries"] = unsafe_entries
    decision["passed"] = bool(decision["passed"]) and not unsupported_packaging and not missing_support_directory and not unsafe_entries
    return decision


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the frozen sidecar Artifact Engine inventory")
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller.utils.cliutils.archive_viewer", "-r", "-b", str(binary)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"PyInstaller archive inspection failed with exit code {result.returncode}")
    decision = package_inventory(binary, result.stdout)
    payload = {
        "schema_version": 3,
        "status": "passed" if decision["passed"] else "failed",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "binary": str(binary),
        **decision,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "report": str(output)}))
    return 0 if decision["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
