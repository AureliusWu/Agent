from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


PermissionMode = Literal["ask", "agent", "full"]
ApprovalScope = Literal["once", "task", "session"]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationCreate(RequestModel):
    title: str = "新对话"
    workspace: str
    permission_mode: PermissionMode = "ask"


class ChatRequest(RequestModel):
    conversation_id: int
    content: str = Field(min_length=1)
    task_id: str | None = Field(default=None, pattern=r"^[a-f0-9-]{16,64}$")
    approved_actions: list[str] = Field(default_factory=list)
    approval_scope: ApprovalScope = "once"
    resume: bool = False
    checkpoint_sequence: int | None = Field(default=None, ge=1)
    allow_workspace_drift: bool = False
    retry_uncertain: bool = False


class TaskResumeRequest(RequestModel):
    approved_actions: list[str] = Field(default_factory=list)
    approval_scope: ApprovalScope = "once"
    checkpoint_sequence: int | None = Field(default=None, ge=1)
    allow_workspace_drift: bool = False
    retry_uncertain: bool = False


class PermissionUpdate(RequestModel):
    permission_mode: PermissionMode


class ToolRequest(RequestModel):
    conversation_id: int | None = None
    workspace: str
    permission_mode: PermissionMode = "ask"
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
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


class CompactRequest(RequestModel):
    force: bool = False


class ConversationRename(RequestModel):
    title: str = Field(min_length=1, max_length=100)


class MemoryCreate(RequestModel):
    key: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=4000)
    kind: Literal["project", "experience"] = "project"
    tags: list[str] = Field(default_factory=list, max_length=20)
    applicable_version: str | None = Field(default=None, max_length=100)
    confidence: float = Field(default=0.8, ge=0, le=1)


class MemoryUpdate(RequestModel):
    key: str | None = Field(default=None, min_length=1, max_length=80)
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    kind: Literal["project", "experience"] | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    applicable_version: str | None = Field(default=None, max_length=100)
    confidence: float | None = Field(default=None, ge=0, le=1)


class MemoryFeedback(RequestModel):
    outcome: Literal["success", "failure", "verify", "reject"]
