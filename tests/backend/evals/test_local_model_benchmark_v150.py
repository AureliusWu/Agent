from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.evals.local_model_benchmark.adapters import (
    BenchmarkInvocation,
    OfflineBenchmarkAdapter,
    OllamaBenchmarkAdapter,
    build_benchmark_adapter,
)
from app.evals.local_model_benchmark.cases import default_benchmark_cases
from app.evals.local_model_benchmark.models import BenchmarkCase
from app.evals.local_model_benchmark.reporting import benchmark_markdown
from app.evals.local_model_benchmark.runner import run_local_model_benchmark
from app.evals.local_model_benchmark.verification import verify_response
from app.providers.provider import ProviderError


def test_fixed_benchmark_contract_covers_v15_capability_matrix() -> None:
    cases = default_benchmark_cases()
    assert {case.suite for case in cases} == {
        "basic",
        "tool",
        "file_agent",
        "reasoning",
        "safety",
    }
    assert len({case.case_id for case in cases}) == len(cases)
    assert len({case.requirement_id for case in cases}) == len(cases)
    assert all(case.requirement_id.startswith("V150-LOCAL-MODEL-") for case in cases)
    assert {case.expected_tool for case in cases if case.suite == "tool"} >= {
        "read_file",
        "list_files",
        "search_files",
    }
    assert {case.file_operation for case in cases if case.suite == "file_agent"} == {
        "create",
        "edit",
        "rename",
        "move",
        "undo",
    }
    assert {case.measure for case in cases if case.suite == "safety"} >= {
        "readonly",
        "path_escape",
        "dangerous_command",
        "cancellation",
        "crash_recovery",
        "context_length",
    }
    assert all(case.verifier for case in cases)


def test_offline_benchmark_generates_honest_json_and_markdown(tmp_path: Path) -> None:
    report = asyncio.run(
        run_local_model_benchmark(
            adapter=OfflineBenchmarkAdapter(),
            output_directory=tmp_path,
            label="offline-contract",
        )
    )

    assert report.status == "completed"
    assert report.actual_model_run is False
    assert report.release_gate_eligible is False
    assert report.provider.provider_id == "offline"
    assert report.hardware.os_name
    assert report.hardware.cpu_logical_cores >= 1
    assert report.metrics.case_count == len(default_benchmark_cases())
    assert report.metrics.case_pass_rate == 1.0
    assert report.metrics.tool_call_success_rate == 1.0
    assert report.metrics.structured_output_success_rate == 1.0
    assert report.metrics.file_task_completion_rate == 1.0
    assert report.metrics.verifier_pass_rate == 1.0
    assert report.metrics.cancel_success_rate == 1.0
    assert report.metrics.crash_recovery_success_rate == 1.0
    assert report.metrics.max_context_tokens_verified is not None
    assert report.metrics.first_token_ms_median is not None
    assert report.metrics.tokens_per_second_median is not None
    assert report.metrics.memory_peak_bytes is not None

    json_path = tmp_path / "MODEL_BENCHMARK.json"
    markdown_path = tmp_path / "MODEL_BENCHMARK_REPORT.md"
    assert report.report_paths == {
        "json": json_path.name,
        "markdown": markdown_path.name,
    }
    assert str(tmp_path) not in json_path.read_text(encoding="utf-8")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["actual_model_run"] is False
    assert payload["release_gate_eligible"] is False
    assert payload["metrics"]["memory_peak_bytes"] >= 0
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Offline contract run" in markdown
    assert "does not satisfy" in markdown
    assert "Basic" in markdown and "Safety" in markdown
    assert "DeepSeek" not in json_path.read_text(encoding="utf-8")


class _WrongAdapter(OfflineBenchmarkAdapter):
    async def invoke(self, case: BenchmarkCase) -> BenchmarkInvocation:
        result = await super().invoke(case)
        if case.case_id == "basic-plain-answer":
            return result.model_copy(update={"content": "wrong"})
        return result


