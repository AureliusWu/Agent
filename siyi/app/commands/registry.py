from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class CommandDefinition:
    name: str
    title: str
    description: str
    usage: str
    category: Literal["conversation", "context", "runtime", "memory", "diagnostics"]
    execution: Literal["frontend", "backend", "runtime-control"]
    risk: Literal["none", "confirm"] = "none"
    requires_argument: bool = False
    requires_conversation: bool = False
    requires_workspace: bool = False
    allowed_while_busy: bool = False
    accepts_arguments: bool = False


COMMANDS = (
    CommandDefinition("/help", "帮助", "显示全部本地指令", "/help", "diagnostics", "frontend", allowed_while_busy=True),
    CommandDefinition("/clear", "清空对话", "清空当前对话消息，需要确认", "/clear", "conversation", "backend", risk="confirm", requires_conversation=True),
    CommandDefinition("/compact", "压缩上下文", "立即压缩当前对话上下文", "/compact", "context", "backend", requires_conversation=True),
    CommandDefinition("/context", "上下文", "显示当前上下文统计", "/context", "context", "backend", requires_conversation=True),
    CommandDefinition("/cost", "用量与成本", "显示累计 Token 和费用", "/cost", "diagnostics", "backend", allowed_while_busy=True),
    CommandDefinition("/doctor", "诊断", "显示本地核心诊断状态", "/doctor", "diagnostics", "backend", allowed_while_busy=True),
    CommandDefinition("/memory", "搜索长期记忆", "仅在本地搜索个人长期记忆", "/memory <关键词>", "memory", "backend", requires_argument=True, allowed_while_busy=True, accepts_arguments=True),
    CommandDefinition("/search", "打开搜索", "打开全局搜索并保留关键词", "/search <关键词>", "memory", "frontend", requires_argument=True, allowed_while_busy=True, accepts_arguments=True),
    CommandDefinition("/stop", "停止任务", "停止当前真实运行任务", "/stop", "runtime", "runtime-control", allowed_while_busy=True),
)
COMMAND_BY_NAME = {item.name: item for item in COMMANDS}


def command_catalog() -> list[dict[str, Any]]:
    return [asdict(item) for item in COMMANDS]


def validate_command(
    text: str, *, has_conversation: bool, has_workspace: bool, running: bool,
    waiting_confirmation: bool = False, recovering: bool = False, confirmed: bool = False,
) -> dict[str, Any]:
    normalized = text.strip()
    name, _, argument = normalized.partition(" ")
    name = name.casefold()
    definition = COMMAND_BY_NAME.get(name)
    if definition is None:
        return {"status": "error", "code": "unknown_command", "message": "未知本地指令", "command": name, "argument": ""}
    argument = argument.strip()
    if definition.requires_argument and not argument:
        return {"status": "error", "code": "argument_required", "message": f"{name} 需要参数", "command": name, "argument": ""}
    if definition.requires_conversation and not has_conversation:
        return {"status": "disabled", "code": "conversation_required", "message": "请先创建或选择对话", "command": name, "argument": argument}
    if definition.requires_workspace and not has_workspace:
        return {"status": "disabled", "code": "workspace_required", "message": "请先打开工作区", "command": name, "argument": argument}
    if waiting_confirmation and name in {"/clear", "/compact"}:
        return {"status": "disabled", "code": "confirmation_pending", "message": "当前有待确认操作，不能修改对话上下文", "command": name, "argument": argument}
    if recovering and name in {"/clear", "/compact"}:
        return {"status": "disabled", "code": "recovery_active", "message": "恢复状态下不能修改对话上下文", "command": name, "argument": argument}
    if running and not definition.allowed_while_busy:
        return {"status": "disabled", "code": "not_allowed_while_running", "message": "任务运行中不能执行此指令", "command": name, "argument": argument}
    if name == "/stop" and not running:
        return {"status": "disabled", "code": "task_not_running", "message": "当前没有正在运行的任务", "command": name, "argument": argument}
    if definition.risk == "confirm" and not confirmed:
        return {"status": "confirmation_required", "code": "confirmation_required", "message": "此操作需要确认", "command": name, "argument": argument}
    return {"status": "ready", "code": "ready", "message": "指令已验证", "command": name, "argument": argument}
