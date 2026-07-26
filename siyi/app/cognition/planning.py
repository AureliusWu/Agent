from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.database import connect, now_iso, rows
from app.runtime.task_verifiers import detect_verifier_domains


FILE_PATTERN = re.compile(
    r"(?<![\w.-])([\w./\\-]+\.(?:py|js|jsx|ts|tsx|rs|go|java|cs|cpp|c|h|vue|svelte|json|toml|ya?ml|ini|md|txt|css|html))(?![\w-]|\.[A-Za-z0-9])",
    re.I,
)
WRITE_PATTERN = re.compile(
    r"(?:创建|新增|添加|更新|修改|修复|调整|优化|写入|删除|移除|移动|重命名|替换|保存|"
    r"\b(?:create|add|modify|update|fix|change|write|delete|remove|move|rename|replace|save)\b)",
    re.I,
)
NEGATED_WRITE_PATTERN = re.compile(
    r"(?:不要|不得|禁止|无需|不应|避免).{0,8}(?:修改|改动|创建|新增|添加|写入|删除|移除|移动|重命名|替换)|"
    r"\b(?:do not|don't|must not|without)\b.{0,20}\b(?:modify|change|create|write|delete|remove|move|rename)\b",
    re.I,
)
PLAN_ONLY_PATTERN = re.compile(
    r"(?:(?:制定|给出|生成|提出|说明|设计|讨论).{0,12})?(?:修改|修复|调整|优化|重构)(?:计划|方案|建议|思路|步骤)|"
    r"\b(?:plan|proposal|suggestion|approach)\s+(?:for|to)\s+(?:modify|fix|change|refactor)\b",
    re.I,
)
DELETE_PATTERN = re.compile(r"(?:删除|移除|\b(?:delete|remove)\b)", re.I)
MOVE_PATTERN = re.compile(r"(?:移动|重命名|\b(?:move|rename)\b)", re.I)
VERIFY_PATTERN = re.compile(
    r"(?:运行|执行|重新运行|通过).{0,12}(?:测试|构建|编译|检查)|"
    r"\b(?:run|execute|rerun).{0,20}(?:test|build|compile|lint|check)|"
    r"\b(?:pytest|npm test|npm run build|typecheck|cargo test|cargo check)\b",
    re.I,
)
WORKSPACE_PATTERN = re.compile(
    r"(?:项目|代码|文件|目录|仓库|测试|配置|脚本|页面|接口|工作区|"
    r"\b(?:project|repository|repo|code|file|directory|folder|test|config|script|workspace)\b)",
    re.I,
)
CODE_TASK_PATTERN = re.compile(r"(?:代码|函数|方法|类|前端|后端|Python|JavaScript|TypeScript|Rust|\b(?:code|function|frontend|backend)\b)", re.I)
CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go", ".java", ".cs", ".cpp", ".c", ".h", ".vue", ".svelte"}
STRICT_SCOPE_PATTERN = re.compile(
    r"(?:不改动其他|不要修改其他|只修改|仅修改|控制修改范围|避免修改无关|\bonly\b.{0,20}\bfile)",
    re.I,
)
EXPECTED_FAILURE_PATTERN = re.compile(
    r"(?:如果.{0,20}(?:失败|拒绝|异常|不可用)|失败时|连接失败|沙箱拒绝|明确停止|不要绕过|不要伪造|"
    r"\b(?:if|when).{0,30}(?:fail|reject|unavailable)|do not bypass|do not fabricate)",
    re.I,
)
UNAVAILABLE_CAPABILITY_PATTERNS = (
    (re.compile(r"(?:未连接|离线|不可用).{0,12}(?:专用)?硬件|(?:专用)?硬件.{0,12}(?:未连接|离线|接口)", re.I), "任务依赖当前不可用的专用硬件接口"),
    (re.compile(r"(?:未连接|不可用).{0,12}(?:摄像头|麦克风|传感器|串口|蓝牙设备)", re.I), "任务依赖当前未连接的本地设备"),
)
CAPABILITY_ALIASES = (
    ("sensor", ("传感器", "sensor")),
    ("temperature", ("温度", "temperature")),
    ("serial", ("串口", "serial")),
    ("bluetooth", ("蓝牙", "bluetooth")),
    ("camera", ("摄像头", "camera")),
    ("microphone", ("麦克风", "microphone")),
)


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    description: str
    kind: str
    required: bool = True
    parameters: dict[str, Any] = field(default_factory=dict)

    @property
    def requirement_id(self) -> str:
        return self.id