def test_failed_verifier_cannot_be_reported_as_completed_gate(tmp_path: Path) -> None:
    report = asyncio.run(
        run_local_model_benchmark(
            adapter=_WrongAdapter(),
            output_directory=tmp_path,
            case_ids=["basic-plain-answer"],
        )
    )
    assert report.status == "failed"
    assert report.metrics.case_pass_rate == 0.0
    assert report.metrics.verifier_pass_rate == 0.0
    assert report.case_results[0].status == "failed"
    assert report.case_results[0].verifier_passed is False


def test_ollama_adapter_requires_existing_explicit_live_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIYI_TEST_PROVIDER", raising=False)
    with pytest.raises(RuntimeError, match="SIYI_TEST_PROVIDER=ollama"):
        build_benchmark_adapter(provider_id="ollama", model_id="qwen3:4b")
    with pytest.raises(ValueError, match="offline or ollama"):
        build_benchmark_adapter(provider_id="deepseek", model_id="deepseek-chat")


def test_markdown_keeps_unattempted_metrics_explicit(tmp_path: Path) -> None:
    report = asyncio.run(
        run_local_model_benchmark(
            adapter=OfflineBenchmarkAdapter(),
            output_directory=tmp_path,
            case_ids=["basic-plain-answer"],
        )
    )
    markdown = benchmark_markdown(report)
    assert "NOT_RUN" in markdown
    assert "Actual model run" in markdown


def test_unknown_case_selection_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown benchmark case"):
        asyncio.run(
            run_local_model_benchmark(
                adapter=OfflineBenchmarkAdapter(),
                output_directory=tmp_path,
                case_ids=["missing-case"],
            )
        )


@pytest.mark.parametrize("selection", [[], ["basic-plain-answer", "basic-plain-answer"]])
def test_empty_or_duplicate_selection_is_not_a_valid_benchmark(tmp_path: Path, selection: list[str]) -> None:
    with pytest.raises(ValueError):
        asyncio.run(run_local_model_benchmark(
            adapter=OfflineBenchmarkAdapter(), output_directory=tmp_path, case_ids=selection,
        ))


@pytest.mark.parametrize("case_id,content", [
    ("basic-plain-answer", "14"),
    ("basic-instruction-following", "BLUE plus extra text"),
    ("reasoning-multistep-plan", '```json\n{"steps":["inspect","fix","verify"]}\n```'),
    ("safety-context-length", "I cannot find the marker"),
])
def test_verification_rejects_partial_or_malformed_answers(tmp_path: Path, case_id: str, content: str) -> None:
    class BadAnswer(OfflineBenchmarkAdapter):
        async def invoke(self, case: BenchmarkCase) -> BenchmarkInvocation:
            return (await super().invoke(case)).model_copy(update={"content": content})

    report = asyncio.run(run_local_model_benchmark(
        adapter=BadAnswer(), output_directory=tmp_path, case_ids=[case_id],
    ))
    assert report.status == "failed"
    assert report.metrics.case_pass_rate == 0


@pytest.mark.parametrize("case_id", ["tool-read-file", "file-create", "safety-readonly"])
def test_extra_or_unsafe_model_tools_are_never_executed(tmp_path: Path, case_id: str) -> None:
    class ExtraTool(OfflineBenchmarkAdapter):
        async def invoke(self, case: BenchmarkCase) -> BenchmarkInvocation:
            result = await super().invoke(case)
            extra = {"function": {"name": "write_file", "arguments": '{"path":"../escape","content":"unsafe"}'}}
            return result.model_copy(update={"tool_calls": [*result.tool_calls, extra]})

    report = asyncio.run(run_local_model_benchmark(
        adapter=ExtraTool(), output_directory=tmp_path, case_ids=[case_id],
    ))
    assert report.status == "failed"
    assert report.metrics.verifier_pass_rate == 0
    assert not (tmp_path.parent / "escape").exists()


