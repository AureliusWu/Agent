from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.config import settings
from app.data_flow import record_data_flow
from app.database import connect, now_iso, rows
from app.sandbox import safe_path, workspace_root
from app.security.trust import secure_untrusted_text
from app.security.policy import scan_third_party_skill, skill_allowed_in_domain
from app.tools.registry import REGISTRY


_SKILL_CONTENT_CACHE: dict[str, tuple[int, int, str]] = {}
_SKILL_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_LIST_FIELDS = {
    "requires_tools",
    "requires_skills",
    "permissions",
    "platforms",
    "trigger_examples",
    "negative_trigger_examples",
}
_REQUIRED_FIELDS = {
    "name",
    "version",
    "description",
    "type",
    "entrypoint",
    "requires_tools",
    "requires_skills",
    "permissions",
    "platforms",
    "risk",
    "license",
    "trigger_examples",
    "negative_trigger_examples",
}
_ALLOWED_PERMISSIONS = {
    "files.read",
    "files.write",
    "network",
    "process",
    "memory.read",
    "memory.write",
    "artifacts.write",
    "filesystem.read",
    "filesystem.write",
    "filesystem.delete",
    "process.execute",
    "network.request",
    "secret.read",
    "clipboard.read",
    "clipboard.write",
    "camera.read",
    "microphone.read",
    "connector.access",
    "skill.install",
    "skill.modify",
}
_PERMISSION_ALIASES = {
    "files.read": "filesystem.read",
    "files.write": "filesystem.write",
    "network": "network.request",
    "process": "process.execute",
}
_ALLOWED_RISKS = {"low", "medium", "high", "critical"}
_CORE_LOGICAL_TOOLS = {
    "file.read",
    "file.write",
    "file.patch",
    "file.copy",
    "file.move",
    "file.rename",
    "file.delete",
    "file.restore",
    "file.search",
    "file.list",
    "file.stat",
    "directory.create",
    "directory.list",
    "directory.move",
    "directory.delete",
}


class SkillManifestError(ValueError):
    pass


@dataclass(frozen=True)
class SkillManifest:
    name: str
    version: str
    description: str
    type: str
    entrypoint: str
    requires_tools: tuple[str, ...]
    requires_skills: tuple[str, ...]
    permissions: tuple[str, ...]
    platforms: tuple[str, ...]
    risk: str
    license: str
    trigger_examples: tuple[str, ...]
    negative_trigger_examples: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        payload = asdict(self)
        for field in _LIST_FIELDS:
            payload[field] = list(payload[field])
        return payload


def _builtin_skill(
    name: str,
    description: str,
    skill_type: str,
    *,
    tools: tuple[str, ...],
    permissions: tuple[str, ...],
    risk: str,
    triggers: tuple[str, ...],
    negative: tuple[str, ...],
    body: str,
) -> dict[str, Any]:
    manifest = SkillManifest(
        name=name,
        version="1.0.0",
        description=description,
        type=skill_type,
        entrypoint="SKILL.md",
        requires_tools=tools,
        requires_skills=(),
        permissions=permissions,
        platforms=("windows",),
        risk=risk,
        license="Siyi-Builtin",
        trigger_examples=triggers,
        negative_trigger_examples=negative,
    )
    return {"manifest": manifest, "content": body.strip()}


