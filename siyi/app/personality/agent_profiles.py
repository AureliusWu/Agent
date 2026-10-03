from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from app.cognition.planning import AcceptanceCriterion, TaskPlan


READ_TOOLS = (
    "discover_tools",
    "list_files",
    "list_directory",
    "search_files",
    "search_text",
    "read_file",
    "read_file_range",
    "file_metadata",
    "file_info",
    "file_diff",
    "view_diff",
    "compare_files",
    "list_file_changes",
    "list_security_snapshots",
    "preview_security_snapshot",
    "list_workspace_memories",
    "get_repo_map",
    "find_symbol",
    "find_definition",
    "find_references",
    "list_module_dependencies",
    "find_related_tests",
    "get_call_chain",
    "inspect_diagnostics",
    "lsp_query",
    "list_worktrees",
)
WRITE_TOOLS = (
    "create_file",
    "write_file",
    "replace_text",
    "apply_patch",
    "copy_file",
    "move_file",
    "rename_file",
    "create_directory",
)
RECOVERY_TOOLS = (
    "undo_file_change",
    "undo_task_changes",
    "restore_security_snapshot",
)
MEMORY_TOOLS = ("remember_workspace", "forget_workspace_memory")


@dataclass(frozen=True)
class AgentProfile:
    id: str
    name: str
    description: str
    system_prompt: str
    tool_allowlist: tuple[str, ...]
    skill_tags: tuple[str, ...]
    completion_standards: tuple[str, ...]
    verifier_id: str
    default_permission: str
    allow_mcp: bool = False
    source: str = "builtin"
    extension_id: str | None = None
    extension_version: str | None = None

    def catalog(self) -> dict[str, Any]:
        return asdict(self)

    def allows_tool(self, name: str) -> bool:
        if "*" in self.tool_allowlist:
            return True
        if name.startswith("mcp__"):
            return self.allow_mcp
        if name.startswith("ext__") and "extension:*" in self.tool_allowlist:
            return True
        return name in self.tool_allowlist


BUILTIN_PROFILES: dict[str, AgentProfile] = {
    "general": AgentProfile(
        id="general",
        name="基础Agent",
        description="司忆唯一的基础 Agent，按任务自动选择工具并完成验证。",
        system_prompt="你是基础Agent。先识别真实目标和边界，再选择最小充分工具集完成任务；不要声称切换成另一个 Agent。",
        tool_allowlist=("*",),
        skill_tags=("general",),
        completion_standards=("回答与用户目标一致", "所有执行声明都有真实工具证据"),
        verifier_id="core",
        default_permission="ask",
        allow_mcp=True,
    ),
    "coding": AgentProfile(
        id="coding",
        name="编程 Agent",
        description="面向代码审查、实现、测试、构建与发布准备。",
        system_prompt="你是编程 Agent。尊重现有架构和代码风格，控制改动范围，并以测试、构建和实际运行证据证明结果。",
        tool_allowlist=(*READ_TOOLS, *WRITE_TOOLS, "delete_file", "run_command", "create_worktree", "remove_worktree", *RECOVERY_TOOLS, *MEMORY_TOOLS, "extension:*"),
        skill_tags=("coding", "testing", "release"),
        completion_standards=("先读取相关代码再修改", "代码改动后运行项目已有验证", "不掩盖失败或无关改动"),
        verifier_id="coding",
        default_permission="agent",
        allow_mcp=True,
    ),
    "data": AgentProfile(
        id="data",
        name="数据 Agent",
        description="面向结构化数据检查、分析、转换与结果验证。",
        system_prompt="你是数据 Agent。保留原始数据，明确口径、缺失值和日期语义，并让计算过程可复现、结论可追溯。",
        tool_allowlist=(*READ_TOOLS, "create_file", "write_file", "replace_text", "apply_patch", "copy_file", "create_directory", "run_command", *MEMORY_TOOLS, "extension:*"),
        skill_tags=("data", "analysis", "quality"),
        completion_standards=("不把缺失值擅自当作零", "说明数据来源和口径", "分析结果具有可复现证据"),
        verifier_id="data",
        default_permission="ask",
        allow_mcp=True,
    ),
    "documents": AgentProfile(
        id="documents",
        name="文档 Agent",
        description="面向 Markdown、文本与办公文档的整理和交付。",
        system_prompt="你是文档 Agent。保持事实和原意，使用清晰结构与一致术语；办公文档必须通过统一 Artifact Engine 创建、编辑、渲染和验证，不能只凭文件存在宣告完成。",
        tool_allowlist=(
            *READ_TOOLS,
            "create_file", "write_file", "replace_text", "apply_patch",
            "copy_file", "create_directory", "run_command",
            "artifact.markdown.create", "artifact.docx.create", "artifact.docx.edit",
            "artifact.pdf.create", "artifact.pdf.merge", "artifact.pdf.extract",
            "artifact.pptx.create", "artifact.pptx.edit",
            "artifact.render", "artifact.validate",
            *MEMORY_TOOLS, "extension:*",
        ),
        skill_tags=("document", "writing", "office"),
        completion_standards=("不遗漏用户指定内容", "结构和术语一致", "交付文件可打开且经过检查"),
        verifier_id="documents",
        default_permission="ask",
        allow_mcp=True,
    ),
    "file_organizer": AgentProfile(
        id="file_organizer",
        name="文件整理 Agent",
        description="面向工作区内文件分类、复制、移动、重命名与清理。",
        system_prompt="你是文件整理 Agent。先盘点再操作，避免覆盖和误删；移动、重命名或删除前必须明确来源、目标和回滚路径。",
        tool_allowlist=(*READ_TOOLS, "copy_file", "move_file", "rename_file", "create_directory", "delete_file", *RECOVERY_TOOLS, "extension:*"),
        skill_tags=("files", "organization"),
        completion_standards=("操作仅限已选工作区", "避免名称冲突和覆盖", "高风险操作可追溯并可恢复"),
        verifier_id="file_organizer",
        default_permission="ask",
        allow_mcp=False,
    ),
}