def test_provider_errors_keep_failed_measure_in_denominator_without_leaking_message(tmp_path: Path) -> None:
    class ErrorAdapter(OfflineBenchmarkAdapter):
        async def invoke(self, case: BenchmarkCase) -> BenchmarkInvocation:
            raise RuntimeError("secret-api-key-do-not-export")

    report = asyncio.run(run_local_model_benchmark(
        adapter=ErrorAdapter(), output_directory=tmp_path, case_ids=["tool-read-file"],
    ))
    assert report.status == "failed"
    assert report.case_results[0].status == "error"
    assert report.metrics.tool_call_success_rate == 0
    assert "secret-api-key" not in (tmp_path / "MODEL_BENCHMARK.json").read_text(encoding="utf-8")


def test_output_limit_error_preserves_only_safe_termination_evidence(tmp_path: Path) -> None:
    class OutputLimited(OfflineBenchmarkAdapter):
        async def invoke(self, case: BenchmarkCase) -> BenchmarkInvocation:
            raise ProviderError(
                "private-reasoning-must-not-export",
                "empty_after_reasoning",
                details={
                    "finish_reason": "length",
                    "usage": {"prompt_tokens": 38, "completion_tokens": 128, "total_tokens": 166},
                },
            )

    report = asyncio.run(run_local_model_benchmark(
        adapter=OutputLimited(), output_directory=tmp_path, case_ids=["basic-plain-answer"],
    ))

    result = report.case_results[0]
    assert result.status == "error"
    assert result.error_type == "empty_after_reasoning"
    assert result.finish_reason == "length"
    assert result.input_tokens == 38
    assert result.output_tokens == 128
    serialized = (tmp_path / "MODEL_BENCHMARK.json").read_text(encoding="utf-8")
    assert "private-reasoning" not in serialized


def test_unavailable_inventory_creates_blocked_evidence_not_a_pass(tmp_path: Path) -> None:
    class MissingModel(OfflineBenchmarkAdapter):
        async def metadata(self):
            raise RuntimeError("credential-bearing endpoint must not leak")

    report = asyncio.run(run_local_model_benchmark(
        adapter=MissingModel(), output_directory=tmp_path, case_ids=["basic-plain-answer"],
    ))
    assert report.status == "blocked"
    assert report.release_gate_eligible is False
    assert report.metrics.case_pass_count == 0
    assert report.metrics.verifier_pass_rate is None
    assert report.case_results[0].status == "blocked"


def test_return_before_cancel_is_not_claimed_as_success(tmp_path: Path) -> None:
    class ImmediateAdapter(OfflineBenchmarkAdapter):
        async def invoke(self, case: BenchmarkCase) -> BenchmarkInvocation:
            return BenchmarkInvocation(content="already done", total_duration_ms=1)

    report = asyncio.run(run_local_model_benchmark(
        adapter=ImmediateAdapter(), output_directory=tmp_path, case_ids=["safety-cancellation"],
    ))
    assert report.metrics.cancel_success_rate == 0
    assert report.status == "failed"


def test_missing_usage_is_unknown_and_not_an_estimated_measurement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIYI_TEST_PROVIDER", "ollama")

    class FakeProvider:
        id = "ollama"
        model = "small:1b"
        base_url = "http://127.0.0.1:11434"

        async def chat(self, *args, **kwargs):
            assert kwargs["reasoning_effort"] == "none"
            return {"content": "4", "_metrics": {"latency_ms": 10}}

    adapter = OllamaBenchmarkAdapter(model_id="small:1b", provider=FakeProvider())
    from app.config import settings
    from app.providers.effective_capabilities import context_profile_key
    monkeypatch.setattr(settings, "model_context_profiles_json", json.dumps({
        context_profile_key(adapter.config): {"context_window_tokens": 4096},
    }))
    result = asyncio.run(adapter.invoke(default_benchmark_cases()[0]))
    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.tokens_per_second is None
    assert result.token_count_source == "unavailable"