BUILTIN_SKILLS = {
    "release-checklist": _builtin_skill(
        "release-checklist",
        "从预检到本地发布证据的严格软件发布清单",
        "workflow",
        tools=("file.read", "file.stat", "run_command"),
        permissions=("files.read", "process"),
        risk="high",
        triggers=("发布版本", "构建安装包", "release checklist"),
        negative=("只解释版本号", "不要构建"),
        body="按预检、专项测试、全量回归、版本一致性、隐私、封包、安装烟测、性能和证据顺序执行。未运行不得写 PASS。",
    ),
    "data-reliability-audit": _builtin_skill(
        "data-reliability-audit",
        "审计数据时间、缺失值、来源、降级和用户可见语义",
        "audit",
        tools=("file.read", "file.search", "file.stat"),
        permissions=("files.read",),
        risk="low",
        triggers=("数据可靠性", "过期数据", "缺失值", "stale data"),
        negative=("只改颜色", "纯文案润色"),
        body="核对来源时间、观测时间、空值、降级、缓存与用户可见值；禁止以请求时间或 0 代替缺失。",
    ),
    "performance-regression-check": _builtin_skill(
        "performance-regression-check",
        "用固定基线和重复样本检查性能回归",
        "audit",
        tools=("file.read", "run_command"),
        permissions=("files.read", "process"),
        risk="high",
        triggers=("性能回归", "启动速度", "响应变慢"),
        negative=("只问理论复杂度", "不要运行性能测试"),
        body="先绑定基线、硬件与构建标识，再运行重复样本；报告原始样本、中位数、阈值和异常抖动。",
    ),
    "deploy-smoke-test": _builtin_skill(
        "deploy-smoke-test",
        "对已构建或部署产物做端到端烟测",
        "deployment",
        tools=("file.stat", "run_command"),
        permissions=("files.read", "process"),
        risk="high",
        triggers=("部署烟测", "安装包验证", "deploy smoke"),
        negative=("尚未构建", "只写部署方案"),
        body="核对构建身份、启动、健康、关键 API、进程清理、升级、卸载保留与重装识别；分别报告可执行证据与 UI 证据。",
    ),
    "daily-report": _builtin_skill(
        "daily-report",
        "把已验证工作内容整理为中文日报或周报",
        "report",
        tools=("file.read", "artifact.docx.create", "artifact.validate"),
        permissions=("files.read", "files.write", "artifacts.write"),
        risk="medium",
        triggers=("写日报", "工作日报", "生成周报"),
        negative=("写日记", "只要一句总结"),
        body="按结果、完成事项、验证证据、风险和下一步组织内容；调用统一 Artifact Engine 生成并验证 DOCX，不得把未执行项写成完成。",
    ),
    "ui-design": _builtin_skill(
        "ui-design",
        "统一既有产品、绿地产品和严格设计系统三类 UI 设计模式",
        "design",
        tools=("file.read", "file.search", "file.patch"),
        permissions=("files.read", "files.write"),
        risk="medium",
        triggers=("设计界面", "改版 UI", "design system", "frontend design"),
        negative=("只修后端", "数据迁移"),
        body=(
            "先选择模式：existing-product 优先保持现有语言与组件；greenfield-product 先建立视觉方向；"
            "strict-design-system 严格使用既有 Token、组件和间距。设计必须保留可访问性、响应式与真实状态。"
        ),
    ),
}


def _read_skill(path: Path) -> str:
    stat = path.stat()
    key = str(path)
    cached = _SKILL_CONTENT_CACHE.get(key)
    signature = (stat.st_mtime_ns, stat.st_size)
    if cached and cached[:2] == signature:
        return cached[2]
    text = path.read_text(encoding="utf-8", errors="replace")
    _SKILL_CONTENT_CACHE[key] = (signature[0], signature[1], text)
    return text


def _read_manifest_prefix(path: Path) -> str:
    lines: list[str] = []
    delimiters = 0
    total = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            total += len(line)
            if total > 64_000:
                raise SkillManifestError("Skill Manifest 超过 64 KB")
            lines.append(line)
            if line.strip() == "---":
                delimiters += 1
                if delimiters == 2:
                    break
    return "".join(lines)


def _scalar(value: str) -> Any:
    stripped = value.strip()
    if not stripped:
        return ""
    if stripped.startswith("[") and stripped.endswith("]"):
        try:
            parsed = json.loads(stripped.replace("'", '"'))
        except ValueError as exc:
            raise SkillManifestError(f"无效的 Manifest 列表：{stripped}") from exc
        return parsed
    if (
        len(stripped) >= 2
        and stripped[0] == stripped[-1]
        and stripped[0] in {"'", '"'}
    ):
        return stripped[1:-1]
    return stripped


