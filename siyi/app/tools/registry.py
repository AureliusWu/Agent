from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Literal


Risk = Literal["low", "medium", "high", "critical"]
Interruptibility = Literal["cancel", "block"]
ConcurrencyPolicy = Literal["parallel_safe", "serial", "exclusive"]

BLOCKING_TOOLS = {
    "create_file", "write_file", "replace_text", "apply_patch", "copy_file", "move_file", "rename_file",
    "create_directory", "delete_file", "delete_directory", "undo_file_change", "undo_task_changes", "restore_security_snapshot",
    "remember_workspace", "forget_workspace_memory",
    "create_worktree", "remove_worktree",
    "artifact.markdown.create", "artifact.docx.create", "artifact.docx.edit",
    "artifact.pdf.create", "artifact.pdf.merge", "artifact.pptx.create",
    "artifact.pptx.edit", "artifact.render",
    "file_batch",
}
EXCLUSIVE_TOOLS = {"run_command", "restore_security_snapshot", "undo_task_changes", "file_batch", "create_worktree", "remove_worktree"}
ROLLBACK_TOOLS = {
    "create_file", "write_file", "replace_text", "apply_patch", "copy_file", "move_file", "rename_file",
    "create_directory", "delete_file", "delete_directory", "restore_security_snapshot",
    "artifact.markdown.create", "artifact.docx.create", "artifact.docx.edit", "artifact.pdf.create",
    "artifact.pdf.merge", "artifact.pptx.create", "artifact.pptx.edit", "artifact.render",
    "file_batch",
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
    version: str = "1.0"
    idempotent: bool | None = None
    rollback_support: bool | None = None
    verification_support: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.interruptibility is None:
            object.__setattr__(self, "interruptibility", "block" if self.name in BLOCKING_TOOLS else "cancel")
        if self.concurrency_policy is None:
            policy: ConcurrencyPolicy = "exclusive" if self.name in EXCLUSIVE_TOOLS else ("serial" if self.name in BLOCKING_TOOLS else "parallel_safe")
            object.__setattr__(self, "concurrency_policy", policy)
        if self.idempotent is None:
            object.__setattr__(self, "idempotent", self.name not in BLOCKING_TOOLS)
        if self.rollback_support is None:
            object.__setattr__(self, "rollback_support", self.name in ROLLBACK_TOOLS)
        if self.verification_support is None:
            support: tuple[str, ...] = ()
            if self.name in ROLLBACK_TOOLS:
                support = ("exists", "hash", "diff")
            elif self.name == "run_command":
                support = ("process_exit", "test_command")
            elif self.name in {"read_file", "read_file_range", "file_metadata", "file_info"}:
                support = ("exists", "hash")
            object.__setattr__(self, "verification_support", support)

    def openai(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": {"type": "object", "properties": self.properties, "required": list(self.required), "additionalProperties": False}}}

    def catalog(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "display_name": self.display_name or self.name,
            "description": self.description,
            "source": self.source,
            "risk_level": self.risk,
            "input_schema": self.openai()["function"]["parameters"],
            "output_schema": self.output_schema or {"type": "object"},
            "timeout": self.timeout_seconds,
            "cancellable": self.interruptibility == "cancel",
            "idempotent": self.idempotent,
            "permission_level": (
                "L2_PROCESS_EXECUTION"
                if self.name == "run_command"
                else {
                    "low": "L0_READ_ONLY",
                    "medium": "L1_WORKSPACE_WRITE",
                    "high": "L3_WORKSPACE_DELETE",
                    "critical": "L5_IRREVERSIBLE",
                }[self.risk]
            ),
            "side_effects": ["workspace"] if self.name in BLOCKING_TOOLS else [],
            "rollback_support": self.rollback_support,
            "verification_support": list(self.verification_support or ()),
            "interruptibility": self.interruptibility,
            "concurrency_policy": self.concurrency_policy,
            "max_result_chars": self.max_result_chars,
        }


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
    ToolSpec("get_repo_map", "获取工作区代码地图、语言构成、索引统计和 Git 变更", "low", {}),
    ToolSpec("find_symbol", "按名称查找 Python、TypeScript、JavaScript 或 Rust 符号", "low", {"query": {"type": "string", "maxLength": 200}, "exact": {"type": "boolean", "default": False}, "kind": {"type": "string", "maxLength": 40}, "max_results": {"type": "integer", "minimum": 1, "maximum": 200}}, ("query",)),
    ToolSpec("find_definition", "查找符号定义位置", "low", {"symbol": {"type": "string", "maxLength": 200}, "max_results": {"type": "integer", "minimum": 1, "maximum": 100}}, ("symbol",)),
    ToolSpec("find_references", "查找符号调用和读取位置", "low", {"symbol": {"type": "string", "maxLength": 200}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500}}, ("symbol",)),
    ToolSpec("list_module_dependencies", "查看工作区模块导入依赖", "low", {"path": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500}}),
    ToolSpec("find_related_tests", "根据源文件或符号查找相关测试", "low", {"path": {"type": "string"}, "symbol": {"type": "string", "maxLength": 200}, "max_results": {"type": "integer", "minimum": 1, "maximum": 200}}),
    ToolSpec("get_call_chain", "向上追踪符号调用链", "low", {"symbol": {"type": "string", "maxLength": 200}, "depth": {"type": "integer", "minimum": 1, "maximum": 8}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500}}, ("symbol",)),
    ToolSpec("inspect_diagnostics", "查看代码索引发现的解析诊断", "low", {"path": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 500}}),
    ToolSpec("lsp_query", "通过语言服务器查询定义、引用或文档符号；无服务器时安全降级到工作区索引", "low", {"path": {"type": "string"}, "operation": {"type": "string", "enum": ["definition", "references", "symbols"]}, "line": {"type": "integer", "minimum": 0}, "character": {"type": "integer", "minimum": 0}, "symbol": {"type": "string", "maxLength": 200}}, ("path", "operation")),
    ToolSpec("list_worktrees", "列出当前 Git 仓库的受控工作树", "low", {}),
    ToolSpec("create_worktree", "在工作区 .agent/worktrees 中创建隔离的 Git 工作树", "high", {"name": {"type": "string", "maxLength": 80}, "ref": {"type": "string", "maxLength": 200}, "branch": {"type": "string", "maxLength": 200}}, ("name",)),
    ToolSpec("remove_worktree", "移除由司忆管理的隔离 Git 工作树", "critical", {"name": {"type": "string", "maxLength": 80}, "force": {"type": "boolean", "default": False}}, ("name",)),
    ToolSpec("list_file_changes", "查看可撤销的文件变更", "low", {"task_id": {"type": "string"}}),
    ToolSpec("list_security_snapshots", "列出当前工作区的高风险操作安全快照", "low", {"task_id": {"type": "string"}}),
    ToolSpec("preview_security_snapshot", "预览恢复安全快照会改变的文件", "low", {"snapshot_id": {"type": "string", "maxLength": 32}}, ("snapshot_id",)),
    ToolSpec("create_file", "仅在目标不存在时原子创建文件", "medium", {"path": {"type": "string"}, "content": {"type": "string"}, "encoding": {"type": "string", "enum": ["utf-8", "utf-8-sig", "utf-16", "gb18030"]}}, ("path", "content")),
    ToolSpec("write_file", "原子写入文件并生成可撤销备份", "medium", {"path": {"type": "string"}, "content": {"type": "string"}, "encoding": {"type": "string", "enum": ["auto", "utf-8", "utf-8-sig", "utf-16", "gb18030"]}}, ("path", "content")),
    ToolSpec("replace_text", "精确替换文件文本并生成 diff", "medium", {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "expected_count": {"type": "integer", "minimum": 1, "maximum": 1000}}, ("path", "old_text", "new_text")),
    ToolSpec("apply_patch", "应用单文件 unified diff", "medium", {"path": {"type": "string"}, "patch": {"type": "string"}}, ("path", "patch")),
    ToolSpec("copy_file", "复制工作区内文件", "medium", {"source": {"type": "string"}, "destination": {"type": "string"}}, ("source", "destination")),
    ToolSpec("move_file", "移动或重命名工作区内文件", "medium", {"source": {"type": "string"}, "destination": {"type": "string"}}, ("source", "destination")),
    ToolSpec("rename_file", "重命名工作区内文件", "medium", {"source": {"type": "string"}, "destination": {"type": "string"}}, ("source", "destination")),
    ToolSpec("create_directory", "创建工作区目录", "medium", {"path": {"type": "string"}}, ("path",)),
    ToolSpec("delete_file", "删除单个文件并生成可撤销备份", "high", {"path": {"type": "string"}}, ("path",)),
    ToolSpec("delete_directory", "递归删除受限目录并生成可撤销备份", "high", {"path": {"type": "string"}, "max_entries": {"type": "integer", "minimum": 1, "maximum": 5000}}, ("path",)),
    ToolSpec("undo_file_change", "撤销指定或最近一次文件变更", "high", {"change_id": {"type": "string"}}, ()),
    ToolSpec("undo_task_changes", "按相反顺序撤销指定任务的全部文件变更", "high", {"task_id": {"type": "string"}}, ("task_id",)),
    ToolSpec(
        "file_batch",
        "预检并执行最多50项文件操作；一次确认覆盖本批，失败逆序回滚。后序操作可用expected_version_token='batch:0'引用同一路径最近的第0项变更（索引从0起）；目录移动/删除、恢复请用独立工具。",
        "high",
        {
            "operations": {"type": "array", "items": {"type": "object"}, "minItems": 1, "maxItems": 50},
            "dry_run": {"type": "boolean", "default": False},
        },
        ("operations",),
        timeout_seconds=120,
    ),
    ToolSpec("restore_security_snapshot", "恢复命令执行前的工作区与 Git 状态", "critical", {"snapshot_id": {"type": "string", "maxLength": 32}}, ("snapshot_id",)),
    ToolSpec("run_command", "在工作区运行具体程序，不使用 shell", "critical", {"command": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}}, "cwd": {"type": "string", "default": "."}, "timeout": {"type": "integer", "minimum": 1, "maximum": 120}}, ("command",)),
    ToolSpec("list_workspace_memories", "列出当前工作区的工程记忆", "low", {"category": {"type": "string", "enum": ["architecture", "build_command", "test_command", "coding_convention", "decision", "known_issue", "successful_fix", "failed_approach", "user_constraint"]}}),
    ToolSpec("remember_workspace", "保存或更新当前工作区的分类工程记忆", "medium", {"key": {"type": "string", "maxLength": 80}, "content": {"type": "string", "maxLength": 4000}, "kind": {"type": "string", "enum": ["project", "experience"]}, "category": {"type": "string", "enum": ["architecture", "build_command", "test_command", "coding_convention", "decision", "known_issue", "successful_fix", "failed_approach", "user_constraint"]}, "tags": {"type": "array", "items": {"type": "string"}}, "applicable_version": {"type": "string", "maxLength": 100}}, ("key", "content")),
    ToolSpec("forget_workspace_memory", "删除当前工作区的一条工程记忆", "high", {"key": {"type": "string", "maxLength": 80}}, ("key",)),
    ToolSpec("web_search", "通过已配置的搜索供应商检索最新公开信息，返回可核验的标题、链接和摘要；回答必须引用返回的来源", "low", {"query": {"type": "string", "description": "搜索关键词", "maxLength": 2000}, "provider": {"type": "string", "enum": ["tavily", "brave"]}, "max_results": {"type": "integer", "minimum": 1, "maximum": 20}, "topic": {"type": "string", "enum": ["general", "news", "finance"]}, "time_range": {"type": "string", "enum": ["day", "week", "month", "year"]}}, ("query",), max_result_chars=80_000, timeout_seconds=60),
    ToolSpec("web_fetch", "读取指定公开网页的正文；内容按不可信外部数据处理，并受 SSRF、类型和响应大小限制", "low", {"url": {"type": "string", "maxLength": 4000}, "max_chars": {"type": "integer", "minimum": 1000, "maximum": 100000}}, ("url",), max_result_chars=100_000, timeout_seconds=60),
    ToolSpec("artifact.markdown.create", "在工作区创建 UTF-8 Markdown 产物", "medium", {"path": {"type": "string"}, "content": {"type": "string", "maxLength": 500000}, "title": {"type": "string", "maxLength": 500}, "image_paths": {"type": "array", "items": {"type": "string"}, "maxItems": 20}}, ("path", "content"), timeout_seconds=60),
    ToolSpec("artifact.docx.create", "从 Markdown 内容创建并验证 DOCX 产物", "medium", {"path": {"type": "string"}, "content": {"type": "string", "maxLength": 500000}, "title": {"type": "string", "maxLength": 500}, "image_paths": {"type": "array", "items": {"type": "string"}, "maxItems": 20}}, ("path", "content"), timeout_seconds=120),
    ToolSpec("artifact.docx.edit", "对 DOCX 执行受限的精确文本替换并重新验证", "medium", {"path": {"type": "string"}, "replacements": {"type": "array", "items": {"type": "object"}, "minItems": 1, "maxItems": 100}, "expected_version_token": {"type": "string"}}, ("path", "replacements"), timeout_seconds=120),
    ToolSpec("artifact.pdf.create", "从 Markdown 内容创建并验证可打印 PDF", "medium", {"path": {"type": "string"}, "content": {"type": "string", "maxLength": 500000}, "title": {"type": "string", "maxLength": 500}, "image_paths": {"type": "array", "items": {"type": "string"}, "maxItems": 20}}, ("path", "content"), timeout_seconds=120),
    ToolSpec("artifact.pdf.merge", "按给定顺序合并并验证工作区 PDF", "medium", {"path": {"type": "string"}, "inputs": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 50}}, ("path", "inputs"), timeout_seconds=120),
    ToolSpec("artifact.pdf.extract", "逐页提取 PDF 文本并明确标记无文本页", "low", {"path": {"type": "string"}, "max_pages": {"type": "integer", "minimum": 1, "maximum": 500}, "max_chars": {"type": "integer", "minimum": 1000, "maximum": 200000}}, ("path",), timeout_seconds=120, max_result_chars=200_000),
    ToolSpec("artifact.pptx.create", "从结构化幻灯片创建并验证 PPTX 产物", "medium", {"path": {"type": "string"}, "title": {"type": "string", "maxLength": 500}, "slides": {"type": "array", "items": {"type": "object"}, "minItems": 1, "maxItems": 100}}, ("path", "slides"), timeout_seconds=120),
    ToolSpec("artifact.pptx.edit", "对 PPTX 执行受限的精确文本替换并重新验证", "medium", {"path": {"type": "string"}, "replacements": {"type": "array", "items": {"type": "object"}, "minItems": 1, "maxItems": 100}, "expected_version_token": {"type": "string"}}, ("path", "replacements"), timeout_seconds=120),
    ToolSpec("artifact.render", "把受支持产物真实渲染为工作区 PNG 页面", "medium", {"path": {"type": "string"}, "output_directory": {"type": "string"}, "dpi": {"type": "integer", "minimum": 72, "maximum": 300}}, ("path", "output_directory"), timeout_seconds=180),
    ToolSpec("artifact.validate", "使用格式专用解析器验证产物结构、安全性和可打开性", "low", {"path": {"type": "string"}}, ("path",), timeout_seconds=120),
    ToolSpec("vision.describe", "描述一张工作区图片，区分观察、推断和不确定性", "low", {"path": {"type": "string"}, "prompt": {"type": "string", "maxLength": 4000}}, ("path",), timeout_seconds=120),
    ToolSpec("vision.extract_text", "从一张工作区图片提取可见文字；OCR 只作为明确后备", "low", {"path": {"type": "string"}, "prompt": {"type": "string", "maxLength": 4000}}, ("path",), timeout_seconds=120),
    ToolSpec("vision.analyze_chart", "分析一张工作区图表并区分读数与推断", "low", {"path": {"type": "string"}, "prompt": {"type": "string", "maxLength": 4000}}, ("path",), timeout_seconds=120),
    ToolSpec("vision.compare", "比较两到四张工作区图片并引用稳定 image id", "low", {"paths": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4}, "prompt": {"type": "string", "maxLength": 4000}}, ("paths",), timeout_seconds=120),
    ToolSpec("vision.inspect_ui", "诊断一张 UI 截图，分开可见现象、可能原因和建议", "low", {"path": {"type": "string"}, "prompt": {"type": "string", "maxLength": 4000}}, ("path",), timeout_seconds=120),
    ToolSpec("vision.classify", "依据可见证据对一张工作区图片分类并标注置信度", "low", {"path": {"type": "string"}, "prompt": {"type": "string", "maxLength": 4000}}, ("path",), timeout_seconds=120),
]
_VERSIONED_MUTATIONS = {
    "write_file": ("expected_version_token",),
    "replace_text": ("expected_version_token",),
    "apply_patch": ("expected_version_token",),
    "copy_file": ("expected_version_token", "expected_destination_version_token"),
    "move_file": ("expected_version_token", "expected_destination_version_token"),
    "rename_file": ("expected_version_token", "expected_destination_version_token"),
    "delete_file": ("expected_version_token",),
    "delete_directory": ("expected_version_token",),
    "artifact.docx.edit": ("expected_version_token",),
    "artifact.pptx.edit": ("expected_version_token",),
}
SPECS = [
    replace(
        spec,
        properties={**spec.properties, **{field: {"type": "string"} for field in _VERSIONED_MUTATIONS[spec.name]}},
        required=(*spec.required, *_VERSIONED_MUTATIONS[spec.name]),
    )
    if spec.name in _VERSIONED_MUTATIONS
    else spec
    for spec in SPECS
]
_MUTATION_TOOLS = {
    "create_file",
    "write_file",
    "replace_text",
    "apply_patch",
    "copy_file",
    "move_file",
    "rename_file",
    "create_directory",
    "delete_file",
    "delete_directory",
    "artifact.markdown.create",
    "artifact.docx.create",
    "artifact.docx.edit",
    "artifact.pdf.create",
    "artifact.pdf.merge",
    "artifact.pptx.create",
    "artifact.pptx.edit",
    "artifact.render",
}
SPECS = [
    replace(spec, properties={**spec.properties, "dry_run": {"type": "boolean", "default": False}})
    if spec.name in _MUTATION_TOOLS
    else spec
    for spec in SPECS
]
REGISTRY = {spec.name: spec for spec in SPECS}
BASE_TOOLS = [spec.openai() for spec in SPECS]
BASE_TOOL_INDEX = {item["function"]["name"]: item for item in BASE_TOOLS}


