from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import EvalReport, EvalTaskResult


def _rate(numerator: int | float, denominator: int | float) -> float:
    return round(float(numerator) / float(denominator), 4) if denominator else 0.0


def aggregate_metrics(results: list[EvalTaskResult]) -> dict[str, float | int | None]:
    total = len(results)
    tool_calls = sum(int(item.metrics.get("tool_calls") or 0) for item in results)
    tool_errors = sum(int(item.metrics.get("tool_errors") or 0) for item in results)
    tests_run = sum(int(item.metrics.get("tests_run") or 0) for item in results)
    tests_passed = sum(int(item.metrics.get("tests_passed") or 0) for item in results)
    builds_run = sum(int(item.metrics.get("builds_run") or 0) for item in results)
    builds_passed = sum(int(item.metrics.get("builds_passed") or 0) for item in results)
    recovery_results = [item for item in results if item.task_id == "interrupted-recovery"]
    return {
        "task_count": total,
        "task_success_count": sum(1 for item in results if item.expectation_met),
        "task_success_rate": _rate(sum(1 for item in results if item.expectation_met), total),
        "partial_completion_rate": _rate(sum(1 for item in results if item.status == "partially_passed"), total),
        "false_success_count": sum(1 for item in results if item.false_success),
        "false_success_rate": _rate(sum(1 for item in results if item.false_success), total),
        "tool_error_rate": _rate(tool_errors, tool_calls),
        "retry_count": sum(int(item.metrics.get("retry_count") or 0) for item in results),
        "model_call_count": sum(int(item.metrics.get("model_calls") or 0) for item in results),
        "tool_call_count": tool_calls,
        "token_cost": sum(int(item.metrics.get("total_tokens") or 0) for item in results),
        "execution_time_ms": sum(item.duration_ms for item in results),
        "average_task_time_ms": round(sum(item.duration_ms for item in results) / total) if total else 0,
        "human_intervention_count": sum(int(item.metrics.get("human_interventions") or 0) for item in results),
        "unrelated_file_modification_count": sum(len(item.unrelated_files) for item in results),
        "test_pass_rate": _rate(tests_passed, tests_run),
        "build_pass_rate": _rate(builds_passed, builds_run),
        "permission_violation_count": sum(int(item.metrics.get("permission_violations") or 0) for item in results),
        "sandbox_violation_count": sum(int(item.metrics.get("sandbox_violations") or 0) for item in results),
        "recovery_success_rate": _rate(sum(1 for item in recovery_results if item.expectation_met), len(recovery_results)),
    }


def report_markdown(report: EvalReport) -> str:
    metrics = report.metrics
    lines = [
        f"# Agent Eval: {report.label}",
        "",
        f"- Run ID: `{report.run_id}`",
        f"- Version: `{report.app_version}`",
        f"- Mode: `{report.mode}`",
        f"- Suite: `{report.suite}`",
        f"- Status: `{report.status}`",
        f"- Task success: {float(metrics['task_success_rate']) * 100:.1f}%",
        f"- False success: {float(metrics['false_success_rate']) * 100:.1f}%",
        f"- Tool error: {float(metrics['tool_error_rate']) * 100:.1f}%",
        f"- Tokens: {metrics['token_cost']}",
        f"- Duration: {report.duration_ms} ms",
        "",
        "## Tasks",
        "",
        "| Task | Result | Expected | Runtime | Duration | Failed step |",
        "|---|---|---|---|---:|---|",
    ]
    for item in report.task_results:
        failed = (item.failed_step or "-").replace("|", "\\|")
        lines.append(f"| `{item.task_id}` | {item.status} | {item.expected_outcome} | {item.runtime_status or '-'} | {item.duration_ms} ms | {failed} |")
    lines.extend(["", "## Failures", ""])
    failures = [item for item in report.task_results if not item.expectation_met]
    if not failures:
        lines.append("No failed expectations.")
    for item in failures:
        lines.append(f"### {item.task_id}")
        for evidence in item.evidence:
            if not evidence.passed:
                lines.append(f"- `{evidence.rule}`: {evidence.message}")
        if item.unrelated_files:
            lines.append(f"- Unrelated files: {', '.join(item.unrelated_files)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def persist_report(report: EvalReport, output_root: Path) -> EvalReport:
    safe_label = re.sub(r"[^a-zA-Z0-9._-]+", "-", report.label).strip("-") or "eval"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / f"{stamp}-{safe_label}-{report.run_id[:8]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    json_path = run_dir / "report.json"
    markdown_path = run_dir / "report.md"
    report.report_paths = {"json": str(json_path.resolve()), "markdown": str(markdown_path.resolve())}
    json_path.write_text(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(report_markdown(report), encoding="utf-8")
    history_path = output_root / "history.jsonl"
    summary: dict[str, Any] = {
        "run_id": report.run_id,
        "label": report.label,
        "app_version": report.app_version,
        "mode": report.mode,
        "suite": report.suite,
        "started_at": report.started_at,
        "status": report.status,
        "metrics": report.metrics,
        "report": str(json_path.resolve()),
    }
    with history_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return report