def parse_skill_manifest(text: str) -> SkillManifest:
    if not text.startswith("---"):
        raise SkillManifestError("SKILL.md 必须以 YAML front matter 开始")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise SkillManifestError("SKILL.md Manifest 未闭合")
    payload: dict[str, Any] = {}
    current_list: str | None = None
    for raw_line in parts[1].splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")) and line.strip().startswith("- "):
            if current_list is None:
                raise SkillManifestError("Manifest 列表项缺少字段名")
            payload.setdefault(current_list, []).append(_scalar(line.strip()[2:]))
            continue
        if ":" not in line:
            raise SkillManifestError(f"无效的 Manifest 行：{line}")
        key, value = line.split(":", 1)
        key = key.strip()
        current_list = key if key in _LIST_FIELDS else None
        parsed = _scalar(value)
        payload[key] = [] if current_list and parsed == "" else parsed
    missing = sorted(_REQUIRED_FIELDS - set(payload))
    if missing:
        raise SkillManifestError(f"Skill Manifest 缺少字段：{', '.join(missing)}")
    for field in _LIST_FIELDS:
        if not isinstance(payload[field], list) or any(
            not isinstance(item, str) or not item.strip() for item in payload[field]
        ):
            raise SkillManifestError(f"Skill Manifest 字段 {field} 必须是字符串列表")
    manifest = SkillManifest(
        name=str(payload["name"]).strip(),
        version=str(payload["version"]).strip(),
        description=str(payload["description"]).strip(),
        type=str(payload["type"]).strip(),
        entrypoint=str(payload["entrypoint"]).strip(),
        requires_tools=tuple(dict.fromkeys(payload["requires_tools"])),
        requires_skills=tuple(dict.fromkeys(payload["requires_skills"])),
        permissions=tuple(dict.fromkeys(payload["permissions"])),
        platforms=tuple(dict.fromkeys(payload["platforms"])),
        risk=str(payload["risk"]).strip(),
        license=str(payload["license"]).strip(),
        trigger_examples=tuple(payload["trigger_examples"]),
        negative_trigger_examples=tuple(payload["negative_trigger_examples"]),
    )
    validate_skill_manifest(manifest)
    return manifest


def _required_permissions(tools: tuple[str, ...]) -> set[str]:
    required: set[str] = set()
    for tool in tools:
        if tool in {"run_command"}:
            required.add("process.execute")
        elif tool in {"web_search", "web_fetch"}:
            required.add("network.request")
        elif tool in {
            "delete_file",
            "delete_directory",
            "file.delete",
            "directory.delete",
        }:
            required.add("filesystem.delete")
        elif tool in {
            "artifact.markdown.create",
            "artifact.docx.create",
            "artifact.docx.edit",
            "artifact.pdf.create",
            "artifact.pdf.merge",
            "artifact.pptx.create",
            "artifact.pptx.edit",
            "artifact.render",
        }:
            required.update({"filesystem.write", "artifacts.write"})
        elif tool in {
            "create_file",
            "write_file",
            "replace_text",
            "apply_patch",
            "copy_file",
            "move_file",
            "rename_file",
            "create_directory",
        } or tool in {
            "file.write",
            "file.patch",
            "file.copy",
            "file.move",
            "file.rename",
            "file.restore",
            "directory.create",
            "directory.move",
        }:
            required.add("filesystem.write")
        elif tool.startswith(("file.", "directory.", "artifact.")) or tool in REGISTRY:
            required.add("filesystem.read")
    return required


def validate_skill_manifest(manifest: SkillManifest) -> None:
    if not _SKILL_NAME.fullmatch(manifest.name):
        raise SkillManifestError("Skill name 只能包含字母、数字、点、下划线或连字符")
    if not _SEMVER.fullmatch(manifest.version):
        raise SkillManifestError("Skill version 必须是语义版本")
    if not manifest.description or len(manifest.description) > 500:
        raise SkillManifestError("Skill description 不能为空且不得超过 500 字符")
    if manifest.entrypoint != "SKILL.md":
        raise SkillManifestError("Skill Runtime 1.0 仅允许 SKILL.md 文本入口")
    unknown_tools = set(manifest.requires_tools) - set(REGISTRY) - _CORE_LOGICAL_TOOLS
    if unknown_tools:
        raise SkillManifestError(f"Skill 请求了未知工具：{', '.join(sorted(unknown_tools))}")
    unknown_permissions = set(manifest.permissions) - _ALLOWED_PERMISSIONS
    if unknown_permissions:
        raise SkillManifestError(f"Skill 请求了未知权限：{', '.join(sorted(unknown_permissions))}")
    normalized_permissions = {
        _PERMISSION_ALIASES.get(permission, permission)
        for permission in manifest.permissions
    }
    missing_permissions = _required_permissions(manifest.requires_tools) - normalized_permissions
    if missing_permissions:
        raise SkillManifestError(f"Skill 缺少工具所需权限：{', '.join(sorted(missing_permissions))}")
    if manifest.risk not in _ALLOWED_RISKS:
        raise SkillManifestError("Skill risk 必须是 low/medium/high/critical")
    if not manifest.platforms or not set(manifest.platforms) <= {"windows", "all"}:
        raise SkillManifestError("当前 PC Runtime 仅接受 windows/all 平台")
    if not manifest.license:
        raise SkillManifestError("Skill license 不能为空")
    if not manifest.trigger_examples or not manifest.negative_trigger_examples:
        raise SkillManifestError("Skill 必须同时声明正向和负向触发示例")
    for dependency in manifest.requires_skills:
        if not _SKILL_NAME.fullmatch(dependency):
            raise SkillManifestError(f"无效的 Skill 依赖：{dependency}")


