from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


BenchmarkSuite = Literal["basic", "tool", "file_agent", "reasoning", "safety"]
BenchmarkKind = Literal["completion", "tool_call", "structured", "cancellation", "context"]
BenchmarkStatus = Literal["completed", "failed", "blocked"]
BenchmarkCaseStatus = Literal["passed", "failed", "error", "blocked"]
BenchmarkFinishReason = Literal[
    "stop", "length", "tool_calls", "content_filter", "function_call", "unknown"
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class BenchmarkCase(StrictModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,79}$")
    requirement_id: str = ""
    target_version: str = Field(default="15.0.0", pattern=r"^\d+\.\d+\.\d+$")
    protocol_version: Literal["local-model-v1", "local-model-v2"] = "local-model-v1"
    title: str
    suite: BenchmarkSuite
    kind: BenchmarkKind = "completion"
    measure: str
    prompt: str
    system_prompt: str = "Follow the request exactly and keep the answer concise."
    tools: list[dict[str, Any]] = Field(default_factory=list)
    expected_contains: list[str] = Field(default_factory=list)
    expected_any: list[str] = Field(default_factory=list)
    expected_json: dict[str, Any] = Field(default_factory=dict)
    expected_tool: str | None = None
    expected_arguments: dict[str, Any] = Field(default_factory=dict)
    forbidden_tools: list[str] = Field(default_factory=list)
    verifier: Literal[
        "exact",
        "contains",
        "markdown",
        "tool",
        "structured",
        "file_operation",
        "safety_rejection",
        "cancellation",
        "crash_recovery",
        "context",
    ]
    file_operation: Literal["create", "edit", "rename", "move", "undo"] | None = None
    initial_files: dict[str, str] = Field(default_factory=dict)
    expected_files: dict[str, str] = Field(default_factory=dict)
    undo_restore: dict[str, str] = Field(default_factory=dict)
    context_tokens: int | None = Field(default=None, ge=128, le=131_072)
    context_marker: str | None = None
    cancel_after_ms: int = Field(default=100, ge=10, le=5_000)
    cancel_timeout_ms: int = Field(default=3_000, ge=100, le=30_000)

    @model_validator(mode="after")
    def validate_contract(self) -> "BenchmarkCase":
        major, minor, patch = self.target_version.split(".")
        version = f"V{major}{minor}" + (f"P{patch}" if patch != "0" else "")
        protocol = "LOCAL-MODEL" if self.protocol_version == "local-model-v1" else "LOCAL-MODEL-V2"
        expected_id = f"{version}-{protocol}-{self.case_id.upper()}"
        if self.requirement_id and self.requirement_id != expected_id:
            raise ValueError("Benchmark requirement_id must match the fixed case")
        self.requirement_id = expected_id
        if self.kind == "tool_call" and not self.tools:
            raise ValueError("tool_call benchmark requires tools")
        if self.verifier in {"tool", "file_operation"} and not self.expected_tool:
            raise ValueError("tool verifier requires expected_tool")
        if self.verifier == "file_operation" and not self.file_operation:
            raise ValueError("file_operation verifier requires file_operation")
        if self.kind == "context" and (not self.context_tokens or not self.context_marker):
            raise ValueError("context benchmark requires context_tokens and context_marker")
        return self


class BenchmarkInvocation(StrictModel):
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    finish_reason: BenchmarkFinishReason | None = None
    first_token_ms: float | None = Field(default=None, ge=0)
    total_duration_ms: float = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    token_count_source: Literal["provider", "offline_fixture", "unavailable"] = "unavailable"
    throughput_scope: str = "end_to_end_output_tokens_per_second"
    tokens_per_second: float | None = Field(default=None, ge=0)


class BenchmarkProvider(StrictModel):
    provider_id: str
    model_id: str
    display_name: str
    model_digest: str = ""
    quantization: str = ""
    size_bytes: int | None = Field(default=None, ge=0)
    context_length: int | None = Field(default=None, ge=0)
    metadata_source: str
    effective_capabilities: dict[str, Any] = Field(default_factory=dict)


class HardwareSnapshot(StrictModel):
    os_name: str
    os_release: str
    os_version: str
    architecture: str
    cpu_name: str
    cpu_logical_cores: int = Field(ge=1)
    ram_total_bytes: int | None = Field(default=None, ge=0)
    gpu_name: str | None = None
    gpu_memory_total_bytes: int | None = Field(default=None, ge=0)


class BenchmarkCaseResult(StrictModel):
    case_id: str
    requirement_id: str
    title: str
    suite: BenchmarkSuite
    measure: str
    status: BenchmarkCaseStatus
    verifier_passed: bool
    error_type: str | None = None
    finish_reason: BenchmarkFinishReason | None = None
    first_token_ms: float | None = Field(default=None, ge=0)
    total_duration_ms: float = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    token_count_source: Literal["provider", "offline_fixture", "unavailable"] = "unavailable"
    throughput_scope: str = "end_to_end_output_tokens_per_second"
    tokens_per_second: float | None = Field(default=None, ge=0)
    memory_peak_bytes: int | None = Field(default=None, ge=0)
    memory_scope: str | None = None
    tool_call_success: bool | None = None
    structured_output_success: bool | None = None
    file_task_completed: bool | None = None
    cancel_success: bool | None = None
    crash_recovery_success: bool | None = None
    context_tokens_verified: int | None = Field(default=None, ge=0)
    verification: dict[str, Any] = Field(default_factory=dict)


class BenchmarkMetrics(StrictModel):
    case_count: int = Field(ge=0)
    case_pass_count: int = Field(ge=0)
    case_pass_rate: float = Field(ge=0, le=1)
    first_token_ms_median: float | None = Field(default=None, ge=0)
    first_token_ms_worst: float | None = Field(default=None, ge=0)
    total_duration_ms: float = Field(ge=0)
    tokens_per_second_median: float | None = Field(default=None, ge=0)
    tokens_per_second_worst: float | None = Field(default=None, ge=0)
    memory_peak_bytes: int | None = Field(default=None, ge=0)
    tool_call_success_rate: float | None = Field(default=None, ge=0, le=1)
    structured_output_success_rate: float | None = Field(default=None, ge=0, le=1)
    file_task_completion_rate: float | None = Field(default=None, ge=0, le=1)
    verifier_pass_rate: float | None = Field(default=None, ge=0, le=1)
    cancel_success_rate: float | None = Field(default=None, ge=0, le=1)
    crash_recovery_success_rate: float | None = Field(default=None, ge=0, le=1)
    max_context_tokens_verified: int | None = Field(default=None, ge=0)
    context_success_rate: float | None = Field(default=None, ge=0, le=1)
    suite_success_rates: dict[BenchmarkSuite, float]


class LocalModelBenchmarkReport(StrictModel):
    schema_version: int = 1
    benchmark_version: str = "local-model-v1"
    target_version: str = "15.0.0"
    evidence_layer: str = "in_memory_model_simulation"
    run_id: str
    label: str
    app_version: str
    started_at: str
    finished_at: str
    status: BenchmarkStatus
    actual_model_run: bool
    release_gate_eligible: bool
    provider: BenchmarkProvider
    hardware: HardwareSnapshot
    metrics: BenchmarkMetrics
    case_results: list[BenchmarkCaseResult]
    limitations: list[str] = Field(default_factory=list)
    report_paths: dict[str, str] = Field(default_factory=dict)

    def eligible_for(self, target_version: str, protocol: str, current_identity: str) -> bool:
        """Legacy simulation remains readable but cannot establish file qualification."""
        return bool(
            self.target_version == target_version and self.benchmark_version == protocol
            and self.evidence_layer == "runtime_filesystem" and self.actual_model_run
            and self.release_gate_eligible and current_identity
            and self.provider.effective_capabilities.get("identity_hash") == current_identity
        )
