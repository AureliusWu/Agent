from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .database import connect, now_iso, rows


FILE_PATTERN = re.compile(
    r"(?<![\w.-])([\w./\\-]+\.(?:py|js|jsx|ts|tsx|rs|go|java|cs|cpp|c|h|vue|svelte|json|toml|ya?ml|ini|md|txt|css|html))(?![\w.-])",
    re.I,
)
WRITE_PATTERN = re.compile(
    r"(?:创建|新增|添加|修改|修复|调整|优化|写入|删除|移除|移动|重命名|替换|保存|"
    r"\b(?:create|add|modify|update|fix|change|write|delete|remove|move|rename|replace|save)\b)",
    re.I,
)
NEGATED_WRITE_PATTERN = re.compile(
    r"(?:不要|不得|禁止|无需|不应|避免).{0,8}(?:修改|改动|创建|新增|添加|写入|删除|移除|移动|重命名|替换)|"
    r"\b(?:do not|don't|must not|without)\b.{0,20}\b(?:modify|change|create|write|delete|remove|move|rename)\b",
    re.I,
)
DELETE_PATTERN = re.compile(r"(?:删除|移除|\b(?:delete|remove)\b)", re.I)
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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mentioned_paths(prompt: str) -> tuple[str, ...]:
    paths: list[str] = []
    for match in FILE_PATTERN.findall(prompt):
        normalized = match.replace("\\", "/").lstrip("./")
        if normalized and normalized not in paths:
            paths.append(normalized)
    return tuple(paths)


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
    positive_write_text = NEGATED_WRITE_PATTERN.sub("", prompt)
    requires_write = bool(WRITE_PATTERN.search(positive_write_text))
    code_change = requires_write and (bool(CODE_TASK_PATTERN.search(prompt)) or any(Path(path).suffix.lower() in CODE_SUFFIXES for path in expected_paths))
    requires_verification = bool(VERIFY_PATTERN.search(prompt)) or code_change
    workspace_task = bool(expected_paths or WORKSPACE_PATTERN.search(prompt) or requires_write or requires_verification)
    strict_scope = bool(STRICT_SCOPE_PATTERN.search(prompt))
    expects_failure = bool(EXPECTED_FAILURE_PATTERN.search(prompt))
    blocked_reason = _blocked_reason(prompt, tool_names)
    task_kind = "blocked" if blocked_reason else ("workspace_change" if requires_write else ("workspace_analysis" if workspace_task else "response"))

    steps: list[PlanStep] = []
    if workspace_task and not blocked_reason:
        steps.append(PlanStep("inspect", "读取相关文件并确认当前状态", tools=("list_files", "search_files", "read_file")))
    if requires_write and not blocked_reason:
        steps.append(PlanStep("execute", "只实施任务要求的工作区变更", depends_on=("inspect",), tools=("write_file", "replace_text", "apply_patch"), risk="medium"))
    if requires_verification and not blocked_reason:
        dependency = "execute" if requires_write else "inspect"
        steps.append(PlanStep("verify", "运行与改动相关的测试、构建或检查", depends_on=(dependency,), tools=("run_command",), risk="critical"))
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
        for index, path in enumerate(expected_paths):
            criteria.append(
                AcceptanceCriterion(
                    f"path_state_{index + 1}",
                    f"{path} 的最终存在状态符合任务要求",
                    "path_state",
                    parameters={"path": path, "exists": not deleting},
                )
            )
    if requires_verification and not blocked_reason:
        criteria.append(AcceptanceCriterion("verification_command", "至少一条真实测试、构建或检查命令成功", "verification_command"))
    if strict_scope:
        criteria.append(AcceptanceCriterion("scope_control", "没有修改任务范围外的文件", "scope_control", parameters={"paths": list(expected_paths)}))
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
    )


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
    )


def mark_plan_status(task_id: str, status: str) -> None:
    with connect() as db:
        db.execute("UPDATE task_plans SET status=?, updated_at=? WHERE task_id=?", (status, now_iso(), task_id))


def executor_brief(plan: TaskPlan) -> str:
    steps = "\n".join(f"- {step.id}: {step.description}" for step in plan.steps)
    criteria = "\n".join(f"- {item.id}: {item.description}" for item in plan.acceptance_criteria)
    return (
        "Planner 已生成以下执行计划。Executor 只能执行计划，不能决定 completed；最终状态由独立 Verifier 决定。\n"
        f"计划：\n{steps}\n验收条件：\n{criteria}"
    )
