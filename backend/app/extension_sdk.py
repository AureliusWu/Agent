from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import __version__
from .tool_registry import REGISTRY, ToolSpec
from .trust import detect_prompt_injection, redact_payload


IDENTIFIER = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
PermissionName = Literal[
    "workspace.read",
    "workspace.write",
    "workspace.delete",
    "memory.read",
    "memory.write",
    "process.execute",
    "network.external",
]
RiskName = Literal["low", "medium", "high", "critical"]


class ExtensionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtensionSignature(ExtensionModel):
    algorithm: Literal["sha256"] = "sha256"
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExtensionToolDeclaration(ExtensionModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,47}$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    delegate: str
    risk: RiskName
    fixed_arguments: dict[str, Any] = Field(default_factory=dict)


class ProfessionalAgentDeclaration(ExtensionModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,47}$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    system_prompt: str = Field(min_length=1, max_length=8000)
    tool_allowlist: list[str] = Field(default_factory=list, max_length=80)
    skill_tags: list[str] = Field(default_factory=list, max_length=20)
    completion_standards: list[str] = Field(min_length=1, max_length=20)
    verifier_id: str = Field(default="extension", pattern=r"^[a-z][a-z0-9._-]{1,63}$")
    default_permission: Literal["ask", "agent", "full"] = "ask"
    allow_mcp: bool = False


class ExtensionUiContribution(ExtensionModel):
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


class ExtensionManifest(ExtensionModel):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z][a-z0-9._-]{1,63}$")
    name: str = Field(min_length=1, max_length=100)
    version: str
    author: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    min_app_version: str = "0.13.0"
    permissions: list[PermissionName] = Field(default_factory=list, max_length=10)
    risk_level: RiskName = "low"
    dependencies: list[str] = Field(default_factory=list, max_length=20)
    tools: list[ExtensionToolDeclaration] = Field(default_factory=list, max_length=40)
    skills: list[str] = Field(default_factory=list, max_length=40)
    agents: list[ProfessionalAgentDeclaration] = Field(default_factory=list, max_length=20)
    ui: list[ExtensionUiContribution] = Field(default_factory=list, max_length=20)
    signature: ExtensionSignature | None = None

    @field_validator("version", "min_app_version")
    @classmethod
    def valid_version(cls, value: str) -> str:
        if not VERSION.fullmatch(value):
            raise ValueError("版本必须使用 SemVer，例如 1.2.0")
        return value

    @field_validator("dependencies")
    @classmethod
    def valid_dependencies(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(not IDENTIFIER.fullmatch(item) for item in values):
            raise ValueError("依赖 ID 必须唯一且符合命名规则")
        return values

    @field_validator("skills")
    @classmethod
    def safe_skill_paths(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            path = value.replace("\\", "/").strip("/")
            if not path.startswith("skills/") or not path.endswith("/SKILL.md") or ".." in path.split("/"):
                raise ValueError("Skill 必须位于 skills/<name>/SKILL.md")
            normalized.append(path)
        if len(normalized) != len(set(normalized)):
            raise ValueError("Skill 路径不能重复")
        return normalized

    @model_validator(mode="after")
    def unique_contributions(self) -> "ExtensionManifest":
        for label, values in (("工具", [item.id for item in self.tools]), ("Agent", [item.id for item in self.agents])):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} ID 不能重复")
        if self.id in self.dependencies:
            raise ValueError("扩展不能依赖自身")
        return self


def version_tuple(value: str) -> tuple[int, int, int]:
    match = VERSION.fullmatch(value)
    if match is None:
        raise ValueError(f"无效 SemVer：{value}")
    return tuple(int(item) for item in match.groups())


def extension_tool_name(extension_id: str, tool_id: str) -> str:
    safe_extension = re.sub(r"[^a-z0-9_]", "_", extension_id.lower())
    return f"ext__{safe_extension}__{tool_id}"


def required_permission(tool: ToolSpec) -> PermissionName:
    if tool.name == "run_command":
        return "process.execute"
    if tool.name in {"delete_file", "undo_file_change", "undo_task_changes", "restore_security_snapshot"}:
        return "workspace.delete"
    if tool.name in {"remember_workspace", "forget_workspace_memory"}:
        return "memory.write"
    if tool.name == "list_workspace_memories":
        return "memory.read"
    if tool.risk == "low":
        return "workspace.read"
    if tool.risk == "high":
        return "workspace.delete"
    return "workspace.write"


@dataclass(frozen=True)
class ExtensionToolRoute:
    name: str
    extension_id: str
    extension_version: str
    delegate: str
    risk: str
    fixed_arguments: dict[str, Any]
    description: str

    def resolve_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        overlap = set(arguments) & set(self.fixed_arguments)
        if overlap:
            raise ValueError(f"不允许覆盖扩展锁定参数：{', '.join(sorted(overlap))}")
        return {**arguments, **self.fixed_arguments}

    def openai(self) -> dict[str, Any]:
        spec = REGISTRY[self.delegate]
        properties = {key: value for key, value in spec.properties.items() if key not in self.fixed_arguments}
        required = [key for key in spec.required if key not in self.fixed_arguments]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            },
        }