def filter_readonly_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in tools:
        name = str((item.get("function") or {}).get("name") or "")
        spec = REGISTRY.get(name)
        if spec is not None and spec.risk == "low":
            result.append(item)
    return result


def _tool_terms(value: str) -> set[str]:
    lowered = value.lower()
    words = set(re.findall(r"[a-z0-9_.:-]{2,}|[\u4e00-\u9fff]{2,}", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    words.update(chinese[index : index + 2] for index in range(max(0, len(chinese) - 1)))
    return {word for word in words if word}


def select_model_tools(
    prompt: str,
    planned_tools: tuple[str, ...] | list[str],
    mcp_tools: list[dict[str, Any]],
    *,
    max_builtin: int = 16,
    max_mcp: int = 4,
) -> list[dict[str, Any]]:
    """Return a bounded task-specific tool set instead of injecting the full registry."""
    lowered = prompt.lower()
    selected = {"list_files", "search_files", "read_file", "file_metadata"}
    selected.update(name for name in planned_tools if name in BASE_TOOL_INDEX)
    keyword_groups = (
        (("批量", "整批", "一批", "batch", "transaction"), ("file_batch", "list_file_changes", "undo_task_changes")),
        (("创建", "新增", "写入", "修改", "修复", "替换", "create", "write", "modify", "fix", "replace"), ("file_diff", "create_file", "write_file", "replace_text", "apply_patch", "list_file_changes")),
        (("复制", "copy"), ("copy_file", "compare_files")),
        (("移动", "重命名", "move", "rename"), ("move_file", "rename_file")),
        (("删除", "移除", "delete", "remove"), ("delete_file",)),
        (("目录", "文件夹", "directory", "folder"), ("list_directory", "create_directory")),
        (("撤销", "回滚", "快照", "undo", "rollback", "snapshot"), ("list_file_changes", "undo_file_change", "undo_task_changes", "list_security_snapshots", "preview_security_snapshot", "restore_security_snapshot")),
        (("比较", "差异", "diff", "compare"), ("file_diff", "compare_files")),
        (("测试", "构建", "编译", "检查", "运行", "test", "build", "compile", "lint", "run"), ("run_command",)),
        (("记忆", "记住", "忘记", "memory", "remember", "forget"), ("list_workspace_memories", "remember_workspace", "forget_workspace_memory")),
        (("项目结构", "仓库结构", "代码地图", "仓库地图", "repo map", "codebase map"), ("get_repo_map",)),
        (("符号", "定义", "引用", "调用链", "依赖", "相关测试", "诊断", "symbol", "definition", "reference", "call chain", "dependency", "related test", "diagnostic"), ("find_symbol", "find_definition", "find_references", "list_module_dependencies", "find_related_tests", "get_call_chain", "inspect_diagnostics")),
        (("lsp", "language server", "go to definition", "find references"), ("lsp_query",)),
        (("worktree", "工作树", "隔离分支"), ("list_worktrees", "create_worktree", "remove_worktree")),
        (("搜索", "查找", "查询", "最新", "实时", "新闻", "今天", "现在", "当前", "search", "lookup", "find online", "current", "latest", "news", "today", "now", "recent"), ("web_search", "web_fetch")),
        (("文档", "报告", "日报", "周报", "幻灯片", "演示文稿", "markdown", "docx", "word", "pdf", "pptx", "powerpoint", "artifact"), ("artifact.markdown.create", "artifact.docx.create", "artifact.docx.edit", "artifact.pdf.create", "artifact.pdf.merge", "artifact.pdf.extract", "artifact.pptx.create", "artifact.pptx.edit", "artifact.render", "artifact.validate")),
        (("图片", "截图", "图表", "视觉", "识图", "ocr", "image", "screenshot", "chart", "vision"), ("vision.describe", "vision.extract_text", "vision.analyze_chart", "vision.compare", "vision.inspect_ui", "vision.classify")),
    )
    for keywords, names in keyword_groups:
        if any(keyword in lowered for keyword in keywords):
            selected.update(names)
    ordered_builtin = [item for item in BASE_TOOLS if item["function"]["name"] in selected][:max_builtin]

    prompt_terms = _tool_terms(prompt)
    scored_mcp: list[tuple[float, dict[str, Any]]] = []
    wants_mcp = any(token in lowered for token in ("mcp", "服务", "接口", "api", "远程"))
    for item in mcp_tools:
        function = item.get("function") or {}
        name = str(function.get("name") or "")
        description = str(function.get("description") or "")
        terms = _tool_terms(f"{name} {description}")
        overlap = len(prompt_terms & terms)
        direct = 2.0 if name.lower() in lowered else 0.0
        score = direct + float(overlap) + (0.1 if wants_mcp else 0.0)
        if score > 0:
            scored_mcp.append((score, item))
    scored_mcp.sort(key=lambda pair: (pair[0], str((pair[1].get("function") or {}).get("name") or "")), reverse=True)
    return [*ordered_builtin, *(item for _, item in scored_mcp[:max_mcp])]


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
        if isinstance(value, list):
            if len(value) < schema.get("minItems", len(value)) or len(value) > schema.get("maxItems", len(value)):
                raise ToolValidationError(f"参数 {field} 项目数量超出允许范围")
            item_type = (schema.get("items") or {}).get("type")
            if item_type == "string" and any(not isinstance(item, str) for item in value):
                raise ToolValidationError(f"参数 {field} 的项目类型应为 string")
            if item_type == "object" and any(not isinstance(item, dict) for item in value):
                raise ToolValidationError(f"参数 {field} 的项目类型应为 object")
        if "enum" in schema and value not in schema["enum"]:
            raise ToolValidationError(f"参数 {field} 不在允许范围内")
    return spec


def requires_confirmation(mode: str, risk: Risk) -> bool:
    if mode == "ask":
        return risk != "low"
    if mode == "agent":
        return risk in {"high", "critical"}
    return risk == "critical"