@dataclass(frozen=True)
class PlanStep:
    id: str
    description: str
    depends_on: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    risk: str = "low"


@dataclass(frozen=True)
class TaskPlan:
    task_id: str
    goal: str
    task_kind: str
    steps: tuple[PlanStep, ...]
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    expected_paths: tuple[str, ...]
    strict_scope: bool
    expects_failure_handling: bool
    blocked_reason: str | None = None
    assumptions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    expected_changes: tuple[str, ...] = ()
    forbidden_changes: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    preferred_executor: str = "local_windows"
    risk: str = "low"
    requires_user_input: bool = False
    interaction_mode: str = "agent"
    data_location: str = "local_workspace"
    privacy_scope: str = "workspace"
    budget_limit: int = 0
    preferred_model: str = ""
    memory_write_policy: str = "explicit"
    planner_source: str = "deterministic"
    schema_version: str = "1.0"
    policy_decisions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mentioned_paths(prompt: str) -> tuple[str, ...]:
    paths: list[str] = []
    for match in FILE_PATTERN.findall(prompt):
        normalized = match.replace("\\", "/").lstrip("./")
        if normalized and normalized not in paths:
            paths.append(normalized)
    return tuple(paths)


def _move_path_pair(prompt: str, expected_paths: tuple[str, ...]) -> tuple[str, str] | None:
    """Resolve the paths attached to an explicit move clause, not unrelated reads."""
    if len(expected_paths) < 2 or not MOVE_PATTERN.search(prompt):
        return None
    normalized_prompt = prompt.replace("\\", "/")
    positions = {
        path: normalized_prompt.lower().find(path.replace("\\", "/").lower())
        for path in expected_paths
    }
    move_match = MOVE_PATTERN.search(normalized_prompt)
    assert move_match is not None
    before = [path for path in expected_paths if 0 <= positions[path] < move_match.start()]
    after = [path for path in expected_paths if positions[path] >= move_match.end()]
    if before and after:
        return max(before, key=lambda path: positions[path]), min(after, key=lambda path: positions[path])
    return expected_paths[-2], expected_paths[-1]


def _write_target_paths(prompt: str, expected_paths: tuple[str, ...]) -> tuple[str, ...]:
    """Return paths governed by a write verb while retaining read-only context paths."""
    if not expected_paths:
        return ()
    normalized_prompt = prompt.replace("\\", "/")
    targets: list[str] = []
    move_pair = _move_path_pair(prompt, expected_paths)
    if move_pair:
        targets.extend(move_pair)
    for path in expected_paths:
        normalized_path = path.replace("\\", "/")
        for occurrence in re.finditer(re.escape(normalized_path), normalized_prompt, re.I):
            clause_start = max(
                normalized_prompt.rfind(separator, 0, occurrence.start())
                for separator in ("；", ";", "。", "\n")
            ) + 1
            prefix = normalized_prompt[clause_start:occurrence.start()]
            if WRITE_PATTERN.search(PLAN_ONLY_PATTERN.sub("", NEGATED_WRITE_PATTERN.sub("", prefix))):
                targets.append(path)
                break
    return tuple(dict.fromkeys(targets))


def _blocked_reason(prompt: str, available_tools: set[str]) -> str | None:
    lowered_prompt = prompt.lower()
    normalized_tools = {name.lower() for name in available_tools}
    for pattern, reason in UNAVAILABLE_CAPABILITY_PATTERNS:
        if pattern.search(prompt):
            requested = {
                marker
                for marker, aliases in CAPABILITY_ALIASES
                if any(alias.lower() in lowered_prompt for alias in aliases)
            }
            if requested and any(marker in name for name in normalized_tools for marker in requested):
                return None
            if not requested and any(marker in name for name in normalized_tools for marker in ("hardware", "device")):
                return None
            return reason
    return None


