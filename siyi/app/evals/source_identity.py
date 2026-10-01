"""Repository-only evaluation provenance; never consult Git in the desktop app."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def repository_identity() -> dict[str, object]:
    root = Path(__file__).resolve().parents[3]
    path = root / "scripts" / "generate_build_info.py"
    if not path.is_file() or not (root / ".git").exists():
        raise RuntimeError("Source-bound evaluation requires a repository checkout")
    spec = importlib.util.spec_from_file_location("siyi_eval_build_identity", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Build provenance module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._release_source_identity(root)
