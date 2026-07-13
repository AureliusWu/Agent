from typing import Any, Literal

from pydantic import BaseModel, Field


PermissionMode = Literal["ask", "agent", "full"]


class ConversationCreate(BaseModel):
    title: str = "新对话"
    workspace: str
    permission_mode: PermissionMode = "ask"


class ChatRequest(BaseModel):
    conversation_id: int
    content: str = Field(min_length=1)
    task_id: str | None = Field(default=None, pattern=r"^[a-f0-9-]{16,64}$")
    approved_actions: list[str] = []


class PermissionUpdate(BaseModel):
    permission_mode: PermissionMode


class ToolRequest(BaseModel):
    conversation_id: int | None = None
    workspace: str
    permission_mode: PermissionMode = "ask"
    tool: str
    arguments: dict[str, Any] = {}
    approved: bool = False


class McpServerCreate(BaseModel):
    name: str
    transport: Literal["http", "sse", "stdio"] = "http"
    url: str | None = None
    command: str | None = None
    args: list[str] = []


class McpCall(BaseModel):
    server_id: int
    method: str
    params: dict[str, Any] = {}


class EnabledUpdate(BaseModel):
    enabled: bool


class CompactRequest(BaseModel):
    force: bool = False


class ConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=100)