def build_task_plan(task_id: str, prompt: str, available_tools: Iterable[str]) -> TaskPlan:
    tool_names = set(available_tools)
    expected_paths = _mentioned_paths(prompt)
    write_target_paths = _write_target_paths(prompt, expected_paths)
    positive_write_text = PLAN_ONLY_PATTERN.sub("", NEGATED_WRITE_PATTERN.sub("", prompt))
    requires_write = bool(WRITE_PATTERN.search(positive_write_text))
    code_change = requires_write and (
        any(Path(path).suffix.lower() in CODE_SUFFIXES for path in write_target_paths)
        or (not expected_paths and bool(CODE_TASK_PATTERN.search(prompt)))
    )
    requires_verification = bool(VERIFY_PATTERN.search(prompt)) or code_change
    workspace_task = bool(expected_paths or WORKSPACE_PATTERN.search(prompt) or requires_write or requires_verification)
    strict_scope = bool(STRICT_SCOPE_PATTERN.search(prompt))
    expects_failure = bool(EXPECTED_FAILURE_PATTERN.search(prompt))
    blocked_reason = _blocked_reason(prompt, tool_names)
    task_kind = "blocked" if blocked_reason else ("workspace_change" if requires_write else ("workspace_analysis" if workspace_task else "response"))

    steps: list[PlanStep] = []
    if workspace_task and not blocked_reason:
        inspect_tools = tuple(name for name in ("list_files", "list_directory", "search_files", "search_text", "read_file") if name in tool_names)
        steps.append(PlanStep("inspect", "读取相关文件并确认当前状态", tools=inspect_tools))
    if requires_write and not blocked_reason:
        if MOVE_PATTERN.search(prompt):
            candidates = ("create_directory", "move_file", "rename_file")
        elif DELETE_PATTERN.search(prompt):
            candidates = ("delete_file",)
        else:
            candidates = ("create_file", "write_file", "replace_text", "apply_patch", "copy_file", "create_directory")
        execute_tools = tuple(name for name in candidates if name in tool_names)
        steps.append(PlanStep("execute", "只实施任务要求的工作区变更", depends_on=("inspect",), tools=execute_tools, risk="medium"))
    if requires_verification and not blocked_reason:
        dependency = "execute" if requires_write else "inspect"
        verify_tools = (("run_command",) if "run_command" in tool_names else ())
        steps.append(PlanStep("verify", "运行与改动相关的测试、构建或检查", depends_on=(dependency,), tools=verify_tools, risk="critical"))
    steps.append(PlanStep("finalize", "等待独立 Verifier 根据证据判定终态", depends_on=((steps[-1].id,) if steps else ())))

    criteria: list[AcceptanceCriterion] = [
        AcceptanceCriterion("response_present", "向用户返回非空结果", "response_present"),
    ]
    if blocked_reason:
        criteria.append(AcceptanceCriterion("blocked_safely", "缺少必要能力时诚实阻塞且不产生副作用", "blocked_safely"))
    elif workspace_task:
        criteria.append(AcceptanceCriterion("workspace_evidence", "存在来自真实工作区工具的执行证据", "tool_evidence"))
    if requires_write and not blocked_reason:
        criteria.append(AcceptanceCriterion("changes_recorded", "请求的文件变更已记录且最终状态与写入结果一致", "changes_recorded"))
        deleting = bool(DELETE_PATTERN.search(prompt))
        move_pair = _move_path_pair(prompt, expected_paths)
        for index, path in enumerate(write_target_paths):
            expected_exists = not deleting
            if move_pair:
                expected_exists = path != move_pair[0]
            criteria.append(
                AcceptanceCriterion(
                    f"path_state_{index + 1}",
                    f"{path} 的最终存在状态符合任务要求",
                    "path_state",
                    parameters={"path": path, "exists": expected_exists},
                )
            )
    if requires_verification and not blocked_reason:
        criteria.append(AcceptanceCriterion("verification_command", "至少一条真实测试、构建或检查命令成功", "verification_command"))
        for domain in detect_verifier_domains(prompt, expected_paths):
            criteria.append(
                AcceptanceCriterion(
                    f"verify_{domain}",
                    f"{domain} 任务要求有对应的专用验证证据",
                    "domain_verification",
                    required=domain != "multimodal",
                    parameters={"domain": domain},
                )
            )
    elif requires_write and not blocked_reason and "document" in detect_verifier_domains(prompt, expected_paths):
        criteria.append(
            AcceptanceCriterion(
                "verify_document",
                "文档任务要求产物存在且非空",
                "domain_verification",
                parameters={"domain": "document"},
            )
        )
    if strict_scope:
        criteria.append(AcceptanceCriterion("scope_control", "没有修改任务范围外的文件", "scope_control", parameters={"paths": list(write_target_paths or expected_paths)}))
    if workspace_task and not blocked_reason:
        criteria.append(AcceptanceCriterion("failures_resolved", "没有未处理的工具或验证失败", "failures_resolved"))

    return TaskPlan(
        task_id=task_id,
        goal=prompt.strip(),
        task_kind=task_kind,
        steps=tuple(steps),
        acceptance_criteria=tuple(criteria),
        expected_paths=expected_paths,
        strict_scope=strict_scope,
        expects_failure_handling=expects_failure,
        blocked_reason=blocked_reason,
        constraints=("文件访问不得超出用户选择的工作区", "不得绕过权限确认或修改安全策略"),
        expected_changes=write_target_paths if requires_write else (),
        forbidden_changes=(("工作区范围外的任何文件",) if workspace_task else ()),
        verification_commands=(("运行与改动匹配的项目测试或构建",) if requires_verification and not blocked_reason else ()),
        required_capabilities=_required_capabilities(requires_write, requires_verification),
        risk=_plan_risk(requires_write, requires_verification, prompt),
        requires_user_input=bool(blocked_reason),
    )