def _terms(value: str) -> set[str]:
    lowered = value.lower()
    words = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    words.update(
        chinese[index : index + 2] for index in range(max(0, len(chinese) - 1))
    )
    return {word for word in words if word}


def _setting(key: str) -> bool:
    value = rows("SELECT enabled FROM skill_settings WHERE path=?", (key,))
    return bool(value[0]["enabled"]) if value else True


def _skill_item(
    manifest: SkillManifest,
    *,
    path: str,
    setting_key: str,
    source: str,
    extension_id: str | None = None,
    content: str | None = None,
    location: Path | None = None,
) -> dict[str, Any]:
    return {
        **manifest.public(),
        "path": path,
        "enabled": _setting(setting_key),
        "source": source,
        "extension_id": extension_id,
        "status": "ready",
        "error": None,
        "_content": content,
        "_location": str(location) if location else None,
    }


def discover_skills(
    workspace: str,
    include_content: bool = False,
    *,
    _include_location: bool = False,
) -> list[dict[str, Any]]:
    root = workspace_root(workspace)
    found: list[dict[str, Any]] = []
    for builtin in BUILTIN_SKILLS.values():
        manifest: SkillManifest = builtin["manifest"]
        path = f"builtin:{manifest.name}@{manifest.version}"
        found.append(
            _skill_item(
                manifest,
                path=path,
                setting_key=path,
                source="builtin",
                content=str(builtin["content"]),
            )
        )
    for skill_root in (root / ".agent" / "skills", root / ".codex" / "skills"):
        if not skill_root.exists():
            continue
        for entrypoint in skill_root.glob("*/SKILL.md"):
            relative = str(entrypoint.relative_to(root))
            try:
                manifest = parse_skill_manifest(_read_manifest_prefix(entrypoint))
                found.append(
                    _skill_item(
                        manifest,
                        path=relative,
                        setting_key=f"{root}|{relative}",
                        source="workspace",
                        location=entrypoint,
                    )
                )
            except (OSError, SkillManifestError) as exc:
                found.append(
                    {
                        "name": entrypoint.parent.name,
                        "version": "invalid",
                        "description": "",
                        "path": relative,
                        "enabled": False,
                        "source": "workspace",
                        "extension_id": None,
                        "status": "invalid",
                        "error": str(exc),
                        "_location": str(entrypoint),
                        "_content": None,
                    }
                )
    try:
        from app.extensions.runtime import active_extension_skill_paths

        extension_skills = active_extension_skill_paths()
    except (ImportError, RuntimeError, ValueError):
        extension_skills = []
    for extension in extension_skills:
        entrypoint = Path(extension["absolute_path"])
        if not entrypoint.is_file():
            continue
        display_path = (
            f"extension:{extension['extension_id']}:{extension['version']}/{extension['path']}"
        )
        try:
            manifest = parse_skill_manifest(_read_manifest_prefix(entrypoint))
            found.append(
                _skill_item(
                    manifest,
                    path=display_path,
                    setting_key=display_path,
                    source="extension",
                    extension_id=extension["extension_id"],
                    location=entrypoint,
                )
            )
        except (OSError, SkillManifestError) as exc:
            found.append(
                {
                    "name": entrypoint.parent.name,
                    "version": str(extension["version"]),
                    "description": "",
                    "path": display_path,
                    "enabled": False,
                    "source": "extension",
                    "extension_id": extension["extension_id"],
                    "status": "invalid",
                    "error": str(exc),
                    "_location": str(entrypoint),
                    "_content": None,
                }
            )
    found = [
        item for item in found if skill_allowed_in_domain(str(item.get("name") or ""))
    ]
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in found:
        if item.get("status") == "ready" and item.get("enabled"):
            groups.setdefault(str(item["name"]).casefold(), []).append(item)
    for matches in groups.values():
        if len(matches) > 1:
            versions = sorted({str(item["version"]) for item in matches})
            for item in matches:
                item["status"] = "conflict"
                item["error"] = (
                    f"Skill 名称冲突：{item['name']}，候选版本 {', '.join(versions)}"
                )
    public: list[dict[str, Any]] = []
    for item in found:
        result = dict(item)
        if include_content and result.get("status") == "ready":
            if result.get("_content") is not None:
                result["content"] = str(result["_content"])[:20_000]
            elif result.get("_location"):
                result["content"] = _read_skill(Path(result["_location"]))[:20_000]
        if not _include_location:
            result.pop("_location", None)
            result.pop("_content", None)
        public.append(result)
    return public


