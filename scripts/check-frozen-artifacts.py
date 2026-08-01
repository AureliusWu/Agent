from __future__ import annotations

import argparse
import json
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
    duplicate_web_payload = "node_modules" in folded
    return {
        "matches": matches,
        "missing_groups": missing,
        "duplicate_web_payload": duplicate_web_payload,
        "passed": not missing and not duplicate_web_payload,
    }


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
    decision = inventory_decision(result.stdout)
    payload = {
        "schema_version": 1,
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
