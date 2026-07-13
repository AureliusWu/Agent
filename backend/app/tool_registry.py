from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


Risk = Literal["low", "medium", "high", "critical"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    risk: Risk
    properties: dict[str, dict[str, Any]]
    required: tuple[str, ...] = ()

    def openai(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": {"type": "object", "properties": self.properties, "required": list(self.required), "additionalProperties": False}}}


SPECS = [
    ToolSpec("list_files", "列出工作区目录", "low", {"path": {"type": "string", "default": "."}}),
    ToolSpec("search_files", "搜索工作区文件名与文本内容", "low", {"query": {"type": "string"}, "path": {"type": "string", "default": "."}, "glob": {"type": "string", "default": "*"}}, ("query",)),
    ToolSpec("read_file", "分段读取工作区文本文件", "low", {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}}, ("path",)),
    ToolSpec("file_metadata", "查看文件元数据与编码", "low", {"path": {"type": "string"}}, ("path",)),
    ToolSpec("file_diff", "预览写入内容与现有文件的差异", "low", {"path": {"type": "string"}, "content": {"type": "string"}}, ("path", "content")),
    ToolSpec("write_file", "原子写入文件并生成可撤销备份", "medium", {"path": {"type": "string"}, "content": {"type": "string"}}, ("path", "content")),
    ToolSpec("copy_file", "复制工作区内文件", "medium", {"source": {"type": "string"}, "destination": {"type": "string"}}, ("source", "destination")),
    ToolSpec("move_file", "移动或重命名工作区内文件", "medium", {"source": {"type": "string"}, "destination": {"type": "string"}}, ("source", "destination")),
    ToolSpec("create_directory", "创建工作区目录", "medium", {"path": {"type": "string"}}, ("path",)),
    ToolSpec("delete_file", "删除单个文件并生成可撤销备份", "high", {"path": {"type": "string"}}, ("path",)),
    ToolSpec("undo_file_change", "撤销最近一次文件写入、移动、复制或删除", "high", {}, ()),
    ToolSpec("run_command", "在工作区运行具体程序，不使用 shell", "critical", {"command": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}}, "cwd": {"type": "string", "default": "."}, "timeout": {"type": "integer", "minimum": 1, "maximum": 120}}, ("command",)),
]
REGISTRY = {spec.name: spec for spec in SPECS}
BASE_TOOLS = [spec.openai() for spec in SPECS]


class ToolValidationError(ValueError):
    pass


def validate_arguments(name: str, arguments: dict[str, Any]) -> ToolSpec:
    spec = REGISTRY.get(name)
    if not spec:
        raise ToolValidationError(f"未知工具：{name}")
    unknown = set(arguments) - set(spec.properties)
    if unknown:
        raise ToolValidationError(f"不允许的参数：{', '.join(sorted(unknown))}")
    missing = [field for field in spec.required if field not in arguments]
    if missing:
        raise ToolValidationError(f"缺少必填参数：{', '.join(missing)}")
    for field, value in arguments.items():
        schema = spec.properties[field]
        expected = schema.get("type")
        valid = (expected == "string" and isinstance(value, str)) or (expected == "integer" and isinstance(value, int) and not isinstance(value, bool)) or (expected == "array" and isinstance(value, list))
        if expected and not valid:
            raise ToolValidationError(f"参数 {field} 类型应为 {expected}")
        if isinstance(value, int) and (value < schema.get("minimum", value) or value > schema.get("maximum", value)):
            raise ToolValidationError(f"参数 {field} 超出允许范围")
    return spec


def requires_confirmation(mode: str, risk: Risk) -> bool:
    if mode == "ask":
        return risk != "low"
    if mode == "agent":
        return risk in {"high", "critical"}
    return risk == "critical"
