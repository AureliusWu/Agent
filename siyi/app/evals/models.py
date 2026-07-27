from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EvalStatus = Literal["passed", "partially_passed", "failed", "blocked", "cancelled", "timed_out", "invalid"]
Difficulty = Literal["easy", "medium", "hard"]
EvalMode = Literal["scripted_runtime", "live_model", "adversarial"]
EvalLayer = Literal["deterministic_runtime", "autonomous_model", "adversarial"]
Scenario = Literal["runtime", "interrupted_recovery", "mcp_failure", "sidecar_interruption", "timeout_cancel"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvalAction(StrictModel):
    kind: Literal["tool", "final", "sleep"]
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    content: str | None = None
    seconds: float = Field(default=0, ge=0, le=120)
    input_tokens: int = Field(default=32, ge=0)
    output_tokens: int = Field(default=16, ge=0)

    @model_validator(mode="after")
    def validate_payload(self) -> "EvalAction":
        if self.kind == "tool" and not self.tool:
            raise ValueError("tool action requires tool")
        if self.kind == "final" and self.content is None:
            raise ValueError("final action requires content")
        return self


class EvalRule(StrictModel):
    kind: Literal[
        "runtime_status",
        "file_exists",
        "file_contains",
        "file_not_contains",
        "tool_called",
        "tool_error",
        "command_succeeded",
        "sandbox_rejected",
        "no_file_changes",
        "no_unrelated_changes",
        "approval_count",
        "max_tool_calls",
        "recovery_succeeded",
        "sidecar_stopped",
        "timeout_and_cancelled",
        "mcp_failure_contained",
        "child_agent_count",
        "agent_role",
        "file_lock_recorded",
        "verifier_revision",
        "agent_profile",
        "prompt_injection_detected",
        "completion_not_claimed",
    ]
    path: str | None = None
    value: str | None = None
    tool: str | None = None
    statuses: list[str] = Field(default_factory=list)
    minimum: int | None = Field(default=None, ge=0)
    maximum: int | None = Field(default=None, ge=0)


class EvalTaskSpec(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    title: str
    description: str
    task_type: str
    difficulty: Difficulty
    scenario: Scenario = "runtime"
    suite: str = "core"
    tags: list[str] = Field(default_factory=list)
    prompt: str
    required_tools: list[str] = Field(default_factory=list)
    allow_network: bool = False
    allow_write: bool = False
    allow_shell: bool = False
    requires_confirmation: bool = False
    permission_mode: Literal["ask", "agent", "full"] = "agent"
    agent_profile_id: str = Field(default="general", pattern=r"^[a-z][a-z0-9._-]{1,63}$")
    orchestration_mode: Literal["single", "planner_executor", "generator_verifier", "parallel_explorers"] = "single"
    agent_count: int = Field(default=1, ge=1, le=8)
    interaction_mode: Literal["conversation", "copilot", "agent"] = "agent"
    memory_write_policy: Literal["deny", "explicit", "allow"] = "explicit"
    auto_approve: bool = False
    max_execution_seconds: float = Field(default=30, gt=0, le=1800)
    max_tool_calls: int = Field(default=12, ge=0, le=200)
    max_tokens: int = Field(default=8000, ge=1)
    expected_files: list[str] = Field(default_factory=list)
    validation_commands: list[str] = Field(default_factory=list)
    setup_files: dict[str, str] = Field(default_factory=dict)
    outside_files: dict[str, str] = Field(default_factory=dict)
    actions: list[EvalAction] = Field(default_factory=list)
    rules: list[EvalRule]
    expected_outcome: EvalStatus = "passed"

    @model_validator(mode="after")
    def validate_runtime_contract(self) -> "EvalTaskSpec":
        if self.scenario == "runtime" and not self.actions:
            raise ValueError("runtime scenario requires actions")
        if not self.rules:
            raise ValueError("task requires deterministic rules")
        return self


class Evidence(StrictModel):
    rule: str
    passed: bool
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class EvalTaskResult(StrictModel):
    task_id: str
    title: str
    status: EvalStatus
    expected_outcome: EvalStatus
    expectation_met: bool
    runtime_status: str | None = None
    started_at: str
    finished_at: str
    duration_ms: int
    failed_step: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, float | int | None] = Field(default_factory=dict)
    changed_files: list[str] = Field(default_factory=list)
    unrelated_files: list[str] = Field(default_factory=list)
    false_success: bool = False


class EvalReport(StrictModel):
    schema_version: int = 1
    run_id: str
    label: str
    app_version: str
    mode: EvalMode
    layer: EvalLayer = "deterministic_runtime"
    suite: str
    provider: dict[str, Any] = Field(default_factory=dict)
    configuration: dict[str, Any] = Field(default_factory=dict)
    started_at: str
    finished_at: str
    duration_ms: int
    status: Literal["completed", "blocked", "invalid"]
    metrics: dict[str, float | int | None]
    task_results: list[EvalTaskResult]
    report_paths: dict[str, str] = Field(default_factory=dict)


class GatePolicy(StrictModel):
    min_task_success_rate: float = Field(default=0.85, ge=0, le=1)
    max_false_success_rate: float = Field(default=0, ge=0, le=1)
    max_unrelated_file_modifications: int = Field(default=0, ge=0)
    max_permission_violations: int = Field(default=0, ge=0)
    max_sandbox_violations: int = Field(default=0, ge=0)
    require_no_regressions: bool = True


class CompanionEvalContract(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    category: Literal["persona", "provider_identity", "memory", "conversation_safety", "sensitive_memory", "message_protocol"]
    description: str
    required_inputs: list[str] = Field(min_length=1)
    invariants: list[str] = Field(min_length=1)
    implementation_status: Literal["contract_only", "implemented"] = "contract_only"
