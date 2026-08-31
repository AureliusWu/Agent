from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PermissionMode = Literal["readonly", "ask", "agent", "full"]
ApprovalScope = Literal["once", "task", "session", "workspace", "always", "deny"]
OrchestrationMode = Literal["auto", "single", "planner_executor", "generator_verifier", "parallel_explorers"]
InteractionMode = Literal["conversation", "copilot", "agent"]
DataLocation = Literal["local_workspace", "uploaded_file", "remote_service"]
PrivacyScope = Literal["workspace", "private", "remote_allowed"]
MemoryWritePolicy = Literal["deny", "explicit", "allow"]
ReasoningEffort = Literal["auto", "low", "medium", "high"]
QueuePriority = Literal["now", "next", "later"]
VisionAction = Literal["describe", "extract_text", "analyze_chart", "compare", "inspect_ui", "classify"]
VisionProviderMode = Literal["active", "remote", "local"]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationCreate(RequestModel):
    title: str = "新对话"
    workspace: str = ""
    permission_mode: PermissionMode | None = None
    agent_profile_id: str = Field(default="general", pattern=r"^[a-z][a-z0-9._-]{1,63}$")


class ChatRequest(RequestModel):
    conversation_id: int
    content: str = Field(min_length=1)
    task_id: str | None = Field(default=None, pattern=r"^[a-f0-9-]{16,64}$")
    # A voice session may only be bound by the normal task creation transaction.
    # The transcript itself stays in the regular user message, never in voice tables.
    voice_session_id: str | None = Field(default=None, pattern=r"^[a-f0-9-]{16,80}$")
    approved_actions: list[str] = Field(default_factory=list)
    approval_scope: ApprovalScope = "once"
    resume: bool = False
    checkpoint_sequence: int | None = Field(default=None, ge=1)
    allow_workspace_drift: bool = False
    retry_uncertain: bool = False
    orchestration_mode: OrchestrationMode = "single"
    agent_count: int = Field(default=2, ge=1, le=8)
    interaction_mode: InteractionMode = "agent"
    data_location: DataLocation = "local_workspace"
    privacy_scope: PrivacyScope = "workspace"
    # ``budget_limit`` remains the v14-compatible alias.  New clients should
    # send the explicit token contract fields below.
    budget_limit: int | None = Field(default=None, ge=1, le=10_000_000)
    token_budget_limit: int | None = Field(default=None, ge=1, le=10_000_000)
    token_budget_mode: Literal["soft", "hard"] | None = None
    cost_budget_limit: float | None = Field(default=None, gt=0, le=1_000_000)
    task_deadline_at: datetime | None = None
    segment_timeout_seconds: float | None = Field(default=None, ge=0.01, le=86_400)
    preferred_model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: ReasoningEffort = "auto"
    memory_write_policy: MemoryWritePolicy = "explicit"

    @model_validator(mode="after")
    def validate_budget_contract(self) -> "ChatRequest":
        if (
            self.budget_limit is not None
            and self.token_budget_limit is not None
            and self.budget_limit != self.token_budget_limit
        ):
            raise ValueError("budget_limit 与 token_budget_limit 不能冲突")
        if self.task_deadline_at is not None and (
            self.task_deadline_at.tzinfo is None or self.task_deadline_at.utcoffset() is None
        ):
            raise ValueError("task_deadline_at 必须包含时区")
        return self


class TaskResumeRequest(RequestModel):
    approved_actions: list[str] = Field(default_factory=list)
    approval_scope: ApprovalScope = "once"
    checkpoint_sequence: int | None = Field(default=None, ge=1)
    allow_workspace_drift: bool = False
    retry_uncertain: bool = False


class SteeringRequest(RequestModel):
    content: str = Field(min_length=1, max_length=100_000)
    priority: QueuePriority = "now"
    target_scope: Literal["task", "agent"] = "task"
    target_agent_id: str | None = Field(default=None, max_length=100)


