from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from app import __version__
from app.providers.provider import ProviderError

from .adapters import BenchmarkAdapter
from .cases import default_benchmark_cases
from .hardware import ProcessMemorySampler, collect_hardware_snapshot
from .models import (
    BenchmarkCase, BenchmarkCaseResult, BenchmarkMetrics, BenchmarkProvider,
    LocalModelBenchmarkReport,
)
from .reporting import write_benchmark_report
from .verification import verify_response


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _measure_fields(case: BenchmarkCase, passed: bool) -> dict[str, bool]:
    fields: dict[str, bool] = {}
    if case.verifier in {"tool", "file_operation"}:
        fields["tool_call_success"] = passed
    if case.kind == "structured":
        fields["structured_output_success"] = passed
    for verifier, field in (
        ("file_operation", "file_task_completed"),
        ("cancellation", "cancel_success"),
        ("crash_recovery", "crash_recovery_success"),
    ):
        if case.verifier == verifier:
            fields[field] = passed
    return fields


async def _cancel_probe(adapter: BenchmarkAdapter, case: BenchmarkCase) -> tuple[bool, dict]:
    task = asyncio.create_task(adapter.invoke(case))
    try:
        await asyncio.sleep(case.cancel_after_ms / 1000)
        if task.done():
            await task  # An error is still an error, not cancellation evidence.
            return False, {"reason": "request_completed_before_cancel"}
        started = time.perf_counter()
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=case.cancel_timeout_ms / 1000)
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        cancelled = bool(done) and task.cancelled()
        if done and not task.cancelled():
            await task
        return cancelled, {
            "cancellation_latency_ms": elapsed,
            "evidence_scope": "in_flight_client_request_cancellation_not_server_process_exit",
        }
    finally:
        if not task.done():
            task.cancel()
        # The supported Ollama transport is cancellation-cooperative. Awaiting
        # cleanup prevents orphaned requests between sequential benchmark cases.
        await asyncio.gather(task, return_exceptions=True)


async def _run_case(adapter: BenchmarkAdapter, case: BenchmarkCase, timeout_seconds: float) -> BenchmarkCaseResult:
    started = time.perf_counter()
    stop = asyncio.Event()
    sampler = ProcessMemorySampler(adapter.resource_pid())
    sampling = asyncio.create_task(sampler.sample_until(stop))
    common = dict(case_id=case.case_id, requirement_id=case.requirement_id,
                  title=case.title, suite=case.suite, measure=case.measure)
    try:
        if case.kind == "cancellation":
            passed, verification = await _cancel_probe(adapter, case)
            result = BenchmarkCaseResult(
                **common, status="passed" if passed else "failed", verifier_passed=passed,
                total_duration_ms=round((time.perf_counter() - started) * 1000, 3),
                verification=verification, **_measure_fields(case, passed),
            )
        else:
            response = await asyncio.wait_for(adapter.invoke(case), timeout=timeout_seconds)
            passed, verification = verify_response(case, response)
            result = BenchmarkCaseResult(
                **common, status="passed" if passed else "failed", verifier_passed=passed,
                **response.model_dump(exclude={"content", "tool_calls"}),
                verification=verification, **_measure_fields(case, passed),
                context_tokens_verified=(
                    response.input_tokens if passed and case.verifier == "context" else None
                ),
            )
    except ProviderError as exc:
        # ProviderError details are a deliberately safe envelope. In
        # particular, output-limit errors may expose only termination metadata
        # and numeric usage, never response text or private reasoning.
        details = exc.details if isinstance(exc.details, dict) else {}
        safe_usage = details.get("usage") if isinstance(details.get("usage"), dict) else {}
        result = BenchmarkCaseResult(
            **common,
            status="blocked" if exc.error_type in {"context_window_unknown", "context_window_exceeded"} else "error",
            verifier_passed=False,
            error_type=exc.error_type,
            finish_reason=_safe_finish_reason(details.get("finish_reason")),
            total_duration_ms=round((time.perf_counter() - started) * 1000, 3),
            input_tokens=_safe_count(safe_usage, "input_tokens", "prompt_tokens"),
            output_tokens=_safe_count(safe_usage, "output_tokens", "completion_tokens"),
            token_count_source="provider" if safe_usage else "unavailable",
            verification={"provider_termination": _safe_finish_reason(details.get("finish_reason")) or "unknown"},
            **_measure_fields(case, False),
        )
    except Exception as exc:
        # Provider errors can carry credentials, paths, or raw response text.
        # Store only the exception class; never echo the exception message.
        result = BenchmarkCaseResult(
            **common, status="error", verifier_passed=False, error_type=type(exc).__name__,
            total_duration_ms=round((time.perf_counter() - started) * 1000, 3),
            **_measure_fields(case, False),
        )
    finally:
        stop.set()
        await sampling
    result.memory_peak_bytes = sampler.peak_bytes
    result.memory_scope = adapter.memory_scope
    return result