def _required_capabilities(requires_write: bool, requires_verification: bool) -> tuple[str, ...]:
    capabilities = ["workspace.read"]
    if requires_write:
        capabilities.append("workspace.write")
    if requires_verification:
        capabilities.append("command.execute")
    return tuple(capabilities)


def _plan_risk(requires_write: bool, requires_verification: bool, prompt: str) -> str:
    if DELETE_PATTERN.search(prompt) or MOVE_PATTERN.search(prompt):
        return "high"
    if requires_write or requires_verification:
        return "medium"
    return "low"


class PlanValidationError(ValueError):
    pass


def _text_items(value: Any, *, limit: int = 20, item_limit: int = 500) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value[:limit]:
        if isinstance(item, str) and item.strip():
            text = item.strip()[:item_limit]
            if text not in result:
                result.append(text)
    return tuple(result)


def _safe_relative_paths(value: Any) -> tuple[str, ...]:
    paths: list[str] = []
    for item in _text_items(value, limit=50, item_limit=500):
        candidate = item.replace("\\", "/")
        if candidate.startswith(("/", "../")) or re.match(r"^[A-Za-z]:/", candidate):
            continue
        if candidate.startswith("./"):
            candidate = candidate[2:]
        path = Path(candidate)
        if not candidate or path.is_absolute() or ".." in path.parts:
            continue
        if candidate not in paths:
            paths.append(candidate)
    return tuple(paths)


def _semantic_steps(value: Any, available_tools: set[str]) -> tuple[PlanStep, ...]:
    if not isinstance(value, list):
        return ()
    steps: list[PlanStep] = []
    seen: set[str] = set()
    for index, item in enumerate(value[:20], start=1):
        if not isinstance(item, Mapping):
            continue
        raw_id = str(item.get("id") or f"semantic_{index}").strip().lower()
        step_id = re.sub(r"[^a-z0-9_-]", "_", raw_id)[:40] or f"semantic_{index}"
        if step_id in seen:
            step_id = f"{step_id}_{index}"
        description = str(item.get("description") or "").strip()[:500]
        if not description:
            continue
        dependencies = tuple(dep for dep in _text_items(item.get("depends_on"), limit=10, item_limit=40) if dep in seen)
        tools = tuple(name for name in _text_items(item.get("tools"), limit=20, item_limit=100) if name in available_tools)
        risk = str(item.get("risk") or "low").lower()
        if risk not in {"low", "medium", "high", "critical"}:
            risk = "low"
        steps.append(PlanStep(step_id, description, dependencies, tools, risk))
        seen.add(step_id)
    return tuple(steps)