class QueuePromoteRequest(RequestModel):
    priority: QueuePriority = "now"


class PermissionUpdate(RequestModel):
    permission_mode: PermissionMode


class AgentProfileUpdate(RequestModel):
    agent_profile_id: str = Field(pattern=r"^[a-z][a-z0-9._-]{1,63}$")


class IdentityVersionCreate(RequestModel):
    identity: dict[str, Any]
    reason: str = Field(min_length=3, max_length=500)
    actor_id: Literal["administrator-001"] = "administrator-001"
    administrator_confirmed: bool = False


class IdentityVersionActivate(RequestModel):
    version: int = Field(ge=1)
    actor_id: Literal["administrator-001"] = "administrator-001"
    administrator_confirmed: bool = False


class ToolRequest(RequestModel):
    conversation_id: int
    workspace: str
    permission_mode: PermissionMode = "ask"
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None
    approval_tokens: list[str] = Field(default_factory=list)
    approval_scope: ApprovalScope = "once"


class VisionRequest(RequestModel):
    conversation_id: int
    workspace: str
    permission_mode: PermissionMode = "ask"
    action: VisionAction
    paths: list[str] = Field(min_length=1, max_length=4)
    prompt: str = Field(default="", max_length=4000)
    provider_mode: VisionProviderMode = "active"
    task_id: str | None = None
    approval_tokens: list[str] = Field(default_factory=list)
    approval_scope: ApprovalScope = "once"


class McpServerCreate(RequestModel):
    name: str
    transport: Literal["http", "sse", "stdio"] = "http"
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)


class McpCall(RequestModel):
    server_id: int
    conversation_id: int
    task_id: str | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
    approval_tokens: list[str] = Field(default_factory=list)
    approval_scope: ApprovalScope = "once"


class EnabledUpdate(RequestModel):
    enabled: bool


class ExtensionInstallRequest(RequestModel):
    workspace: str
    source_path: str = Field(min_length=1, max_length=500)
    enable: bool = True


class CompactRequest(RequestModel):
    force: bool = False


class ConversationRename(RequestModel):
    title: str = Field(min_length=1, max_length=100)


class MemoryCreate(RequestModel):
    key: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=4000)
    kind: Literal["project", "experience"] = "project"
    namespace: Literal["project", "personal"] = "project"
    category: Literal["architecture", "build_command", "test_command", "coding_convention", "decision", "known_issue", "successful_fix", "failed_approach", "user_constraint"] | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)
    applicable_version: str | None = Field(default=None, max_length=100)
    confidence: float = Field(default=0.8, ge=0, le=1)


class MemoryUpdate(RequestModel):
    key: str | None = Field(default=None, min_length=1, max_length=80)
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    kind: Literal["project", "experience"] | None = None
    namespace: Literal["project", "personal"] | None = None
    category: Literal["architecture", "build_command", "test_command", "coding_convention", "decision", "known_issue", "successful_fix", "failed_approach", "user_constraint"] | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    applicable_version: str | None = Field(default=None, max_length=100)
    confidence: float | None = Field(default=None, ge=0, le=1)


class MemoryFeedback(RequestModel):
    outcome: Literal["success", "failure", "verify", "reject"]


