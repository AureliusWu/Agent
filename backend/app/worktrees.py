from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .sandbox import workspace_root


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45, check=False)


def _managed_root(root: Path) -> Path:
    return (root / ".agent" / "worktrees").resolve()


def _ensure_excluded(root: Path) -> None:
    exclude = root / ".git" / "info" / "exclude"
    if not exclude.parent.is_dir():
        return
    marker = "/.agent/worktrees/"
    existing = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
    if marker not in existing.splitlines():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{prefix}{marker}\n")


def _target(root: Path, name: str) -> Path:
    if not _NAME.fullmatch(name):
        raise ValueError("Worktree name must contain only letters, numbers, dots, underscores, or hyphens")
    target = (_managed_root(root) / name).resolve()
    if _managed_root(root) not in target.parents:
        raise ValueError("Worktree path escapes the managed directory")
    return target


def list_worktrees(workspace: str) -> dict[str, Any]:
    root = workspace_root(workspace)
    result = _run(root, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return {"success": False, "status": "error", "error_code": "not_git_repository", "error_message": result.stderr.strip()}
    managed = _managed_root(root)
    items: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in [*result.stdout.splitlines(), ""]:
        if not line:
            if current:
                raw_path = Path(str(current.pop("worktree"))).resolve()
                if raw_path == root or managed in raw_path.parents:
                    current["path"] = "." if raw_path == root else raw_path.relative_to(root).as_posix()
                    current["managed"] = raw_path != root
                    items.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value or True
    return {"success": True, "status": "ok", "worktrees": items}


def create_worktree(workspace: str, name: str, *, ref: str = "HEAD", branch: str | None = None) -> dict[str, Any]:
    root = workspace_root(workspace)
    target = _target(root, name)
    if target.exists():
        raise FileExistsError(str(target.relative_to(root)))
    target.parent.mkdir(parents=True, exist_ok=True)
    _ensure_excluded(root)
    args = ["worktree", "add"]
    if branch:
        if not _NAME.fullmatch(branch):
            raise ValueError("Branch name contains unsupported characters")
        args.extend(["-b", branch])
    else:
        args.append("--detach")
    args.extend([str(target), ref])
    result = _run(root, *args)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return {"success": True, "status": "ok", "name": name, "path": target.relative_to(root).as_posix(), "branch": branch, "ref": ref}


def remove_worktree(workspace: str, name: str, *, force: bool = False) -> dict[str, Any]:
    root = workspace_root(workspace)
    target = _target(root, name)
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(target))
    result = _run(root, *args)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return {"success": True, "status": "ok", "name": name, "removed": True}
