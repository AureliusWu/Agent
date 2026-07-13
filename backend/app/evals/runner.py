from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException

from .. import __version__
from ..config import settings
from ..database import connect, init_db, now_iso, rows
from ..mcp import call_http_mcp
from ..planning import build_task_plan, save_task_plan
from ..recovery import create_checkpoint
from ..schemas import ChatRequest
from ..task_runner import TaskLimits, cancel_task, run_chat
from .evidence import SANDBOX_MARKERS, changed_paths, evaluate_rules, normalized_tool_runs, snapshot_workspace
from .loader import load_tasks
from .models import EvalAction, EvalMode, EvalReport, EvalStatus, EvalTaskResult, EvalTaskSpec, Evidence
from .reporting import aggregate_metrics, persist_report


class ScriptedCompletion:
    def __init__(self, actions: list[EvalAction]) -> None:
        self.actions = actions
        self.position = 0
        self.call_count = 0

    async def __call__(self, messages: list[dict[str, Any]], api_key: str | None = None, **_: Any) -> dict[str, Any]:
        del messages, api_key
        if self.position >= len(self.actions):
            raise RuntimeError("评测脚本已耗尽，但 Agent 仍请求模型调用")
        action = self.actions[self.position]
        self.position += 1
        self.call_count += 1
        metrics = {"usage": {"prompt_tokens": action.input_tokens, "completion_tokens": action.output_tokens, "total_tokens": action.input_tokens + action.output_tokens}}
        if action.kind == "sleep":
            await asyncio.sleep(action.seconds)
            return {"role": "assistant", "content": action.content or "", "_metrics": metrics}
        if action.kind == "final":
            return {"role": "assistant", "content": action.content or "", "_metrics": metrics}
        call_id = f"eval-{self.call_count}-{uuid.uuid4().hex[:8]}"
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": action.tool, "arguments": json.dumps(action.arguments, ensure_ascii=False)}}],
            "_metrics": metrics,
        }

    def retry_last_action(self) -> None:
        if self.position:
            self.position -= 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_fixture_path(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"评测 fixture 路径越界：{relative}") from exc
    return target


def _prepare_workspace(stage: Path, spec: EvalTaskSpec) -> Path:
    root = stage / "workspaces" / spec.id
    root.mkdir(parents=True, exist_ok=False)
    for relative, content in spec.setup_files.items():
        target = _safe_fixture_path(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for relative, content in spec.outside_files.items():
        if Path(relative).name != relative:
            raise ValueError(f"outside fixture 只能使用文件名：{relative}")
        (root.parent / relative).write_text(content, encoding="utf-8")
    return root


def _create_conversation(workspace: Path, spec: EvalTaskSpec, suffix: str = "") -> int:
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO conversations(title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?)",
            (f"Eval {spec.id}{suffix}", str(workspace), spec.permission_mode, now_iso(), now_iso()),
        )
        return int(cursor.lastrowid)


def _decode_report(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}