def guard_task_contract(
    baseline: TaskPlan,
    proposal: Mapping[str, Any],
    available_tools: Iterable[str],
    *,
    interaction_mode: str,
    data_location: str,
    privacy_scope: str,
    budget_limit: int,
    preferred_model: str,
    memory_write_policy: str,
) -> TaskPlan:
    """Turn an untrusted model proposal into a bounded executable contract."""
    tool_names = set(available_tools)
    baseline_tools = {tool for step in baseline.steps for tool in step.tools}
    semantic_tool_scope = tool_names if "workspace.write" in baseline.required_capabilities else baseline_tools
    semantic_steps = _semantic_steps(proposal.get("steps"), semantic_tool_scope)
    steps = semantic_steps or baseline.steps
    if semantic_steps:
        existing_ids = {step.id for step in semantic_steps}
        steps = (*semantic_steps, *(step for step in baseline.steps if step.id not in existing_ids))
    if baseline.blocked_reason:
        steps = baseline.steps

    semantic_changes = _safe_relative_paths(proposal.get("expected_changes")) if "workspace.write" in baseline.required_capabilities else ()
    expected_changes = tuple(dict.fromkeys((*baseline.expected_changes, *semantic_changes)))
    proposed_risk = str(proposal.get("risk") or baseline.risk).lower()
    risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    if proposed_risk not in risk_order:
        proposed_risk = baseline.risk
    risk = max((baseline.risk, proposed_risk), key=lambda item: risk_order[item])

    semantic_criteria = _text_items(proposal.get("acceptance_criteria"), limit=20)
    criteria = list(baseline.acceptance_criteria)
    existing_domains = {item.parameters.get("domain") for item in criteria if item.kind == "domain_verification"}
    for domain in detect_verifier_domains(baseline.goal, expected_changes):
        if domain in existing_domains or (domain != "document" and "command.execute" not in baseline.required_capabilities):
            continue
        criteria.append(
            AcceptanceCriterion(
                f"verify_{domain}",
                f"{domain} 任务要求有对应的专用验证证据",
                "domain_verification",
                required=domain != "multimodal",
                parameters={"domain": domain},
            )
        )
    for index, description in enumerate(semantic_criteria, start=1):
        if any(item.description == description for item in criteria):
            continue
        criteria.append(AcceptanceCriterion(f"semantic_{index}", description, "semantic_requirement", required=False))

    constraints = tuple(dict.fromkeys((*baseline.constraints, *_text_items(proposal.get("constraints")))))
    forbidden = tuple(dict.fromkeys((*baseline.forbidden_changes, *_text_items(proposal.get("forbidden_changes")))))
    policy_decisions = (
        "权限模式、工作区和安全策略来自可信运行时，模型提案无权修改",
        f"数据位置固定为 {data_location}，执行器固定为 local_windows",
        f"任务预算限制为 {max(1, budget_limit)} tokens",
    )
    plan = TaskPlan(
        task_id=baseline.task_id,
        goal=baseline.goal,
        task_kind=baseline.task_kind,
        steps=steps,
        acceptance_criteria=tuple(criteria),
        expected_paths=baseline.expected_paths,
        strict_scope=baseline.strict_scope,
        expects_failure_handling=baseline.expects_failure_handling,
        blocked_reason=baseline.blocked_reason,
        assumptions=_text_items(proposal.get("assumptions")),
        constraints=constraints,
        expected_changes=expected_changes,
        forbidden_changes=forbidden,
        verification_commands=_text_items(proposal.get("verification_commands"), limit=20),
        required_capabilities=baseline.required_capabilities,
        preferred_executor="local_windows",
        risk=risk,
        requires_user_input=bool(proposal.get("requires_user_input")) or baseline.requires_user_input,
        interaction_mode=interaction_mode,
        data_location=data_location,
        privacy_scope=privacy_scope,
        budget_limit=max(1, budget_limit),
        preferred_model=preferred_model,
        memory_write_policy=memory_write_policy,
        planner_source="semantic_model",
        policy_decisions=policy_decisions,
    )
    validate_task_contract(plan, tool_names)
    return plan


def validate_task_contract(plan: TaskPlan, available_tools: Iterable[str]) -> None:
    if plan.schema_version != "1.0" or not plan.goal.strip():
        raise PlanValidationError("任务合同版本或目标无效")
    if plan.interaction_mode not in {"conversation", "copilot", "agent"}:
        raise PlanValidationError("交互模式无效")
    if plan.data_location not in {"local_workspace", "uploaded_file", "remote_service"}:
        raise PlanValidationError("数据位置无效")
    if plan.privacy_scope not in {"workspace", "private", "remote_allowed"}:
        raise PlanValidationError("隐私范围无效")
    if plan.memory_write_policy not in {"deny", "explicit", "allow"}:
        raise PlanValidationError("记忆写入策略无效")
    if plan.preferred_executor != "local_windows" or plan.budget_limit < 1:
        raise PlanValidationError("执行器或预算无效")
    step_ids = {step.id for step in plan.steps}
    if len(step_ids) != len(plan.steps):
        raise PlanValidationError("计划步骤 ID 重复")
    allowed = set(available_tools)
    for step in plan.steps:
        if any(dependency not in step_ids for dependency in step.depends_on):
            raise PlanValidationError("计划步骤依赖不存在")
        if any(tool not in allowed for tool in step.tools):
            raise PlanValidationError("计划请求了未授权工具")
    for path in plan.expected_changes:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise PlanValidationError("计划试图扩大工作区")


