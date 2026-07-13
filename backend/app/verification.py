from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .database import connect, now_iso, rows
from .planning import AcceptanceCriterion, TaskPlan, build_task_plan, mark_plan_status, save_task_plan
from .sandbox import safe_path, verify_task_changes, workspace_root
from .task_state import TaskStatus


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


def _decode(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {"raw": value}


def _is_verification_command(run: dict[str, Any]) -> bool:
    payload = _decode(run.get("input") or "{}")
    if not isinstance(payload, dict):
        return False
    raw_args = payload.get("args") or []
    args = raw_args if isinstance(raw_args, list) else str(raw_args).split()
    parts = [str(payload.get("command") or ""), *[str(item) for item in args]]
    return any(part.lower().strip("-:/\\") in VERIFY_COMMAND_WORDS for part in parts)


def _exit_code(run: dict[str, Any]) -> int | None:
    output = _decode(run.get("output") or "{}")
    if not isinstance(output, dict):
        return None
    value = output.get("exit_code")
    if value is None and isinstance(output.get("data"), dict):
        value = output["data"].get("exit_code")
    return int(value) if isinstance(value, int) else None


def _tool_target(payload: dict[str, Any]) -> str:
    return str(payload.get("path") or payload.get("source") or payload.get("destination") or payload.get("command") or "")


def _file_state(workspace: str, relative: str) -> dict[str, Any]:
    try:
        path = safe_path(workspace_root(workspace), relative)
    except Exception as exc:
        return {"path": relative, "accessible": False, "error": str(exc)}
    if not path.exists():
        return {"path": relative, "accessible": True, "exists": False}
    if not path.is_file():
        return {"path": relative, "accessible": True, "exists": True, "type": "directory"}
    raw = path.read_bytes()
    return {"path": relative, "accessible": True, "exists": True, "type": "file", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _unresolved_failures(tool_runs: list[dict[str, Any]], expects_failure_handling: bool) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for index, run in enumerate(tool_runs):
        if run.get("status") != "error":
            continue
        later_success = any(item.get("tool") == run.get("tool") and item.get("status") == "ok" for item in tool_runs[index + 1 :])
        if later_success or expects_failure_handling:
            continue
        output = _decode(run.get("output") or "{}")
        failures.append({"tool": run.get("tool"), "error_code": output.get("error_code") if isinstance(output, dict) else None, "duration_ms": run.get("duration_ms")})
    return failures


def build_verifier_input(task_id: str, workspace: str, plan: TaskPlan, response: str) -> dict[str, Any]:
    raw_runs = rows("SELECT * FROM tool_runs WHERE task_id=? ORDER BY id", (task_id,))
    tool_runs = [{**run, "input": _decode(run.get("input") or "{}"), "output": _decode(run.get("output") or "{}")} for run in raw_runs]
    file_result = verify_task_changes(workspace, task_id)
    changed_paths = [item["target"] for item in file_result["checks"]]
    command_runs = [run for run in raw_runs if run.get("tool") == "run_command" and run.get("status") in {"ok", "error"} and _is_verification_command(run)]
    commands = [
        {
            "status": "passed" if run.get("status") == "ok" and _exit_code(run) in {0, None} else "failed",
            "target": _tool_target(_decode(run.get("input") or "{}")),
            "exit_code": _exit_code(run),
            "duration_ms": run.get("duration_ms"),
        }
        for run in command_runs
    ]
    key_results = []
    for run in tool_runs:
        payload = run["input"] if isinstance(run["input"], dict) else {}
        output = run["output"] if isinstance(run["output"], dict) else {}
        key_results.append(
            {
                "tool": run.get("tool"),
                "status": run.get("status"),
                "source": run.get("source"),
                "risk": run.get("risk"),
                "confirmed": bool(run.get("confirmed")),
                "target": _tool_target(payload),
                "exit_code": output.get("exit_code"),
                "error_code": output.get("error_code"),
                "duration_ms": run.get("duration_ms"),
            }
        )
    final_paths = sorted(set(changed_paths) | set(plan.expected_paths))
    side_effects = []
    if plan.strict_scope and plan.expected_paths:
        allowed = {Path(item).as_posix().lower() for item in plan.expected_paths}
        side_effects = [item for item in changed_paths if Path(item).as_posix().lower() not in allowed]
    response_bytes = response.encode("utf-8")
    return {
        "task_id": task_id,
        "user_task": plan.goal,
        "acceptance_criteria": [criterion.__dict__ for criterion in plan.acceptance_criteria],
        "final_file_state": [_file_state(workspace, path) for path in final_paths],
        "file_checks": file_result["checks"],
        "changed_files": changed_paths,
        "verification_commands": commands,
        "key_tool_results": key_results,
        "failures": _unresolved_failures(raw_runs, plan.expects_failure_handling),
        "side_effects": side_effects,
        "response": {"present": bool(response.strip()), "chars": len(response), "sha256": hashlib.sha256(response_bytes).hexdigest()},
        "blocked_reason": plan.blocked_reason,
        "expects_failure_handling": plan.expects_failure_handling,
        "project": detect_project(workspace),
    }


def _criterion_check(criterion: AcceptanceCriterion, evidence: dict[str, Any]) -> dict[str, Any]:
    kind = criterion.kind
    status = "failed"
    detail: Any = None
    reason = "验收条件未满足"
    if kind == "response_present":
        status = "passed" if evidence["response"]["present"] else "failed"
        detail = {"present": evidence["response"]["present"], "chars": evidence["response"]["chars"]}
        reason = "存在最终响应" if status == "passed" else "最终响应为空"
    elif kind == "blocked_safely":
        clean = bool(evidence.get("blocked_reason")) and not evidence["changed_files"]
        status = "passed" if clean else "failed"
        detail = {"blocked_reason": evidence.get("blocked_reason"), "changed_files": evidence["changed_files"]}
        reason = "能力不可用且没有产生副作用" if clean else "阻塞条件或副作用检查失败"
    elif kind == "tool_evidence":
        has_evidence = bool(evidence["key_tool_results"] or evidence["file_checks"])
        status = "passed" if has_evidence else "not_run"
        detail = {"tool_run_count": len(evidence["key_tool_results"]), "file_check_count": len(evidence["file_checks"])}
        reason = "存在真实工具证据" if status == "passed" else "没有执行工作区工具"
    elif kind == "changes_recorded":
        checks = evidence["file_checks"]
        if not checks:
            status, reason = "not_run", "没有记录任何文件变更"
        elif all(item.get("status") == "passed" for item in checks):
            status, reason = "passed", "文件终态与已记录变更一致"
        else:
            status, reason = "failed", "文件终态与已记录变更不一致"
        detail = checks
    elif kind == "path_state":
        path = criterion.parameters.get("path")
        expected = bool(criterion.parameters.get("exists"))
        actual = next((item for item in evidence["final_file_state"] if item.get("path") == path), {})
        matches = actual.get("accessible") is True and bool(actual.get("exists")) is expected
        status = "passed" if matches else "failed"
        detail = {"path": path, "expected_exists": expected, "actual": actual}
        reason = "目标路径终态符合要求" if matches else "目标路径终态不符合要求"
    elif kind == "verification_command":
        commands = evidence["verification_commands"]
        if any(item["status"] == "passed" for item in commands):
            status, reason = "passed", "存在成功的真实验证命令"
        elif commands:
            status, reason = "failed", "验证命令已运行但失败"
        else:
            status, reason = "not_run", "没有运行测试、构建或检查"
        detail = commands
    elif kind == "scope_control":
        status = "passed" if not evidence["side_effects"] else "failed"
        detail = {"side_effects": evidence["side_effects"]}
        reason = "没有范围外修改" if status == "passed" else "发现范围外修改"
    elif kind == "failures_resolved":
        status = "passed" if not evidence["failures"] else "failed"
        detail = {"failures": evidence["failures"], "expected_failure_handling": evidence["expects_failure_handling"]}
        reason = "没有未处理失败" if status == "passed" else "仍有未处理的工具失败"
    return {
        "criterion_id": criterion.id,
        "description": criterion.description,
        "kind": kind,
        "required": criterion.required,
        "status": status,
        "evidence": detail,
        "reason": reason,
    }


def _evidence_fingerprint(evidence: dict[str, Any], checks: list[dict[str, Any]]) -> str:
    stable = {
        "final_file_state": evidence["final_file_state"],
        "verification_commands": evidence["verification_commands"],
        "failures": evidence["failures"],
        "side_effects": evidence["side_effects"],
        "response_present": evidence["response"]["present"],
        "tool_run_count": len(evidence["key_tool_results"]),
        "checks": [{"id": item["criterion_id"], "status": item["status"]} for item in checks],
    }
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def verify_task(
    task_id: str,
    workspace: str,
    plan: TaskPlan | str,
    response: str,
    *,
    previous_evidence_fingerprint: str | None = None,
) -> dict[str, Any]:
    if isinstance(plan, str):
        plan = build_task_plan(task_id, plan, ())
        save_task_plan(plan)
    verifier_input = build_verifier_input(task_id, workspace, plan, response)
    checks = [_criterion_check(item, verifier_input) for item in plan.acceptance_criteria]
    failed = [item for item in checks if item["required"] and item["status"] == "failed"]
    incomplete = [item for item in checks if item["required"] and item["status"] in {"not_run", "unavailable"}]
    met = [item for item in checks if item["status"] == "passed"]
    if plan.blocked_reason and not failed and not incomplete:
        status = "blocked"
    elif failed:
        status = "failed"
    elif incomplete:
        status = "partially_passed"
    else:
        status = "passed"
    fingerprint = _evidence_fingerprint(verifier_input, checks)
    progressed = previous_evidence_fingerprint is None or previous_evidence_fingerprint != fingerprint
    retry_scope = [item["criterion_id"] for item in [*failed, *incomplete]]
    retry_recommended = status in {"failed", "partially_passed"} and bool(retry_scope) and progressed
    reason = {
        "passed": "所有必需验收条件均有确定性证据",
        "partially_passed": "部分验收条件缺少执行证据",
        "failed": "至少一项必需验收条件失败",
        "blocked": plan.blocked_reason or "任务所需能力不可用",
    }[status]
    if previous_evidence_fingerprint and not progressed and status not in {"passed", "blocked"}:
        reason += "；返工后没有产生新的可验证证据"
    summary = {"passed": "验证通过", "partially_passed": "验证不完整", "failed": "验证失败", "blocked": "任务已诚实阻塞"}[status]
    score = max(0, round(100 * len(met) / max(len(checks), 1)) - len(verifier_input["failures"]) * 5)
    report = {
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "requirements_met": met,
        "requirements_failed": [*failed, *incomplete],
        "evidence": checks,
        "side_effects": verifier_input["side_effects"],
        "retry_recommended": retry_recommended,
        "retry_scope": retry_scope,
        "reason": reason,
        "checks": checks,
        "modified_files": verifier_input["changed_files"],
        "project": verifier_input["project"],
        "tool_run_count": len(verifier_input["key_tool_results"]),
        "evaluation": {"score": score, "tool_failures": len(verifier_input["failures"]), "basis": "独立验收条件与确定性工具证据"},
        "verifier_input": verifier_input,
        "evidence_fingerprint": fingerprint,
        "progress_since_previous": progressed,
    }
    with connect() as db:
        attempt = int(db.execute("SELECT COUNT(*) FROM task_verification_attempts WHERE task_id=?", (task_id,)).fetchone()[0]) + 1
        stamp = now_iso()
        encoded = json.dumps(report, ensure_ascii=False)
        db.execute(
            "INSERT INTO task_verifications(task_id, status, summary, report, created_at) VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, summary=excluded.summary, report=excluded.report, created_at=excluded.created_at",
            (task_id, status, summary, encoded, stamp),
        )
        db.execute(
            "INSERT INTO task_verification_attempts(task_id, attempt, status, report, evidence_fingerprint, created_at) VALUES(?,?,?,?,?,?)",
            (task_id, attempt, status, encoded, fingerprint, stamp),
        )
        db.execute("UPDATE agent_tasks SET verification_attempts=?, updated_at=? WHERE id=?", (attempt, stamp, task_id))
    mark_plan_status(task_id, "verified" if status in {"passed", "blocked"} else "repair_required")
    return report


def finalize_task_from_verification(task_id: str, report: dict[str, Any], **fields: Any) -> TaskStatus:
    status = report.get("status")
    if status == "passed":
        final = TaskStatus.COMPLETED
    elif status == "blocked":
        final = TaskStatus.BLOCKED
    elif status == "failed" and not report.get("requirements_met"):
        final = TaskStatus.FAILED
    else:
        final = TaskStatus.PARTIALLY_COMPLETED
    allowed = {
        "model_calls",
        "tool_calls",
        "files_modified",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "phase_tokens",
        "estimated_cost_usd",
        "model_route",
        "cache_hits",
        "cache_misses",
        "current_step",
        "completed_steps",
        "pending_steps",
        "repair_attempts",
        "verification_attempts",
    }
    values = {key: value for key, value in fields.items() if key in allowed}
    for key in ("completed_steps", "pending_steps", "phase_tokens", "model_route"):
        if key in values:
            values[key] = json.dumps(values[key], ensure_ascii=False)
    values.update({"termination_reason": report.get("reason") or report.get("summary"), "finished_at": now_iso(), "resumable": 0})
    assignments = ["status=?", "updated_at=?", *[f"{key}=?" for key in values]]
    with connect() as db:
        db.execute(f"UPDATE agent_tasks SET {', '.join(assignments)} WHERE id=?", (final.value, now_iso(), *values.values(), task_id))
    return final