def _task_trace(task_ids: list[str]) -> dict[str, Any]:
    if not task_ids:
        return {"tasks": [], "plans": [], "tool_runs": [], "model_runs": [], "verifications": [], "verification_attempts": [], "repair_runs": [], "checkpoints": [], "operations": [], "agent_runs": [], "agent_events": [], "file_locks": []}
    placeholders = ",".join("?" for _ in task_ids)
    tasks = rows(f"SELECT * FROM agent_tasks WHERE id IN ({placeholders}) ORDER BY created_at", tuple(task_ids))
    tool_runs = normalized_tool_runs(rows(f"SELECT * FROM tool_runs WHERE task_id IN ({placeholders}) ORDER BY id", tuple(task_ids)))
    model_runs = rows(f"SELECT * FROM model_runs WHERE task_id IN ({placeholders}) ORDER BY id", tuple(task_ids))
    verifications = rows(f"SELECT * FROM task_verifications WHERE task_id IN ({placeholders}) ORDER BY id", tuple(task_ids))
    plans = rows(f"SELECT * FROM task_plans WHERE task_id IN ({placeholders}) ORDER BY created_at", tuple(task_ids))
    verification_attempts = rows(f"SELECT * FROM task_verification_attempts WHERE task_id IN ({placeholders}) ORDER BY id", tuple(task_ids))
    repair_runs = rows(f"SELECT * FROM task_repair_runs WHERE task_id IN ({placeholders}) ORDER BY id", tuple(task_ids))
    checkpoints = rows(f"SELECT id, task_id, sequence, phase, reason, workspace_hash, git_status, created_at FROM task_checkpoints WHERE task_id IN ({placeholders}) ORDER BY id", tuple(task_ids))
    operations = rows(f"SELECT * FROM task_operations WHERE task_id IN ({placeholders}) ORDER BY started_at", tuple(task_ids))
    multi_task_ids = [str(task["id"]) for task in tasks if task.get("orchestration_mode") != "single"]
    if multi_task_ids:
        multi_placeholders = ",".join("?" for _ in multi_task_ids)
        agent_runs = rows(f"SELECT * FROM agent_runs WHERE parent_task_id IN ({multi_placeholders}) ORDER BY depth, started_at", tuple(multi_task_ids))
        agent_events = rows(f"SELECT * FROM agent_trace_events WHERE parent_task_id IN ({multi_placeholders}) ORDER BY id", tuple(multi_task_ids))
        file_locks = rows(f"SELECT * FROM agent_file_locks WHERE holder_task_id IN ({multi_placeholders}) ORDER BY acquired_at", tuple(multi_task_ids))
    else:
        agent_runs, agent_events, file_locks = [], [], []
    for item in verifications:
        item["report"] = _decode_report(item.get("report"))
    for item in verification_attempts:
        item["report"] = _decode_report(item.get("report"))
    for item in plans:
        item["plan"] = _decode_report(item.get("plan"))
        item["acceptance_criteria"] = _decode_report(item.get("acceptance_criteria"))
    return {"tasks": tasks, "plans": plans, "tool_runs": tool_runs, "model_runs": model_runs, "verifications": verifications, "verification_attempts": verification_attempts, "repair_runs": repair_runs, "checkpoints": checkpoints, "operations": operations, "agent_runs": agent_runs, "agent_events": agent_events, "file_locks": file_locks}


def _runtime_limits(spec: EvalTaskSpec) -> TaskLimits:
    return TaskLimits(
        max_agent_rounds=max(4, len(spec.actions) + 4),
        task_timeout_seconds=spec.max_execution_seconds,
        max_task_tokens=spec.max_tokens,
        max_phase_tokens=min(spec.max_tokens, settings.max_phase_tokens),
        max_model_call_tokens=min(spec.max_tokens, settings.max_model_call_tokens),
        max_tool_calls=spec.max_tool_calls,
        max_tool_result_chars=settings.max_tool_result_chars,
        max_file_snippet_chars=settings.max_file_snippet_chars,
        max_duplicate_tool_calls=settings.max_duplicate_tool_calls,
        max_consecutive_failures=settings.max_consecutive_failures,
        max_no_progress_rounds=settings.max_no_progress_rounds,
        max_repair_attempts=settings.max_repair_attempts,
    )


async def _run_runtime(
    spec: EvalTaskSpec,
    workspace: Path,
    *,
    mode: EvalMode,
    api_key: str | None,
) -> tuple[str | None, dict[str, Any], int, list[str]]:
    conversation_id = _create_conversation(workspace, spec)
    task_id = uuid.uuid4().hex
    script = ScriptedCompletion(spec.actions) if mode == "scripted_runtime" else None
    approval_tokens: list[str] = []
    approval_count = 0
    final_result: dict[str, Any] = {}
    runtime_error: str | None = None
    for _ in range(12):
        payload = ChatRequest(
            conversation_id=conversation_id,
            content=spec.prompt,
            task_id=task_id,
            approved_actions=approval_tokens,
            approval_scope="once",
            orchestration_mode=spec.orchestration_mode,
            agent_count=spec.agent_count,
        )
        try:
            final_result = await run_chat(
                payload,
                api_key,
                completion_fn=script,
                limits=_runtime_limits(spec),
            )
        except HTTPException as exc:
            runtime_error = f"HTTP {exc.status_code}: {exc.detail}"
            break
        pending = final_result.get("pending_actions") or []
        if final_result.get("task_status") != "waiting_confirmation" or not pending:
            break
        if not spec.auto_approve:
            break
        approval_count += len(pending)
        approval_tokens = [item["approval_key"] for item in pending if item.get("approval_key")]
    else:
        runtime_error = "确认循环超过安全上限"

    trace = _task_trace([task_id])
    trace["final_response"] = final_result.get("content")
    trace["runtime_error"] = runtime_error
    runtime_status = final_result.get("task_status")
    if not runtime_status and trace["tasks"]:
        runtime_status = trace["tasks"][0].get("status")
    return runtime_status, trace, approval_count, [task_id]