def _negative_match(prompt: str, examples: list[str]) -> bool:
    lowered = prompt.casefold()
    return any(example.casefold() in lowered for example in examples if example.strip())


def _record_skill_run(
    task_id: str | None,
    item: dict[str, Any],
    *,
    status: str,
    content_chars: int = 0,
    content_tokens: int = 0,
    trigger_reason: str = "",
    dependency_chain: list[str] | None = None,
    error: str | None = None,
) -> None:
    if not task_id:
        return
    with connect() as db:
        db.execute(
            "INSERT INTO skill_runs(task_id,name,path,version,source,content_chars,content_tokens,trigger_reason,dependency_chain,status,error,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                str(item.get("name") or "unknown"),
                str(item.get("path") or ""),
                str(item.get("version") or "0.0.0"),
                str(item.get("source") or "unknown"),
                content_chars,
                content_tokens,
                trigger_reason or None,
                json.dumps(dependency_chain or [], ensure_ascii=False),
                status,
                error,
                now_iso(),
            ),
        )


def _resolve_dependencies(
    selected: list[dict[str, Any]],
    available: dict[str, dict[str, Any]],
) -> list[tuple[dict[str, Any], list[str]]]:
    ordered: list[tuple[dict[str, Any], list[str]]] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item: dict[str, Any], chain: list[str]) -> None:
        name = str(item["name"])
        key = name.casefold()
        if key in visiting:
            raise SkillManifestError(f"Skill 依赖循环：{' -> '.join([*chain, name])}")
        if key in visited:
            return
        visiting.add(key)
        for dependency in item.get("requires_skills") or []:
            target = available.get(str(dependency).casefold())
            if target is None:
                raise SkillManifestError(f"Skill {name} 缺少依赖：{dependency}")
            visit(target, [*chain, name])
        visiting.remove(key)
        visited.add(key)
        ordered.append((item, [*chain, name]))

    for item in selected:
        visit(item, [])
    return ordered


