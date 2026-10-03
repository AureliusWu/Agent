from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, TYPE_CHECKING

from app.tools.outcomes import RuntimeToolOutcome
from app.tools.spec import ToolSpec

if TYPE_CHECKING:
    from app.permissions import PermissionDecision


PluginCategory = Literal["hear", "speak", "read", "write", "execute", "memory"]


@dataclass(frozen=True)
class PluginCall:
    workspace: str
    mode: str
    name: str
    arguments: dict[str, Any]
    tool_call_id: str
    approved_actions: list[str]
    approval_scope: str
    conversation_id: int
    task_id: str
    permission_fn: Callable[..., PermissionDecision]
    memory_write_policy: str = "explicit"
    memory_write_explicit: bool = False
    search_credentials: dict[str, str] = field(default_factory=dict)
    available_tool_names: tuple[str, ...] | None = None


PluginHandler = Callable[[PluginCall], Awaitable[RuntimeToolOutcome]]


@dataclass(frozen=True)
class PluginDefinition:
    id: str
    name: str
    description: str
    category: PluginCategory
    tools: tuple[ToolSpec, ...]
    handler: PluginHandler
    version: str = "1.0.0"
    requires_workspace: bool = True
    configured: Callable[[], bool] | None = None
    configuration_hint: str = ""
    tool_configuration: Callable[[str], bool] | None = None