async def _run_interrupted_recovery(spec: EvalTaskSpec, workspace: Path) -> tuple[str, dict[str, Any], int, list[str], dict[str, Any]]:
    conversation_id = _create_conversation(workspace, spec)
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO agent_tasks(id, conversation_id, status, prompt, current_step, completed_steps, pending_steps, created_at, updated_at, started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, conversation_id, "running", spec.prompt, "implementation", '["file:state.txt"]', '["verification"]', stamp, stamp, stamp),
        )
    plan = build_task_plan(task_id, spec.prompt, [])
    save_task_plan(plan)
    create_checkpoint(
        task_id,
        str(workspace),
        "finalization",
        "simulated_process_shutdown",
        {
            "goal": spec.prompt,
            "completed_steps": ["file:state.txt"],
            "pending_steps": ["verification"],
            "context_summary": "文件操作已完成，等待最终验证",
            "executor_messages": [{"role": "user", "content": spec.prompt}],
            "pending_final_response": "恢复完成",
            "round_number": 1,
            "model_calls": 1,
            "tool_calls": 1,
            "files_modified_count": 1,
        },
    )
    init_db()
    recovery_error: str | None = None
    try:
        await run_chat(
            ChatRequest(conversation_id=conversation_id, content=spec.prompt, task_id=task_id, resume=True),
            completion_fn=ScriptedCompletion([EvalAction(kind="final", content="恢复完成")]),
            limits=_runtime_limits(spec),
        )
    except HTTPException as exc:
        recovery_error = f"HTTP {exc.status_code}: {exc.detail}"
    trace = _task_trace([task_id])
    trace["recovery_error"] = recovery_error
    runtime_status = trace["tasks"][0]["status"] if trace["tasks"] else "invalid"
    succeeded = runtime_status in {"completed", "partially_completed"} and recovery_error is None
    return runtime_status, trace, 0, [task_id], {"recovery_succeeded": succeeded}


async def _run_mcp_failure(spec: EvalTaskSpec, workspace: Path, *, mode: EvalMode, api_key: str | None) -> tuple[str | None, dict[str, Any], int, list[str], dict[str, Any]]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    transport_error: str | None = None
    try:
        await asyncio.wait_for(call_http_mcp(f"http://127.0.0.1:{port}", "tools/list", {}), timeout=3)
    except Exception as exc:
        transport_error = type(exc).__name__
    runtime_status, trace, approvals, task_ids = await _run_runtime(spec, workspace, mode=mode, api_key=api_key)
    tool_error = any(item.get("status") == "error" for item in trace.get("tool_runs", []))
    trace["mcp_transport_error"] = transport_error
    contained = bool(transport_error and tool_error and runtime_status in {"completed", "partially_completed"})
    return runtime_status, trace, approvals, task_ids, {"mcp_failure_contained": contained}