def save_task_plan(plan: TaskPlan) -> None:
    stamp = now_iso()
    payload = json.dumps(plan.as_dict(), ensure_ascii=False)
    criteria = json.dumps([asdict(item) for item in plan.acceptance_criteria], ensure_ascii=False)
    with connect() as db:
        db.execute(
            "INSERT INTO task_plans(task_id, status, plan, acceptance_criteria, created_at, updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, plan=excluded.plan, acceptance_criteria=excluded.acceptance_criteria, updated_at=excluded.updated_at",
            (plan.task_id, "planned", payload, criteria, stamp, stamp),
        )


def load_task_plan(task_id: str) -> TaskPlan | None:
    records = rows("SELECT plan FROM task_plans WHERE task_id=?", (task_id,))
    if not records:
        return None
    payload = json.loads(records[0]["plan"])
    return TaskPlan(
        task_id=payload["task_id"],
        goal=payload["goal"],
        task_kind=payload["task_kind"],
        steps=tuple(PlanStep(**{**item, "depends_on": tuple(item.get("depends_on") or ()), "tools": tuple(item.get("tools") or ())}) for item in payload["steps"]),
        acceptance_criteria=tuple(AcceptanceCriterion(**item) for item in payload["acceptance_criteria"]),
        expected_paths=tuple(payload.get("expected_paths") or ()),
        strict_scope=bool(payload.get("strict_scope")),
        expects_failure_handling=bool(payload.get("expects_failure_handling")),
        blocked_reason=payload.get("blocked_reason"),
        assumptions=tuple(payload.get("assumptions") or ()),
        constraints=tuple(payload.get("constraints") or ()),
        expected_changes=tuple(payload.get("expected_changes") or ()),
        forbidden_changes=tuple(payload.get("forbidden_changes") or ()),
        verification_commands=tuple(payload.get("verification_commands") or ()),
        required_capabilities=tuple(payload.get("required_capabilities") or ()),
        preferred_executor=str(payload.get("preferred_executor") or "local_windows"),
        risk=str(payload.get("risk") or "low"),
        requires_user_input=bool(payload.get("requires_user_input")),
        interaction_mode=str(payload.get("interaction_mode") or "agent"),
        data_location=str(payload.get("data_location") or "local_workspace"),
        privacy_scope=str(payload.get("privacy_scope") or "workspace"),
        budget_limit=int(payload.get("budget_limit") or 0),
        preferred_model=str(payload.get("preferred_model") or ""),
        memory_write_policy=str(payload.get("memory_write_policy") or "explicit"),
        planner_source=str(payload.get("planner_source") or "deterministic"),
        schema_version=str(payload.get("schema_version") or "1.0"),
        policy_decisions=tuple(payload.get("policy_decisions") or ()),
    )


def mark_plan_status(task_id: str, status: str) -> None:
    with connect() as db:
        db.execute("UPDATE task_plans SET status=?, updated_at=? WHERE task_id=?", (status, now_iso(), task_id))


def executor_brief(plan: TaskPlan) -> str:
    steps = "\n".join(f"- {step.id}: {step.description}" for step in plan.steps)
    criteria = "\n".join(f"- {item.id}: {item.description}" for item in plan.acceptance_criteria)
    boundaries = "\n".join(f"- {item}" for item in (*plan.constraints, *plan.policy_decisions))
    return (
        "Planner 已生成以下执行计划。Executor 只能执行计划，不能决定 completed；最终状态由独立 Verifier 决定。\n"
        f"合同版本：{plan.schema_version}；来源：{plan.planner_source}；风险：{plan.risk}；"
        f"执行器：{plan.preferred_executor}；数据位置：{plan.data_location}；隐私范围：{plan.privacy_scope}；"
        f"预算：{plan.budget_limit or '运行时默认'} tokens；记忆写入：{plan.memory_write_policy}。\n"
        f"计划：\n{steps}\n验收条件：\n{criteria}\n不可变边界：\n{boundaries or '- 运行时安全策略'}"
    )
