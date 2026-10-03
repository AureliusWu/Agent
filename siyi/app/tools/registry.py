from __future__ import annotations

import re
from typing import Any

from app.plugins.registry import PLUGIN_LIBRARY
from app.tools.spec import ToolSpec, Risk, Interruptibility, ConcurrencyPolicy, BLOCKING_TOOLS, EXCLUSIVE_TOOLS


# Compatibility exports: plugin modules are the only source of tool declarations.
SPECS = list(PLUGIN_LIBRARY.tool_specs())
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
    selected = {"discover_tools", "list_files", "search_files", "read_file", "file_metadata"}
    selected.update(name for name in planned_tools if name in BASE_TOOL_INDEX)
    keyword_groups = (
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
        (("转写", "转录", "语音识别", "transcribe", "transcription"), ("transcribe_audio",)),
        (("语音合成", "朗读", "读出来", "tts", "synthesize", "text to speech"), ("synthesize_speech",)),
        (("搜索", "查找", "查询", "最新", "实时", "新闻", "今天", "现在", "当前", "search", "lookup", "find online", "current", "latest", "news", "today", "now", "recent"), ("web_search", "web_fetch")),
    )
    for keywords, names in keyword_groups:
        if any(keyword in lowered for keyword in keywords):
            selected.update(names)
    ordered_builtin = [item for item in BASE_TOOLS if item["function"]["name"] in selected and PLUGIN_LIBRARY.configured(item["function"]["name"])][:max_builtin]

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
    if not isinstance(arguments, dict):
        raise ToolValidationError("工具参数必须为对象")
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
