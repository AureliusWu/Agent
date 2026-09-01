from __future__ import annotations

from pathlib import Path

from .models import LocalModelBenchmarkReport


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _number(value: int | float | None, suffix: str = "") -> str:
    return "NOT_RUN / UNKNOWN" if value is None else f"{value:,.3f}{suffix}"


def benchmark_markdown(report: LocalModelBenchmarkReport) -> str:
    hardware, metrics, provider = report.hardware, report.metrics, report.provider
    lines = [
        "# Local Model Benchmark v1", "",
        f"- Run: {_cell(report.run_id)}; app version: {_cell(report.app_version)}",
        f"- Label: {_cell(report.label)}",
        f"- Started: {report.started_at}; finished: {report.finished_at}",
        f"- Status: **{report.status.upper()}**",
        f"- Actual model run: **{str(report.actual_model_run).lower()}**",
        f"- Full benchmark release gate eligible: **{str(report.release_gate_eligible).lower()}**",
        "",
    ]
    if not report.actual_model_run:
        lines += ["Offline contract run / no successful live setup; this does not satisfy the real Ollama release gate.", ""]
    lines += [
        "## Identity and hardware", "",
        "| Field | Value |", "| --- | --- |",
        f"| Provider / model | {_cell(provider.provider_id)} / {_cell(provider.model_id)} |",
        f"| Digest / quantization | {_cell(provider.model_digest or 'UNKNOWN')} / {_cell(provider.quantization or 'UNKNOWN')} |",
        f"| Model bytes / advertised context | {_number(provider.size_bytes)} / {_number(provider.context_length)} |",
        f"| Metadata source | {_cell(provider.metadata_source)} |",
        f"| OS | {_cell(hardware.os_name)} {_cell(hardware.os_release)} {_cell(hardware.os_version)} |",
        f"| CPU | {_cell(hardware.cpu_name)}; {hardware.cpu_logical_cores} logical cores |",
        f"| RAM bytes | {_number(hardware.ram_total_bytes)} |",
        f"| GPU | {_cell(hardware.gpu_name or 'UNKNOWN')} |",
        f"| GPU memory bytes | {_number(hardware.gpu_memory_total_bytes)} |", "",
        "## Measurements", "",
        "| Metric | Value |", "| --- | --- |",
        f"| Passed cases | {metrics.case_pass_count} / {metrics.case_count} |",
        f"| First token median / worst (ms) | {_number(metrics.first_token_ms_median)} / {_number(metrics.first_token_ms_worst)} |",
        f"| Total wall duration (ms) | {_number(metrics.total_duration_ms)} |",
        f"| End-to-end output tokens/s median / worst | {_number(metrics.tokens_per_second_median)} / {_number(metrics.tokens_per_second_worst)} |",
        f"| Sampled process RSS peak bytes (not full model memory) | {_number(metrics.memory_peak_bytes)} |",
        f"| Largest successful context probe input tokens | {_number(metrics.max_context_tokens_verified)} |",
    ]
    for title, field in (
        ("Tool Calling", "tool_call_success_rate"), ("JSON / Structured Output", "structured_output_success_rate"),
        ("File task simulation", "file_task_completion_rate"), ("Verifier", "verifier_pass_rate"),
        ("Cancel", "cancel_success_rate"), ("Crash recovery decision", "crash_recovery_success_rate"),
        ("Context retention", "context_success_rate"),
    ):
        value = getattr(metrics, field)
        lines.append(f"| {title} success rate | {'NOT_RUN' if value is None else f'{value:.1%}'} |")
    lines += ["", "## Suites", "", "| Suite | Success rate |", "| --- | --- |"]
    for suite, title in (("basic", "Basic"), ("tool", "Tool"), ("file_agent", "File Agent"),
                         ("reasoning", "Reasoning"), ("safety", "Safety")):
        rate = metrics.suite_success_rates.get(suite)
        lines.append(f"| {title} | {'NOT_RUN' if rate is None else f'{rate:.1%}'} |")
    lines += ["", "## Cases", "", "| Case | Status | Finish reason | First token ms | Duration ms | Input / output tokens |", "| --- | --- | --- | --- | --- | --- |"]
    for result in report.case_results:
        lines.append(f"| {_cell(result.case_id)} | {result.status.upper()} | {_cell(result.finish_reason or 'UNKNOWN')} | {_number(result.first_token_ms)} | {_number(result.total_duration_ms)} | {_number(result.input_tokens)} / {_number(result.output_tokens)} |")
    lines += ["", "## Evidence boundaries", ""]
    lines.extend(f"- {item}" for item in report.limitations)
    return "\n".join(lines) + "\n"


def write_benchmark_report(report: LocalModelBenchmarkReport, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = (directory / "MODEL_BENCHMARK.json").resolve()
    markdown_path = (directory / "MODEL_BENCHMARK_REPORT.md").resolve()
    # Persist portable artifact identities only. Absolute host/user paths are
    # neither release evidence nor safe content for a shareable report.
    report.report_paths = {
        "json": json_path.name,
        "markdown": markdown_path.name,
    }
    # Writes are only benchmark artifacts, never model-supplied file operations.
    json_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(benchmark_markdown(report), encoding="utf-8")