async def _run_sidecar(spec: EvalTaskSpec, stage: Path) -> tuple[str, dict[str, Any], int, list[str], dict[str, Any]]:
    backend_root = Path(__file__).resolve().parents[2]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = os.environ.copy()
    env.pop("AGENT_DEEPSEEK_API_KEY", None)
    env.update({
        "AGENT_PORT": str(port),
        "AGENT_DATABASE_PATH": str(stage / "sidecar.db"),
        "AGENT_LOG_PATH": str(stage / "sidecar.log"),
    })
    process = subprocess.Popen(
        [sys.executable, "run_server.py"],
        cwd=backend_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    healthy = False
    startup_error: str | None = None
    try:
        deadline = time.monotonic() + min(spec.max_execution_seconds, 12)
        async with httpx.AsyncClient(timeout=0.6) as client:
            while time.monotonic() < deadline:
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/api/health")
                    if response.status_code == 200 and response.json().get("status") == "ok":
                        healthy = True
                        break
                except (httpx.HTTPError, ValueError):
                    await asyncio.sleep(0.1)
        if not healthy:
            startup_error = "Sidecar 未在时限内健康"
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
                else:
                    process.kill()
                process.wait(timeout=5)
    stopped = process.poll() is not None
    trace = {"sidecar": {"pid": process.pid, "port": port, "healthy": healthy, "stopped": stopped, "return_code": process.returncode, "startup_error": startup_error}}
    success = healthy and stopped
    return "completed" if success else "failed", trace, 0, [], {"sidecar_stopped": success}


async def _run_timeout_cancel(spec: EvalTaskSpec, workspace: Path) -> tuple[str, dict[str, Any], int, list[str], dict[str, Any]]:
    async def never_finishes(messages: list[dict[str, Any]], api_key: str | None = None, **_: Any) -> dict[str, Any]:
        del messages, api_key
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    timeout_conversation = _create_conversation(workspace, spec, " timeout")
    timeout_id = uuid.uuid4().hex
    timeout_limits = TaskLimits(
        max_agent_rounds=2,
        task_timeout_seconds=0.2,
        max_task_tokens=spec.max_tokens,
        max_phase_tokens=min(spec.max_tokens, settings.max_phase_tokens),
        max_model_call_tokens=min(spec.max_tokens, settings.max_model_call_tokens),
        max_tool_calls=max(1, spec.max_tool_calls),
        max_tool_result_chars=settings.max_tool_result_chars,
        max_file_snippet_chars=settings.max_file_snippet_chars,
        max_duplicate_tool_calls=3,
        max_consecutive_failures=3,
        max_no_progress_rounds=2,
        max_repair_attempts=0,
    )
    timeout_result = await run_chat(
        ChatRequest(conversation_id=timeout_conversation, content="timeout", task_id=timeout_id),
        completion_fn=never_finishes,
        limits=timeout_limits,
    )

    cancel_conversation = _create_conversation(workspace, spec, " cancel")
    cancel_id = uuid.uuid4().hex
    started = asyncio.Event()

    async def cancellable(messages: list[dict[str, Any]], api_key: str | None = None, **_: Any) -> dict[str, Any]:
        del messages, api_key
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    running = asyncio.create_task(
        run_chat(
            ChatRequest(conversation_id=cancel_conversation, content="cancel", task_id=cancel_id),
            completion_fn=cancellable,
            limits=TaskLimits(
                max_agent_rounds=2,
                task_timeout_seconds=5,
                max_task_tokens=spec.max_tokens,
                max_phase_tokens=min(spec.max_tokens, settings.max_phase_tokens),
                max_model_call_tokens=min(spec.max_tokens, settings.max_model_call_tokens),
                max_tool_calls=max(1, spec.max_tool_calls),
                max_tool_result_chars=settings.max_tool_result_chars,
                max_file_snippet_chars=settings.max_file_snippet_chars,
                max_duplicate_tool_calls=3,
                max_consecutive_failures=3,
                max_no_progress_rounds=2,
                max_repair_attempts=0,
            ),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    cancellation = cancel_task(cancel_id)
    cancel_result = await running
    statuses = [timeout_result.get("task_status"), cancel_result.get("task_status")]
    success = statuses == ["timed_out", "cancelled"] and cancellation.get("interrupted") is True
    trace = _task_trace([timeout_id, cancel_id])
    trace["termination_results"] = {"timeout": timeout_result, "cancel": cancel_result, "cancel_request": cancellation}
    return "+".join(str(item) for item in statuses), trace, 0, [timeout_id, cancel_id], {"timeout_and_cancelled": success}


def _trace_metrics(trace: dict[str, Any], approval_count: int) -> dict[str, int]:
    tool_runs = trace.get("tool_runs") or []
    tasks = trace.get("tasks") or []
    model_runs = trace.get("model_runs") or []
    tests = []
    builds = []
    for run in tool_runs:
        if run.get("tool") != "run_command":
            continue
        payload = run.get("input") or {}
        words = [str(payload.get("command") or ""), *[str(item) for item in payload.get("args") or []]]
        lowered = {word.lower().strip("-:/\\") for word in words}
        if run.get("status") in {"ok", "error"} and lowered & {"test", "pytest", "unittest", "vitest", "jest"}:
            tests.append(run)
        if run.get("status") in {"ok", "error"} and lowered & {"build", "compile", "check", "tsc", "cargo"}:
            builds.append(run)
    permission_violations = sum(1 for run in tool_runs if run.get("status") == "ok" and run.get("risk") == "critical" and not run.get("confirmed"))
    sandbox_violations = 0
    for run in tool_runs:
        payload = run.get("input") or {}
        attempted_escape = any(".." in str(payload.get(field) or "").replace("\\", "/").split("/") for field in ("path", "source", "destination", "cwd"))
        rejected = run.get("status") == "error" and any(marker in json.dumps(run.get("output"), ensure_ascii=False) for marker in SANDBOX_MARKERS)
        if attempted_escape and run.get("status") == "ok" and not rejected:
            sandbox_violations += 1
    return {
        "model_calls": sum(int(task.get("model_calls") or 0) for task in tasks),
        "tool_calls": len(tool_runs),
        "tool_errors": sum(1 for run in tool_runs if run.get("status") == "error"),
        "retry_count": sum(int(run.get("retry_count") or 0) for run in model_runs),
        "total_tokens": sum(int(task.get("total_tokens") or 0) for task in tasks),
        "human_interventions": approval_count,
        "tests_run": len(tests),
        "tests_passed": sum(1 for run in tests if run.get("status") == "ok"),
        "builds_run": len(builds),
        "builds_passed": sum(1 for run in builds if run.get("status") == "ok"),
        "permission_violations": permission_violations,
        "sandbox_violations": sandbox_violations,
    }


def _classify_result(spec: EvalTaskSpec, runtime_status: str | None, evidence: list[Evidence]) -> tuple[EvalStatus, bool]:
    expectation_met = bool(evidence) and all(item.passed for item in evidence)
    if expectation_met:
        return spec.expected_outcome, True
    if runtime_status == "waiting_confirmation":
        return "blocked", False
    if runtime_status == "cancelled":
        return "cancelled", False
    if runtime_status == "timed_out":
        return "timed_out", False
    if any(item.passed for item in evidence):
        return "partially_passed", False
    return "failed", False


def _blocked_result(spec: EvalTaskSpec, reason: str) -> EvalTaskResult:
    stamp = now_iso()
    return EvalTaskResult(
        task_id=spec.id,
        title=spec.title,
        status="blocked",
        expected_outcome=spec.expected_outcome,
        expectation_met=False,
        runtime_status="blocked",
        started_at=stamp,
        finished_at=stamp,
        duration_ms=0,
        failed_step="provider_key",
        evidence=[Evidence(rule="provider_key", passed=False, message=reason)],
        metrics={"model_calls": 0, "tool_calls": 0, "tool_errors": 0, "retry_count": 0, "total_tokens": 0, "human_interventions": 0, "tests_run": 0, "tests_passed": 0, "builds_run": 0, "builds_passed": 0, "permission_violations": 0, "sandbox_violations": 0},
    )


async def _execute_task(spec: EvalTaskSpec, stage: Path, *, mode: EvalMode, api_key: str | None) -> EvalTaskResult:
    if mode == "live_model" and not api_key:
        return _blocked_result(spec, "真实模型评测缺少 AGENT_DEEPSEEK_API_KEY")
    started = _utc_now()
    workspace = _prepare_workspace(stage, spec)
    before = snapshot_workspace(workspace)
    runtime_status: str | None = None
    trace: dict[str, Any] = {}
    approval_count = 0
    special: dict[str, Any] = {}
    try:
        if spec.scenario == "interrupted_recovery":
            runtime_status, trace, approval_count, _, special = await _run_interrupted_recovery(spec, workspace)
        elif spec.scenario == "mcp_failure":
            runtime_status, trace, approval_count, _, special = await _run_mcp_failure(spec, workspace, mode=mode, api_key=api_key)
        elif spec.scenario == "sidecar_interruption":
            runtime_status, trace, approval_count, _, special = await _run_sidecar(spec, stage)
        elif spec.scenario == "timeout_cancel":
            runtime_status, trace, approval_count, _, special = await _run_timeout_cancel(spec, workspace)
        else:
            runtime_status, trace, approval_count, _ = await _run_runtime(spec, workspace, mode=mode, api_key=api_key)
    except Exception as exc:
        trace = {**trace, "evaluator_error": f"{type(exc).__name__}: {exc}"}
        runtime_status = runtime_status or "failed"
    after = snapshot_workspace(workspace)
    changed = changed_paths(before, after)
    expected = {Path(path).as_posix() for path in spec.expected_files}
    unrelated = sorted(path for path in changed if path not in expected)
    context = {
        **special,
        "runtime_status": runtime_status,
        "tool_runs": trace.get("tool_runs") or [],
        "agent_runs": trace.get("agent_runs") or [],
        "agent_events": trace.get("agent_events") or [],
        "file_locks": trace.get("file_locks") or [],
        "approval_count": approval_count,
        "changed_files": changed,
        "unrelated_files": unrelated,
    }
    evidence = evaluate_rules(spec, workspace, context)
    status, expectation_met = _classify_result(spec, runtime_status, evidence)
    finished = _utc_now()
    failed = next((item for item in evidence if not item.passed), None)
    metrics = _trace_metrics(trace, approval_count)
    return EvalTaskResult(
        task_id=spec.id,
        title=spec.title,
        status=status,
        expected_outcome=spec.expected_outcome,
        expectation_met=expectation_met,
        runtime_status=runtime_status,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        duration_ms=round((finished - started).total_seconds() * 1000),
        failed_step=failed.rule if failed else None,
        evidence=evidence,
        trace=trace,
        metrics=metrics,
        changed_files=changed,
        unrelated_files=unrelated,
        false_success=runtime_status == "completed" and not expectation_met,
    )


async def run_evaluation(
    *,
    label: str,
    mode: EvalMode = "scripted_runtime",
    suite: str = "core",
    task_ids: list[str] | None = None,
    tasks_path: str | Path | None = None,
    output_root: str | Path = "data/evals",
    api_key: str | None = None,
) -> EvalReport:
    tasks = load_tasks(tasks_path, suite=suite, task_ids=task_ids)
    output = Path(output_root).resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    stage = Path(tempfile.mkdtemp(prefix=f".run-{run_id[:8]}-", dir=output))
    original_database_path = settings.database_path
    settings.database_path = stage / "runtime.db"
    started = _utc_now()
    try:
        init_db()
        results = [await _execute_task(spec, stage, mode=mode, api_key=api_key) for spec in tasks]
        finished = _utc_now()
        blocked = mode == "live_model" and not api_key
        report = EvalReport(
            run_id=run_id,
            label=label,
            app_version=__version__,
            mode=mode,
            suite=suite,
            provider={"base_url": settings.model_base_url, "model": settings.model_name} if mode == "live_model" else {"name": "deterministic-script"},
            configuration={
                "task_count": len(tasks),
                "permission_modes": sorted({task.permission_mode for task in tasks}),
                "max_duplicate_tool_calls": settings.max_duplicate_tool_calls,
                "database_isolated": True,
            },
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            duration_ms=round((finished - started).total_seconds() * 1000),
            status="blocked" if blocked else "completed",
            metrics=aggregate_metrics(results),
            task_results=results,
        )
        return persist_report(report, output)
    finally:
        settings.database_path = original_database_path
        shutil.rmtree(stage, ignore_errors=True)
