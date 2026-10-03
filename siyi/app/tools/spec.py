from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Risk = Literal["low", "medium", "high", "critical"]
Interruptibility = Literal["cancel", "block"]
ConcurrencyPolicy = Literal["parallel_safe", "serial", "exclusive"]

BLOCKING_TOOLS = {
    "create_file",
    "write_file",
    "replace_text",
    "apply_patch",
    "copy_file",
    "move_file",
    "rename_file",
    "create_directory",
    "delete_file",
    "undo_file_change",
    "undo_task_changes",
    "restore_security_snapshot",
    "remember_workspace",
    "forget_workspace_memory",
    "create_worktree",
    "remove_worktree",
}
EXCLUSIVE_TOOLS = {
    "run_command",
    "restore_security_snapshot",
    "undo_task_changes",
    "create_worktree",
    "remove_worktree",
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    risk: Risk
    properties: dict[str, dict[str, Any]]
    required: tuple[str, ...] = ()
    display_name: str = ""
    source: str = "builtin"
    output_schema: dict[str, Any] | None = None
    timeout_seconds: int = 30
    interruptibility: Interruptibility | None = None
    concurrency_policy: ConcurrencyPolicy | None = None
    max_result_chars: int = 40_000
    plugin_id: str = ""
    selection_order: int = 1000

    def __post_init__(self) -> None:
        if self.interruptibility is None:
            object.__setattr__(
                self, "interruptibility", "block" if self.name in BLOCKING_TOOLS else "cancel"
            )
        if self.concurrency_policy is None:
            policy: ConcurrencyPolicy = (
                "exclusive"
                if self.name in EXCLUSIVE_TOOLS
                else ("serial" if self.name in BLOCKING_TOOLS else "parallel_safe")
            )
            object.__setattr__(self, "concurrency_policy", policy)

    def openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": list(self.required),
                    "additionalProperties": False,
                },
            },
        }

    def catalog(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "description": self.description,
            "source": self.source,
            "plugin_id": self.plugin_id,
            "risk_level": self.risk,
            "input_schema": self.openai()["function"]["parameters"],
            "output_schema": self.output_schema or {"type": "object"},
            "timeout": self.timeout_seconds,
            "interruptibility": self.interruptibility,
            "concurrency_policy": self.concurrency_policy,
            "max_result_chars": self.max_result_chars,
        }
