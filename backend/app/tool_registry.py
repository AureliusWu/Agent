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
    display_name: str = ""
    source: str = "builtin"
    output_schema: dict[str, Any] | None = None
    timeout_seconds: int = 30

    def openai(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": {"type": "object", "properties": self.properties, "required": list(self.required), "additionalProperties": False}}}

    def catalog(self) -> dict[str, Any]:
        return {"name": self.name, "display_name": self.display_name or self.name, "description": self.description, "source": self.source, "risk_level": self.risk, "input_schema": self.openai()["function"]["parameters"], "output_schema": self.output_schema or {"type": "object"}, "timeout": self.timeout_seconds}


SPECS = [
    ToolSpec("list_files", "列出工作区目录", "low", {"path": {"type": "string", "default": "."}}),
    ToolSpec("list_directory", "列出工作区目录", "low", {"path": {"type": "string", "default": "."}}),
    ToolSpec("search_files", "搜索工作区文件名与文本内容", "low", {"query": {"type": "string", "maxLength": 1000}, "path": {"type": "string", "default": "."}, "glob": {"type": "string", "default": "*"}, "regex": {"type": "boolean", "default": False}, "context_lines": {"type": "integer", "minimum": 0, "maximum": 10}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500}}, ("query",)),
    ToolSpec("search_text", "搜索工作区文本内容", "low", {"query": {"type": "string", "maxLength": 1000}, "path": {"type": "string", "default": "."}, "glob": {"type": "string", "default": "*"}, "regex": {"type": "boolean", "default": False}, "context_lines": {"type": "integer", "minimum": 0, "maximum": 10}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500}}, ("query",)),
    ToolSpec("read_file", "分段读取工作区文本文件", "low", {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}, "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000}, "encoding": {"type": "string", "enum": ["auto", "utf-8", "utf-8-sig", "utf-16", "gb18030"]}}, ("path",)),
    ToolSpec("read_file_range", "按行范围读取工作区文本文件", "low", {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}, "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000}, "encoding": {"type": "string", "enum": ["auto", "utf-8", "utf-8-sig", "utf-16", "gb18030"]}}, ("path", "start_line", "end_line")),
    ToolSpec("file_metadata", "查看文件元数据与编码", "low", {"path": {"type": "string"}}, ("path",)),
    ToolSpec("file_info", "查看文件元数据与编码", "low", {"path": {"type": "string"}}, ("path",)),
    ToolSpec("file_diff", "预览写入内容与现有文件的差异", "low", {"path": {"type": "string"}, "content": {"type": "string"}}, ("path", "content")),
    ToolSpec("view_diff", "预览写入内容与现有文件的差异", "low", {"path": {"type": "string"}, "content": {"type": "string"}}, ("path", "content")),
    ToolSpec("compare_files", "比较工作区内两个文本文件", "low", {"left": {"type": "string"}, "right": {"type": "string"}}, ("left", "right")),
    ToolSpec("list_file_changes", "查看可撤销的文件变更", "low", {"task_id": {"type": "string"}}),
    ToolSpec("create_file", "仅在目标不存在时原子创建文件", "medium", {"path": {"type": "string"}, "content": {"type": "string"}, "encoding": {"type": "string", "enum": ["utf-8", "utf-8-sig", "utf-16", "gb18030"]}}, ("path", "content")),
    ToolSpec("write_file", "原子写入文件并生成可撤销备份", "medium", {"path": {"type": "string"}, "content": {"type": "string"}, "encoding": {"type": "string", "enum": ["auto", "utf-8", "utf-8-sig", "utf-16", "gb18030"]}}, ("path", "content")),
    ToolSpec("replace_text", "精确替换文件文本并生成 diff", "medium", {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "expected_count": {"type": "integer", "minimum": 1, "maximum": 1000}}, ("path", "old_text", "new_text")),
    ToolSpec("apply_patch", "应用单文件 unified diff", "medium", {"path": {"type": "string"}, "patch": {"type": "string"}}, ("path", "patch")),
    ToolSpec("copy_file", "复制工作区内文件", "medium", {"source": {"type": "string"}, "destination": {"type": "string"}}, ("source", "destination")),
    ToolSpec("move_file", "移动或重命名工作区内文件", "medium", {"source": {"type": "string"}, "destination": {"type": "string"}}, ("source", "destination")),
    ToolSpec("rename_file", "重命名工作区内文件", "medium", {"source": {"type": "string"}, "destination": {"type": "string"}}, ("source", "destination")),
    ToolSpec("create_directory", "创建工作区目录", "medium", {"path": {"type": "string"}}, ("path",)),
    ToolSpec("delete_file", "删除单个文件并生成可撤销备份", "high", {"path": {"type": "string"}}, ("path",)),
    ToolSpec("undo_file_change", "撤销指定或最近一次文件变更", "high", {"change_id": {"type": "string"}}, ()),
    ToolSpec("undo_task_changes", "按相反顺序撤销指定任务的全部文件变更", "high", {"task_id": {"type": "string"}}, ("task_id",)),
    ToolSpec("run_command", "在工作区运行具体程序，不使用 shell", "critical", {"command": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}}, "cwd": {"type": "string", "default": "."}, "timeout": {"type": "integer", "minimum": 1, "maximum": 120}}, ("command",)),
    ToolSpec("list_workspace_memories", "列出当前工作区的长期记忆", "low", {}),
    ToolSpec("remember_workspace", "保存或更新当前工作区的长期记忆", "medium", {"key": {"type": "string", "maxLength": 80}, "content": {"type": "string", "maxLength": 4000}}, ("key", "content")),
    ToolSpec("forget_workspace_memory", "删除当前工作区的一条长期记忆", "high", {"key": {"type": "string", "maxLength": 80}}, ("key",)),
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
        valid = (expected == "string" and isinstance(value, str)) or (expected == "integer" and isinstance(value, int) and not isinstance(value, bool)) or (expected == "array" and isinstance(value, list)) or (expected == "boolean" and isinstance(value, bool))
        if expected and not valid:
            raise ToolValidationError(f"参数 {field} 类型应为 {expected}")
        if isinstance(value, int) and (value < schema.get("minimum", value) or value > schema.get("maximum", value)):
            raise ToolValidationError(f"参数 {field} 超出允许范围")
        if isinstance(value, str) and len(value) > schema.get("maxLength", len(value)):
            raise ToolValidationError(f"参数 {field} 长度超过允许范围")
        if "enum" in schema and value not in schema["enum"]:
            raise ToolValidationError(f"参数 {field} 不在允许范围内")
    return spec


def requires_confirmation(mode: str, risk: Risk) -> bool:
    if mode == "ask":
        return risk != "low"
    if mode == "agent":
        return risk in {"high", "critical"}
    return risk == "critical"