def _safe_count(values: dict, *keys: str) -> int | None:
    for key in keys:
        value = values.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _safe_finish_reason(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value in {"stop", "length", "tool_calls", "content_filter", "function_call"}:
        return value
    return "unknown"


def summarize_results(results: list[BenchmarkCaseResult], total_duration_ms: float) -> BenchmarkMetrics:
    attempted = [item for item in results if item.status != "blocked"]

    def rate(field: str) -> float | None:
        values = [getattr(item, field) for item in attempted if getattr(item, field) is not None]
        return sum(values) / len(values) if values else None

    first = [item.first_token_ms for item in attempted if item.first_token_ms is not None]
    speed = [item.tokens_per_second for item in attempted if item.tokens_per_second is not None]
    memory = [item.memory_peak_bytes for item in attempted if item.memory_peak_bytes is not None]
    context = [item for item in attempted if item.measure == "context_length"]
    verified_context = [item.context_tokens_verified for item in context if item.context_tokens_verified is not None]
    passed = sum(item.status == "passed" for item in results)
    return BenchmarkMetrics(
        case_count=len(results), case_pass_count=passed,
        case_pass_rate=passed / len(results) if results else 0,
        first_token_ms_median=median(first) if first else None,
        first_token_ms_worst=max(first) if first else None,
        total_duration_ms=total_duration_ms,
        tokens_per_second_median=median(speed) if speed else None,
        tokens_per_second_worst=min(speed) if speed else None,
        memory_peak_bytes=max(memory) if memory else None,
        tool_call_success_rate=rate("tool_call_success"),
        structured_output_success_rate=rate("structured_output_success"),
        file_task_completion_rate=rate("file_task_completed"),
        verifier_pass_rate=rate("verifier_passed"),
        cancel_success_rate=rate("cancel_success"),
        crash_recovery_success_rate=rate("crash_recovery_success"),
        max_context_tokens_verified=max(verified_context) if verified_context else None,
        context_success_rate=sum(item.verifier_passed for item in context) / len(context) if context else None,
        suite_success_rates={
            suite: sum(item.status == "passed" for item in results if item.suite == suite)
            / sum(item.suite == suite for item in results)
            for suite in dict.fromkeys(item.suite for item in results)
        },
    )


async def run_local_model_benchmark(
    *, adapter: BenchmarkAdapter, output_directory: str | Path = "data/evals/local-model-benchmark",
    label: str = "local-model-benchmark", case_ids: list[str] | None = None,
    timeout_seconds: float = 200,
) -> LocalModelBenchmarkReport:
    if not 0 < timeout_seconds <= 600:
        raise ValueError("Benchmark timeout must be between 0 and 600 seconds")
    all_cases = [BenchmarkCase.model_validate({**case.model_dump(), "requirement_id": "",
                  "target_version": "16.0.0", "protocol_version": "local-model-v2"})
                 for case in default_benchmark_cases()]
    known = {case.case_id for case in all_cases}
    if case_ids is not None:
        if not case_ids or len(set(case_ids)) != len(case_ids):
            raise ValueError("Benchmark case selection must be nonempty and unique")
        if set(case_ids) - known:
            raise ValueError("Unknown benchmark case selection")
    cases = [case for case in all_cases if case_ids is None or case.case_id in case_ids]
    started_at, started = _now(), time.perf_counter()
    hardware = collect_hardware_snapshot()
    limitations = [
        "File tasks use in-memory file simulation; tools are never dispatched to the real workspace.",
        "Safety checks measure model refusal, not Permission Kernel enforcement.",
        "Crash recovery measures a model policy decision, not an actual process crash/restart.",
        "Cancellation measures in-flight client request termination, not Ollama server process exit.",
        "Context is one marker-retention probe; requested size is approximate and is not maximum model context.",
        "Tokens/s is end-to-end output throughput including prompt processing and first-token wait, not decode speed.",
        "Memory is sampled process RSS only; Ollama listener RSS excludes model worker processes and GPU VRAM.",
        "One sample per scenario is directional capability evidence, not a statistical performance comparison.",
    ]
    results: list[BenchmarkCaseResult] = []
    try:
        provider = await asyncio.wait_for(adapter.metadata(), timeout=min(timeout_seconds, 30))
    except Exception as exc:
        provider = BenchmarkProvider(
            provider_id=adapter.provider_id, model_id=adapter.model_id,
            display_name=adapter.model_id, metadata_source="unavailable",
        )
        results = [BenchmarkCaseResult(
            case_id=case.case_id, requirement_id=case.requirement_id,
            title=case.title, suite=case.suite, measure=case.measure,
            status="blocked", verifier_passed=False, error_type=type(exc).__name__,
        ) for case in cases]
        status = "blocked"
    else:
        for case in cases:
            results.append(await _run_case(adapter, case, timeout_seconds))
        status = "completed" if all(item.status == "passed" for item in results) else "failed"
    actual = adapter.actual_model_run and adapter.model_requests > 0
    if not adapter.actual_model_run:
        limitations.insert(0, "Offline contract run uses synthetic fixture telemetry and does not satisfy the real Ollama release gate.")
    report = LocalModelBenchmarkReport(
        run_id=uuid.uuid4().hex, label=label, app_version=__version__,
        target_version="16.0.0", benchmark_version="local-model-v2",
        started_at=started_at, finished_at=_now(), status=status,
        actual_model_run=actual,
        # This protocol is still model-output simulation, never Runtime file qualification.
        release_gate_eligible=False,
        provider=provider, hardware=hardware,
        metrics=summarize_results(results, round((time.perf_counter() - started) * 1000, 3)),
        case_results=results, limitations=limitations,
    )
    write_benchmark_report(report, Path(output_directory))
    return report
