from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from app.artifacts.service import execute_artifact_tool


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _convert(viewer: Path, source: Path, output: Path, profile: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        str(viewer),
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--norestore",
        f"-env:UserInstallation={profile.as_uri()}",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output),
        str(source),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    target = output / f"{source.stem}.pdf"
    if result.returncode != 0 or not target.is_file():
        raise RuntimeError(
            f"LibreOffice conversion failed for {source.name}: exit={result.returncode}; "
            f"stdout={result.stdout[-1000:]}; stderr={result.stderr[-1000:]}"
        )
    reader = PdfReader(target)
    return {
        "source": source.name,
        "pdf": f"{output.name}/{target.name}",
        "exit_code": result.returncode,
        "page_count": len(reader.pages),
        "source_sha256": _sha256(source),
        "pdf_sha256": _sha256(target),
        "pdf_bytes": target.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Artifact Engine OOXML in isolated LibreOffice")
    parser.add_argument("--soffice", type=Path, required=True)
    parser.add_argument("--viewer-version", required=True)
    parser.add_argument("--viewer-msi", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    viewer_exe = args.soffice.resolve(strict=True)
    viewer = viewer_exe.with_suffix(".com") if viewer_exe.with_suffix(".com").is_file() else viewer_exe
    viewer_msi = args.viewer_msi.resolve(strict=True)
    output = args.output.resolve()
    workspace = output / "workspace"
    exported = output / "exported"
    profiles = output / "profiles"
    for directory in (workspace, exported, profiles, workspace / "images"):
        directory.mkdir(parents=True, exist_ok=True)

    image_path = workspace / "images" / "proof.png"
    with Image.new("RGB", (480, 240), (34, 116, 165)) as image:
        image.save(image_path, format="PNG")

    docx = execute_artifact_tool(
        str(workspace),
        "artifact.docx.create",
        {
            "path": "viewer-proof.docx",
            "title": "司忆产物引擎独立查看器验收",
            "content": (
                "# 司忆产物引擎独立查看器验收\n\n"
                "这是由 Artifact Engine 创建的中文 DOCX，用于真实打开与可打印导出。\n\n"
                "- 中文列表一\n- 中文列表二\n\n"
                "| 检查项 | 状态 |\n| --- | --- |\n| 表格 | 通过 |\n| 图片 | 通过 |\n\n"
                "![隔离验收图片](images/proof.png)"
            ),
            "image_paths": ["images/proof.png"],
        },
    )
    pptx = execute_artifact_tool(
        str(workspace),
        "artifact.pptx.create",
        {
            "path": "viewer-proof.pptx",
            "title": "司忆产物引擎独立查看器验收",
            "slides": [
                {
                    "title": "第一页：中文与列表",
                    "blocks": [
                        {"kind": "paragraph", "text": "独立查看器真实打开验收。"},
                        {"kind": "bullet_list", "items": ["中文字体", "结构完整", "可打印导出"]},
                    ],
                },
                {
                    "title": "第二页：表格与图片",
                    "blocks": [
                        {"kind": "table", "table": {"rows": [["检查项", "状态"], ["图片", "通过"]]}},
                        {"kind": "image", "image": {"source": "images/proof.png", "alt_text": "验收图片"}},
                    ],
                },
            ],
        },
    )
    if not docx.get("success") or not pptx.get("success"):
        raise RuntimeError("Artifact Engine source generation failed")

    conversions = [
        _convert(viewer, workspace / "viewer-proof.docx", exported / "docx", profiles / "docx"),
        _convert(viewer, workspace / "viewer-proof.pptx", exported / "pptx", profiles / "pptx"),
    ]
    if conversions[0]["page_count"] < 1 or conversions[1]["page_count"] != 2:
        raise RuntimeError(f"Unexpected independent viewer page counts: {conversions}")

    payload = {
        "schema_version": 1,
        "status": "passed",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "viewer": {
            "name": "LibreOffice",
            "version": args.viewer_version,
            "executable_sha256": _sha256(viewer_exe),
            "msi_sha256": _sha256(viewer_msi),
            "isolated_profile": True,
            "system_install": False,
        },
        "artifact_validation": {
            "docx": docx["validation"],
            "pptx": pptx["validation"],
        },
        "conversions": conversions,
    }
    report = output / "report.json"
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "report": str(report)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
