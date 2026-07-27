from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from .config import settings
from app.memory.service import project_signature
from .sandbox import workspace_root


MANIFESTS = {
    "python": ("pyproject.toml", "requirements.txt"),
    "node": ("package.json",),
    "rust": ("Cargo.toml",),
    "go": ("go.mod",),
}
EXECUTABLES = ("python", "node", "npm", "git", "cargo", "rustc", "go")
_ENVIRONMENT_CACHE: dict[str, tuple[float, str, dict[str, Any]]] = {}


def detect_build_environment(workspace: str) -> dict[str, Any]:
    root = workspace_root(workspace)
    signature = project_signature(str(root))["fingerprint"]
    key = str(root)
    cached = _ENVIRONMENT_CACHE.get(key)
    if cached and cached[1] == signature and time.monotonic() < cached[0]:
        return {**cached[2], "cache_hit": True}
    manifests = {
        stack: [name for name in names if (root / name).is_file()]
        for stack, names in MANIFESTS.items()
    }
    result = {
        "workspace": key,
        "project_signature": signature,
        "stacks": [stack for stack, names in manifests.items() if names],
        "manifests": manifests,
        "executables": {name: bool(shutil.which(name)) for name in EXECUTABLES},
        "cache_hit": False,
    }
    _ENVIRONMENT_CACHE[key] = (time.monotonic() + settings.read_cache_ttl_seconds, signature, result)
    return result


def invalidate_build_environment(workspace: str) -> None:
    root = str(Path(workspace_root(workspace)))
    _ENVIRONMENT_CACHE.pop(root, None)