def _extension_profiles() -> list[AgentProfile]:
    try:
        from app.extensions.runtime import active_extension_profiles

        payloads = active_extension_profiles()
    except (ImportError, RuntimeError, ValueError):
        return []
    profiles: list[AgentProfile] = []
    for payload in payloads:
        try:
            profile = AgentProfile(
                id=str(payload["id"]),
                name=str(payload["name"]),
                description=str(payload["description"]),
                system_prompt=str(payload["system_prompt"]),
                tool_allowlist=tuple(str(item) for item in payload.get("tool_allowlist") or ()),
                skill_tags=tuple(str(item) for item in payload.get("skill_tags") or ()),
                completion_standards=tuple(str(item) for item in payload.get("completion_standards") or ()),
                verifier_id=str(payload.get("verifier_id") or "extension"),
                default_permission=str(payload.get("default_permission") or "ask"),
                allow_mcp=bool(payload.get("allow_mcp")),
                source="extension",
                extension_id=str(payload["extension_id"]),
                extension_version=str(payload["extension_version"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if profile.id not in BUILTIN_PROFILES:
            profiles.append(profile)
    return profiles


def list_agent_profiles() -> list[AgentProfile]:
    return [*BUILTIN_PROFILES.values(), *_extension_profiles()]


def get_agent_profile(profile_id: str) -> AgentProfile | None:
    if profile_id == "base":
        return BUILTIN_PROFILES["general"]
    builtin = BUILTIN_PROFILES.get(profile_id)
    if builtin is not None:
        return builtin
    return next((profile for profile in _extension_profiles() if profile.id == profile_id), None)


def require_agent_profile(profile_id: str) -> AgentProfile:
    profile = get_agent_profile(profile_id)
    if profile is None:
        raise ValueError(f"专业 Agent 不存在或已停用：{profile_id}")
    return profile


def filter_profile_tools(tools: list[dict[str, Any]], profile: AgentProfile) -> list[dict[str, Any]]:
    return [item for item in tools if profile.allows_tool(str((item.get("function") or {}).get("name") or ""))]


def apply_profile_to_plan(plan: TaskPlan, profile: AgentProfile) -> TaskPlan:
    if profile.id == "general":
        return plan
    criterion = AcceptanceCriterion(
        id="profile_tool_scope",
        description=f"所有工具调用符合 {profile.name} 的能力边界",
        kind="profile_tool_scope",
        parameters={
            "profile_id": profile.id,
            "tool_allowlist": list(profile.tool_allowlist),
            "allow_mcp": profile.allow_mcp,
        },
    )
    if any(item.id == criterion.id for item in plan.acceptance_criteria):
        return plan
    return replace(plan, acceptance_criteria=(*plan.acceptance_criteria, criterion))