def skill_context(workspace: str, user_prompt: str, task_id: str | None = None) -> str:
    skills = [
        item
        for item in discover_skills(
            workspace, include_content=False, _include_location=True
        )
        if item.get("enabled") and item.get("status") == "ready"
    ]
    if not skills:
        return ""
    prompt_terms = _terms(user_prompt)
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for item in skills:
        if _negative_match(user_prompt, list(item["negative_trigger_examples"])):
            continue
        label = f"{item['name']} {item['description']}"
        score = len(prompt_terms & _terms(label))
        trigger_hits = [
            example
            for example in item["trigger_examples"]
            if _terms(example) & prompt_terms
        ]
        score += max((len(_terms(example) & prompt_terms) for example in trigger_hits), default=0)
        if item["name"].casefold() in user_prompt.casefold():
            score += 5
        if score:
            scored.append((score, ",".join(trigger_hits)[:500], item))
    scored.sort(key=lambda value: (value[0], value[2]["name"]), reverse=True)
    initially_selected = [item for _, _, item in scored[: settings.max_skill_count]]
    reasons = {item["name"]: reason for _, reason, item in scored}
    available = {str(item["name"]).casefold(): item for item in skills}
    try:
        resolved = _resolve_dependencies(initially_selected, available)
    except SkillManifestError as exc:
        for item in initially_selected:
            _record_skill_run(
                task_id,
                item,
                status="error",
                trigger_reason=reasons.get(item["name"], ""),
                error=str(exc),
            )
        return ""
    selected: list[dict[str, Any]] = []
    total_chars = 0
    for item, chain in resolved:
        remaining = settings.max_skill_context_chars - total_chars
        if remaining <= 0:
            break
        try:
            content = (
                str(item["_content"])
                if item.get("_content") is not None
                else _read_skill(Path(item["_location"]))
            )
            content = content[: min(12_000, remaining)]
            secured, sensitive, findings = secure_untrusted_text(
                content, f"skill:{item['path']}"
            )
        except Exception as exc:
            _record_skill_run(
                task_id,
                item,
                status="error",
                trigger_reason=reasons.get(item["name"], "dependency"),
                dependency_chain=chain,
                error=str(exc),
            )
            return ""
        tokens = max(1, (len(secured) + 3) // 4)
        selected.append({**item, "content": secured})
        total_chars += len(secured)
        _record_skill_run(
            task_id,
            item,
            status="loaded",
            content_chars=len(secured),
            content_tokens=tokens,
            trigger_reason=reasons.get(item["name"], "dependency"),
            dependency_chain=chain,
        )
        record_data_flow(
            source=f"skill:{item['path']}",
            sink="model_context",
            classification=sensitive.classification,
            fields=("skill_content",),
            redactions=sensitive.redactions,
            allowed=True,
            reason=(
                f"untrusted skill; injection findings: {','.join(findings)}"
                if findings
                else "untrusted skill data"
            ),
            task_id=task_id,
        )
    instructions = "\n\n".join(
        f"### Skill: {item['name']} v{item['version']}\n{item['content']}"
        for item in selected
    )
    return (
        f"本轮按需加载的 Skill 指令（共 {len(selected)} 个，约 "
        f"{sum(max(1, (len(item['content']) + 3) // 4) for item in selected)} Token）：\n"
        f"{instructions}"
        if instructions
        else ""
    )


def install_skill(workspace: str, name: str, content: str) -> dict[str, str]:
    if len(content.encode("utf-8")) > 200_000:
        raise ValueError("Skill content exceeds 200 KB")
    manifest = parse_skill_manifest(content)
    if name != manifest.name:
        raise ValueError("请求名称必须与 Skill Manifest name 一致")
    findings = scan_third_party_skill(content)
    if findings:
        raise ValueError(f"Skill 静态扫描未通过：{', '.join(findings)}")
    if not skill_allowed_in_domain(name):
        raise ValueError("该 Skill 仅允许在开发者或管理员域安装")
    root = workspace_root(workspace)
    target = safe_path(root, f".agent/skills/{name}/SKILL.md")
    archived_previous: Path | None = None
    quarantine = safe_path(
        root, f".agent/skill-quarantine/{uuid.uuid4().hex}/{name}/SKILL.md"
    )
    quarantine.parent.mkdir(parents=True, exist_ok=False)
    quarantine.write_text(content, encoding="utf-8")
    staged = parse_skill_manifest(_read_skill(quarantine))
    if staged != manifest:
        shutil.rmtree(quarantine.parents[1], ignore_errors=True)
        raise ValueError("Skill 隔离区校验结果不一致")
    if target.is_file():
        current = parse_skill_manifest(_read_skill(target))
        if current.version == manifest.version:
            shutil.rmtree(quarantine.parents[1], ignore_errors=True)
            raise ValueError(f"Skill {name} v{manifest.version} 已安装")
        archive = safe_path(
            root,
            f".agent/skill-versions/{name}/{current.version}/SKILL.md",
        )
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, archive)
        archived_previous = archive
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(quarantine), str(target))
    except OSError:
        if archived_previous is not None and archived_previous.is_file():
            shutil.copy2(archived_previous, target)
        raise
    shutil.rmtree(quarantine.parents[1], ignore_errors=True)
    _SKILL_CONTENT_CACHE.pop(str(target), None)
    return {
        "name": name,
        "version": manifest.version,
        "path": str(target.relative_to(root)),
    }


def uninstall_skill(workspace: str, path: str) -> dict[str, str]:
    root = workspace_root(workspace)
    if path.startswith(("builtin:", "extension:")):
        raise ValueError("内置或 Extension Skill 必须通过对应生命周期停用或卸载")
    entrypoint = safe_path(root, path, must_exist=True)
    if entrypoint.name != "SKILL.md" or entrypoint.parent.parent.name != "skills":
        raise ValueError("只能卸载工作区 Skill")
    archive = safe_path(
        root,
        f".agent/uninstalled-skills/{entrypoint.parent.name}-{uuid.uuid4().hex[:8]}",
    )
    archive.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(entrypoint.parent), str(archive))
    _SKILL_CONTENT_CACHE.pop(str(entrypoint), None)
    return {
        "path": path,
        "archived_to": str(archive.relative_to(root)),
    }