class LongTermMemoryCreate(RequestModel):
    memory_type: Literal["semantic", "episodic", "procedural", "relationship"]
    title: str | None = Field(default=None, max_length=200)
    content: str = Field(min_length=1, max_length=8000)
    source_type: Literal["conversation", "user_confirmed", "system_observation", "imported_document", "manual_entry", "agent_inference"] = "manual_entry"
    confidence: float = Field(default=0.8, ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    emotional_weight: float = Field(default=0, ge=-1, le=1)
    occurred_at: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    user_confirmed: bool = False
    is_locked: bool = False
    is_sensitive: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    admin_grant_token: str = Field(min_length=20, max_length=200)
    ui_session_id: str = Field(min_length=8, max_length=200)


class LongTermMemoryUpdate(RequestModel):
    title: str | None = Field(default=None, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=8000)
    memory_type: Literal["semantic", "episodic", "procedural", "relationship"] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    importance: float | None = Field(default=None, ge=0, le=1)
    emotional_weight: float | None = Field(default=None, ge=-1, le=1)
    occurred_at: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    status: Literal["candidate", "active", "superseded", "expired", "rejected", "archived"] | None = None
    user_confirmed: bool | None = None
    is_locked: bool | None = None
    is_sensitive: bool | None = None
    metadata: dict[str, Any] | None = None
    admin_grant_token: str = Field(min_length=20, max_length=200)
    ui_session_id: str = Field(min_length=8, max_length=200)


class LongTermMemorySearchRequest(RequestModel):
    query: str = Field(min_length=1, max_length=500)
    memory_types: list[Literal["semantic", "episodic", "procedural", "relationship"]] = Field(default_factory=list, max_length=4)
    statuses: list[Literal["candidate", "active", "superseded", "expired", "rejected", "deleted", "archived"]] = Field(default_factory=lambda: ["active"], max_length=7)
    source_types: list[Literal["conversation", "user_confirmed", "system_observation", "imported_document", "manual_entry", "agent_inference"]] = Field(default_factory=list, max_length=6)
    user_confirmed: bool | None = None
    is_locked: bool | None = None
    valid_from: str | None = Field(default=None, max_length=50)
    valid_to: str | None = Field(default=None, max_length=50)
    min_importance: float | None = Field(default=None, ge=0, le=1)
    min_confidence: float | None = Field(default=None, ge=0, le=1)
    sensitive_mode: Literal["exclude", "redacted", "full"] = "exclude"
    sort: Literal["relevance", "updated", "importance"] = "relevance"
    offset: int = Field(default=0, ge=0)
    cursor: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=20, ge=1, le=100)
    admin_grant_token: str | None = Field(default=None, min_length=20, max_length=200)
    ui_session_id: str | None = Field(default=None, min_length=8, max_length=200)


class MemoryCandidateCreate(RequestModel):
    memory_type: Literal["semantic", "episodic", "procedural", "relationship"]
    content: str = Field(min_length=1, max_length=8000)
    reason: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(default=0.5, ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    source_conversation_id: str | None = None
    is_sensitive: bool | None = None


class MemoryCandidateDecision(RequestModel):
    accept: bool
    admin_grant_token: str | None = Field(default=None, min_length=20, max_length=200)
    ui_session_id: str | None = Field(default=None, min_length=8, max_length=200)


class AdminActionGrantCreate(RequestModel):
    operation: Literal["memory.create", "memory.update", "memory.delete", "memory.search_sensitive", "memory_candidate.accept"]
    target_id: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
    ui_session_id: str = Field(min_length=8, max_length=200)


class CommandValidationRequest(RequestModel):
    text: str = Field(min_length=1, max_length=1000)
    has_conversation: bool = False
    has_workspace: bool = False
    running: bool = False
    waiting_confirmation: bool = False
    recovering: bool = False
    confirmed: bool = False


class CommandAuditRequest(RequestModel):
    command: Literal["/help", "/clear", "/compact", "/context", "/cost", "/doctor", "/memory", "/search", "/stop"]
    status: Literal["ok", "error"]
    conversation_id: int | None = None


class AffectEventCreate(RequestModel):
    event_type: str = Field(min_length=1, max_length=50)
    intensity: float = Field(ge=0, le=1)
    valence_delta: float = Field(default=0, ge=-0.05, le=0.05)
    trigger_memory_id: str | None = None
    decay_rate: float = Field(default=0.12, ge=0, le=1)
    conversation_id: int | None = None


class RelationshipEventCreate(RequestModel):
    delta: dict[str, float]
    importance: Literal["normal", "important", "manual"] = "normal"
    reason: str = Field(min_length=1, max_length=1000)
    conversation_id: int | None = None