def validate_manifest(manifest: ExtensionManifest) -> None:
    if version_tuple(manifest.min_app_version) > version_tuple(__version__):
        raise ValueError(f"扩展需要 Agent {manifest.min_app_version} 或更高版本")
    declared_permissions = set(manifest.permissions)
    local_tool_ids = {item.id for item in manifest.tools}
    for declaration in manifest.tools:
        spec = REGISTRY.get(declaration.delegate)
        if spec is None:
            raise ValueError(f"扩展工具 {declaration.id} 指向未知工具 {declaration.delegate}")
        if spec.risk == "critical":
            raise ValueError(f"扩展工具 {declaration.id} 不能代理 critical 工具")
        if RISK_ORDER[declaration.risk] < RISK_ORDER[spec.risk]:
            raise ValueError(f"扩展工具 {declaration.id} 不能降低底层工具风险级别")
        permission = required_permission(spec)
        if permission not in declared_permissions:
            raise ValueError(f"扩展工具 {declaration.id} 缺少权限声明 {permission}")
        unknown_fixed = set(declaration.fixed_arguments) - set(spec.properties)
        if unknown_fixed:
            raise ValueError(f"扩展工具 {declaration.id} 包含未知锁定参数")
        _, sensitive = redact_payload(declaration.fixed_arguments)
        if sensitive.redactions:
            raise ValueError(f"扩展工具 {declaration.id} 不能在清单中携带凭据")
        if detect_prompt_injection(declaration.description):
            raise ValueError(f"扩展工具 {declaration.id} 的描述包含不允许的指令注入特征")
        if RISK_ORDER[declaration.risk] > RISK_ORDER[manifest.risk_level]:
            raise ValueError(f"扩展风险级别低于工具 {declaration.id}")
    builtin_names = set(REGISTRY)
    for agent in manifest.agents:
        unknown = {
            item
            for item in agent.tool_allowlist
            if item not in builtin_names and item not in local_tool_ids and item != "extension:*"
        }
        if unknown:
            raise ValueError(f"专业 Agent {agent.id} 引用未声明工具：{', '.join(sorted(unknown))}")
        for tool_name in agent.tool_allowlist:
            spec = REGISTRY.get(tool_name)
            if spec is not None and required_permission(spec) not in declared_permissions:
                raise ValueError(f"专业 Agent {agent.id} 缺少工具 {tool_name} 的权限声明 {required_permission(spec)}")
            if spec is not None and RISK_ORDER[manifest.risk_level] < RISK_ORDER[spec.risk]:
                raise ValueError(f"扩展风险级别低于专业 Agent {agent.id} 允许的工具 {tool_name}")
        if agent.allow_mcp and "network.external" not in declared_permissions:
            raise ValueError(f"专业 Agent {agent.id} 启用 MCP 前必须声明 network.external")


def build_tool_routes(manifest: ExtensionManifest) -> dict[str, ExtensionToolRoute]:
    validate_manifest(manifest)
    return {
        extension_tool_name(manifest.id, item.id): ExtensionToolRoute(
            name=extension_tool_name(manifest.id, item.id),
            extension_id=manifest.id,
            extension_version=manifest.version,
            delegate=item.delegate,
            risk=item.risk,
            fixed_arguments=dict(item.fixed_arguments),
            description=f"[{manifest.name}] {item.description}",
        )
        for item in manifest.tools
    }
