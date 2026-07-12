from __future__ import annotations

import fnmatch
import shutil
import subprocess
from pathlib import Path
from typing import Any


WRITE_TOOLS = {"write_file", "move_file", "create_directory", "delete_file", "run_command"}
IGNORED_DIRECTORIES = {".git", "node_modules", "dist", "build", "target", "__pycache__", ".venv", "venv"}


class SandboxError(ValueError):
    pass


def workspace_root(workspace: str) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise SandboxError("工作区不存在或不是目录")
    return root


def safe_path(root: Path, relative: str) -> Path:
    candidate = Path(relative).resolve() if Path(relative).is_absolute() else (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SandboxError("路径超出已选择的工作区") from exc
    return candidate


def approval_key(tool: str, arguments: dict[str, Any]) -> str:
    target = arguments.get("path") or arguments.get("source") or arguments.get("command") or ""
    return f"{tool}:{target}"


def _search(root: Path, base: Path, query: str, pattern: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    lowered = query.lower()
    for path in base.rglob("*"):
        if any(part in IGNORED_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        if not path.is_file() or not fnmatch.fnmatch(path.name, pattern):
            continue
        relative = str(path.relative_to(root))
        if lowered in path.name.lower():
            results.append({"path": relative, "line": None, "text": "文件名匹配"})
        if path.stat().st_size > 1_000_000:
            continue
        try:
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if lowered in line.lower():
                    results.append({"path": relative, "line": number, "text": line[:300]})
                    if len(results) >= 200:
                        return results
        except OSError:
            continue
    return results[:200]


def execute_tool(workspace: str, mode: str, tool: str, arguments: dict[str, Any], approved: bool = False) -> dict[str, Any]:
    root = workspace_root(workspace)
    if tool in WRITE_TOOLS:
        if mode == "readonly":
            return {"status": "denied", "error": "只读模式禁止修改或执行命令"}
        if mode == "confirm" and not approved:
            return {"status": "confirmation_required", "approval_key": approval_key(tool, arguments), "tool": tool, "arguments": arguments}

    if tool == "list_files":
        path = safe_path(root, str(arguments.get("path", ".")))
        if not path.is_dir():
            raise SandboxError("目标不是目录")
        items = [{"name": item.name, "path": str(item.relative_to(root)), "type": "directory" if item.is_dir() else "file", "size": item.stat().st_size if item.is_file() else None} for item in sorted(path.iterdir(), key=lambda entry: (not entry.is_dir(), entry.name.lower()))[:500]]
        return {"status": "ok", "items": items}
    if tool == "search_files":
        base = safe_path(root, str(arguments.get("path", ".")))
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise SandboxError("搜索内容不能为空")
        return {"status": "ok", "matches": _search(root, base, query, str(arguments.get("glob", "*")))}
    if tool == "read_file":
        path = safe_path(root, str(arguments["path"]))
        if path.stat().st_size > 2_000_000:
            raise SandboxError("文件超过 2 MB 读取上限")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(int(arguments.get("start_line") or 1), 1)
        end = min(int(arguments.get("end_line") or len(lines)), len(lines))
        return {"status": "ok", "path": str(path.relative_to(root)), "start_line": start, "end_line": end, "content": "\n".join(lines[start - 1:end])}
    if tool == "write_file":
        path = safe_path(root, str(arguments["path"])); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(arguments.get("content", "")), encoding="utf-8")
        return {"status": "ok", "path": str(path.relative_to(root)), "bytes": path.stat().st_size}
    if tool == "move_file":
        source = safe_path(root, str(arguments["source"])); destination = safe_path(root, str(arguments["destination"])); destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return {"status": "ok", "source": str(source.relative_to(root)), "destination": str(destination.relative_to(root))}
    if tool == "create_directory":
        path = safe_path(root, str(arguments["path"])); path.mkdir(parents=True, exist_ok=True)
        return {"status": "ok", "path": str(path.relative_to(root))}
    if tool == "delete_file":
        path = safe_path(root, str(arguments["path"]))
        if path.is_dir():
            raise SandboxError("不支持删除目录")
        path.unlink()
        return {"status": "ok", "path": str(path.relative_to(root))}
    if tool == "run_command":
        cwd = safe_path(root, str(arguments.get("cwd", ".")))
        if not cwd.is_dir():
            raise SandboxError("命令工作目录不存在")
        command = str(arguments.get("command", "")).strip()
        if not command or command.lower() in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "bash", "sh"}:
            raise SandboxError("请直接调用具体程序，不允许启动交互式 shell")
        args = [str(item) for item in arguments.get("args", [])]
        timeout = min(max(int(arguments.get("timeout", 60)), 1), 120)
        process = subprocess.run([command, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False, shell=False)
        return {"status": "ok" if process.returncode == 0 else "error", "exit_code": process.returncode, "stdout": process.stdout[-20_000:], "stderr": process.stderr[-20_000:]}
    raise SandboxError(f"未知工具：{tool}")
