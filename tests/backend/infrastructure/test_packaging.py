from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / "scripts" / "pyinstaller-hooks" / "hook-PIL.Image.py"


def test_sidecar_packages_only_supported_pillow_image_plugins() -> None:
    module = ast.parse(HOOK.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "hiddenimports" for target in node.targets)
    )
    assert ast.literal_eval(assignment.value) == [
        "PIL.JpegImagePlugin",
        "PIL.PngImagePlugin",
        "PIL.WebPImagePlugin",
    ]


def test_all_sidecar_build_paths_use_the_bounded_pillow_hook() -> None:
    for relative_path in ("scripts/build-desktop.ps1", "scripts/build-runtime.ps1"):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "scripts\\pyinstaller-hooks" in source
        assert "--additional-hooks-dir $hookDirectory" in source
