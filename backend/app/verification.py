from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .database import connect, now_iso, rows
from .sandbox import verify_task_changes, workspace_root


CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go", ".java", ".cs", ".cpp", ".c", ".h", ".vue", ".svelte"}
VERIFY_REQUEST_WORDS = re.compile(
    r"(?:运行|执行|重新运行|通过).{0,10}(?:测试|构建|编译|检查)|"
    r"(?:run|execute|rerun).{0,20}(?:test|build|compile|lint|check)|"
    r"\b(?:pytest|npm test|npm run build|typecheck)\b",
    re.I,
)
VERIFY_COMMAND_WORDS = {"test", "build", "lint", "check", "pytest", "unittest", "tsc", "cargo", "mypy", "ruff", "eslint", "vitest", "jest"}


def detect_project(workspace: str) -> dict[str, Any]:
    root = workspace_root(workspace)
    types: list[str] = []
    commands: list[dict[str, Any]] = []
    package = root / "package.json"
    if package.is_file():
        types.append("node")
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
            for name in ("lint", "typecheck", "test", "build"):
                if name in scripts:
                    commands.append({"name": name, "command": "npm", "args": ["run", name]})
        except (OSError, ValueError):
            pass
    if (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file():
        types.append("python")
        commands.append({"name": "test", "command": "python", "args": ["-m", "pytest"]})
    if (root / "Cargo.toml").is_file():
        types.append("rust")
        commands.extend([{"name": "check", "command": "cargo", "args": ["check"]}, {"name": "test", "command": "cargo", "args": ["test"]}])
    return {"root": str(root), "types": types or ["generic"], "commands": commands}


def _is_verification_command(run: dict[str, Any]) -> bool:
    try:
        payload = json.loads(run.get("input") or "{}")
    except ValueError:
        return False
    parts = [str(payload.get("command") or ""), *[str(item) for item in payload.get("args") or []]]
    return any(part.lower().strip("-:/\\") in VERIFY_COMMAND_WORDS for part in parts)


def verify_task(task_id: str, workspace: str, prompt: str, content: str) -> dict[str, Any]:
    tool_runs = rows("SELECT * FROM tool_runs WHERE task_id=? ORDER BY id", (task_id,))
    file_result = verify_task_changes(workspace, task_id)
    checks: list[dict[str, Any]] = list(file_result["checks"])
    changed_paths = [item["target"] for item in checks]
    has_code_changes = any(Path(path).suffix.lower() in CODE_SUFFIXES for path in changed_paths)
    command_runs = [run for run in tool_runs if run["tool"] == "run_command" and run["status"] in {"ok", "error"} and _is_verification_command(run)]
    for run in command_runs:
        checks.append({"kind": "command", "target": json.loads(run.get("input") or "{}"), "status": "passed" if run["status"] == "ok" else "failed", "duration_ms": run.get("duration_ms")})

    needs_command = has_code_changes or bool(VERIFY_REQUEST_WORDS.search(prompt))
    if needs_command and not command_runs:
        checks.append({"kind": "command", "target": "项目验证", "status": "not_run", "reason": "代码发生变化或任务明确要求验证，但没有执行测试、构建或静态检查"})
    if not checks:
        checks.append({"kind": "response", "target": "模型回答", "status": "passed" if content.strip() else "failed"})

    failed = [item for item in checks if item["status"] == "failed"]
    not_run = [item for item in checks if item["status"] in {"not_run", "unavailable"}]
    status = "failed" if failed else ("partial" if not_run else "passed")
    tool_failures = sum(1 for run in tool_runs if run["status"] == "error")
    score = max(0, 100 - len(failed) * 50 - len(not_run) * 25 - tool_failures * 5)
    summary = "验证通过" if status == "passed" else ("验证失败" if status == "failed" else "验证不完整")
    report = {
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "checks": checks,
        "modified_files": changed_paths,
        "project": detect_project(workspace),
        "tool_run_count": len(tool_runs),
        "evaluation": {"score": score, "tool_failures": tool_failures, "basis": "机器验证结果与真实工具执行记录"},
    }
    with connect() as db:
        db.execute(
            "INSERT INTO task_verifications(task_id, status, summary, report, created_at) VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, summary=excluded.summary, report=excluded.report, created_at=excluded.created_at",
            (task_id, status, summary, json.dumps(report, ensure_ascii=False), now_iso()),
        )
    return report