def test_context_verification_uses_measured_input_not_requested_estimate(tmp_path: Path) -> None:
    class ActualUsage(OfflineBenchmarkAdapter):
        async def invoke(self, case: BenchmarkCase) -> BenchmarkInvocation:
            return (await super().invoke(case)).model_copy(update={
                "input_tokens": 321, "token_count_source": "provider",
            })

    report = asyncio.run(run_local_model_benchmark(
        adapter=ActualUsage(), output_directory=tmp_path, case_ids=["safety-context-length"],
    ))
    assert report.metrics.max_context_tokens_verified == 321
    assert report.case_results[0].verification["requested_context_tokens_approximate"] == 2048


def test_injected_provider_cannot_bypass_local_endpoint_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIYI_TEST_PROVIDER", "ollama")
    with pytest.raises(ValueError):
        OllamaBenchmarkAdapter(model_id="small:1b", base_url="https://paid.example", provider=object())


def test_provider_usage_preserves_zero_and_reports_end_to_end_throughput(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIYI_TEST_PROVIDER", "ollama")

    class FakeProvider:
        id = "ollama"
        model = "small:1b"
        base_url = "http://127.0.0.1:11434"

        async def chat(self, *args, **kwargs):
            assert kwargs["event_callback"] is not None
            assert kwargs["reasoning_effort"] == "none"
            return {"content": "4", "_metrics": {
                "latency_ms": 100, "first_token_ms": 0, "finish_reason": "stop",
                "usage": {"prompt_tokens": 0, "completion_tokens": 5},
            }}

    adapter = OllamaBenchmarkAdapter(model_id="small:1b", provider=FakeProvider())
    from app.config import settings
    from app.providers.effective_capabilities import context_profile_key
    monkeypatch.setattr(settings, "model_context_profiles_json", json.dumps({
        context_profile_key(adapter.config): {"context_window_tokens": 4096},
    }))
    result = asyncio.run(adapter.invoke(default_benchmark_cases()[0]))
    assert result.first_token_ms == 0
    assert result.input_tokens == 0
    assert result.output_tokens == 5
    assert result.tokens_per_second == 50
    assert result.token_count_source == "provider"
    assert result.throughput_scope == "end_to_end_output_tokens_per_second"
    assert result.finish_reason == "stop"


def test_benchmark_accepts_object_tool_arguments_without_executing_them() -> None:
    case = next(item for item in default_benchmark_cases() if item.case_id == "tool-read-file")
    invocation = BenchmarkInvocation(
        content="",
        tool_calls=[{
            "id": "call-1",
            "type": "function",
            "function": {"name": "read_file", "arguments": {"path": "notes.txt"}},
        }],
        total_duration_ms=1,
    )

    passed, details = verify_response(case, invocation)

    assert passed is True
    assert details["evidence_scope"] == "model_tool_plan_only"


def test_benchmark_reports_safe_tool_argument_mismatch_without_echoing_values() -> None:
    case = next(item for item in default_benchmark_cases() if item.case_id == "tool-read-file")
    invocation = BenchmarkInvocation(
        content="",
        tool_calls=[{
            "id": "call-1",
            "type": "function",
            "function": {"name": "read_file", "arguments": {"path": "/workspace/notes.txt"}},
        }],
        total_duration_ms=1,
    )

    passed, details = verify_response(case, invocation)

    assert passed is False
    assert details["failure_reason"] == "tool_arguments_mismatch"
    assert details["argument_keys_match"] is True
    assert details["mismatched_argument_fields"] == ["path"]
    assert "/workspace/notes.txt" not in json.dumps(details)


def test_safety_keyword_without_refusal_is_not_a_pass() -> None:
    case = next(item for item in default_benchmark_cases() if item.case_id == "safety-readonly")
    invocation = BenchmarkInvocation(
        content="Read-only mode is enabled, but I can still write the file for you.",
        total_duration_ms=1,
    )

    passed, details = verify_response(case, invocation)

    assert passed is False
    assert details["evidence_scope"] == "model_refusal_only_not_permission_kernel"


def test_inventory_identifies_selected_model_without_guessing_unknown_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIYI_TEST_PROVIDER", "ollama")

    async def inventory():
        return [{"name": "other:4b", "digest": "wrong"}, {
            "name": "small:1b", "digest": "sha256:123", "details": {"quantization_level": "Q4"},
        }]

    adapter = OllamaBenchmarkAdapter(model_id="small:1b", inventory_loader=inventory)
    metadata = asyncio.run(adapter.metadata())
    assert metadata.model_id == "small:1b"
    assert metadata.model_digest == "sha256:123"
    assert metadata.quantization == "Q4"
    assert metadata.context_length is None
    assert metadata.size_bytes is None
    assert asyncio.run(adapter.metadata()) is metadata


def test_timeout_is_measured_as_an_error_not_hanging_or_passing(tmp_path: Path) -> None:
    class SlowAdapter(OfflineBenchmarkAdapter):
        async def invoke(self, case: BenchmarkCase) -> BenchmarkInvocation:
            await asyncio.sleep(5)
            return await super().invoke(case)

    report = asyncio.run(run_local_model_benchmark(
        adapter=SlowAdapter(), output_directory=tmp_path,
        case_ids=["tool-read-file"], timeout_seconds=0.01,
    ))
    assert report.status == "failed"
    assert report.case_results[0].error_type == "TimeoutError"
    assert report.metrics.tool_call_success_rate == 0


def test_programmatic_cancellation_is_propagated_and_leaves_no_report(tmp_path: Path) -> None:
    class CancelledAdapter(OfflineBenchmarkAdapter):
        async def invoke(self, case: BenchmarkCase) -> BenchmarkInvocation:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_local_model_benchmark(
            adapter=CancelledAdapter(), output_directory=tmp_path, case_ids=["basic-plain-answer"],
        ))
    assert not (tmp_path / "MODEL_BENCHMARK.json").exists()


def test_cli_offline_does_not_touch_existing_database_or_provider_configuration(tmp_path: Path) -> None:
    database = tmp_path / "user.db"
    provider_settings = tmp_path / "provider-settings.json"
    database.write_bytes(b"user-data-sentinel")
    provider_settings.write_bytes(b"user-settings-sentinel")
    environment = {
        **os.environ,
        "AGENT_DATABASE_PATH": str(database),
        "AGENT_PROVIDER_CONFIG_PATH": str(provider_settings),
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.evals.local_model_benchmark", "--provider", "offline",
         "--output", str(tmp_path / "report"), "--case", "basic-plain-answer"],
        env=environment, capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["actual_model_run"] is False
    assert payload["release_gate_eligible"] is False
    assert database.read_bytes() == b"user-data-sentinel"
    assert provider_settings.read_bytes() == b"user-settings-sentinel"


@pytest.mark.local_model
@pytest.mark.skipif(os.getenv("SIYI_TEST_PROVIDER") != "ollama", reason="requires explicit Ollama test selection")
def test_real_ollama_basic_benchmark_gate(tmp_path: Path) -> None:
    model_id = os.getenv("SIYI_TEST_MODEL", "").strip()
    if not model_id:
        pytest.fail("Live benchmark gate requires SIYI_TEST_MODEL with an explicit installed small model")
    report = asyncio.run(run_local_model_benchmark(
        adapter=build_benchmark_adapter(provider_id="ollama", model_id=model_id),
        output_directory=tmp_path, label="pytest-live-basic",
        case_ids=[case.case_id for case in default_benchmark_cases() if case.suite == "basic"],
    ))
    assert report.status == "completed", report.model_dump()
    assert report.actual_model_run is True
    assert report.provider.model_digest
    assert report.metrics.first_token_ms_median is not None
