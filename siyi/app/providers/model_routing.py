from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace

from app.config import settings
from app.database import rows
from app.providers.capabilities import provider_capability_matrix


ROUTE_TIERS = ("light", "medium", "strong")
LIGHT_MARKERS = (
    "分类",
    "整理",
    "摘要",
    "总结",
    "翻译",
    "重命名",
    "列出",
    "查找",
    "搜索",
    "读取",
    "inspect",
    "list",
    "search",
    "summarize",
    "classify",
)
MEDIUM_MARKERS = (
    "修改",
    "创建",
    "实现",
    "增加",
    "修复",
    "测试",
    "构建",
    "refactor",
    "implement",
    "update",
    "fix",
    "test",
    "build",
)
STRONG_MARKERS = (
    "架构",
    "全面审计",
    "复杂",
    "根本原因",
    "迁移",
    "安全",
    "性能回归",
    "并发",
    "生产事故",
    "architecture",
    "security",
    "migration",
    "root cause",
    "race condition",
    "incident",
)


@dataclass(frozen=True)
class ModelRoute:
    tier: str
    model: str
    task_type: str
    confidence: float
    reason: str
    max_output_tokens: int


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def model_for_tier(tier: str) -> str:
    return settings.model_routes.get(tier, settings.model_name)


def max_output_tokens_for_tier(tier: str) -> int:
    configured = {
        "light": settings.model_light_max_tokens,
        "medium": settings.model_medium_max_tokens,
        "strong": settings.model_strong_max_tokens,
    }.get(tier, settings.model_max_tokens)
    return min(configured, settings.model_max_tokens)


def classify_task(prompt: str) -> ModelRoute:
    text = prompt.lower()
    if not settings.model_routing_enabled:
        return ModelRoute(
            tier="strong",
            model=settings.model_name,
            task_type="fixed",
            confidence=1.0,
            reason="模型路由已关闭",
            max_output_tokens=settings.model_max_tokens,
        )
    if _contains_any(text, STRONG_MARKERS):
        tier, task_type, confidence, reason = "strong", "complex_reasoning", 0.9, "命中复杂推理或高风险任务"
    elif _contains_any(text, MEDIUM_MARKERS):
        tier, task_type, confidence, reason = "medium", "implementation", 0.82, "命中代码实现或修改任务"
    elif _contains_any(text, LIGHT_MARKERS):
        tier, task_type, confidence, reason = "light", "lightweight", 0.82, "命中分类、摘要或只读任务"
    else:
        tier, task_type, confidence, reason = "medium", "general", 0.5, "任务类型不明确，使用稳健默认档"
    route = ModelRoute(
        tier=tier,
        model=model_for_tier(tier),
        task_type=task_type,
        confidence=confidence,
        reason=reason,
        max_output_tokens=max_output_tokens_for_tier(tier),
    )
    return route_from_observations(route, requires_tools=_contains_any(text, MEDIUM_MARKERS + STRONG_MARKERS))


def model_performance(model: str, *, limit: int = 100) -> dict[str, float | int]:
    records = rows(
        "SELECT success,duration_ms,estimated_cost_usd FROM model_runs WHERE model=? ORDER BY id DESC LIMIT ?",
        (model, max(1, min(limit, 1000))),
    )
    samples = len(records)
    return {
        "samples": samples,
        "success_rate": round(sum(int(item.get("success") or 0) for item in records) / samples, 3) if samples else 0.0,
        "average_latency_ms": round(sum(int(item.get("duration_ms") or 0) for item in records) / samples) if samples else 0,
        "average_cost_usd": round(sum(float(item.get("estimated_cost_usd") or 0) for item in records) / samples, 8) if samples else 0.0,
    }


def route_from_observations(route: ModelRoute, *, requires_tools: bool = False) -> ModelRoute:
    if not settings.model_data_routing_enabled:
        return route
    performance = model_performance(route.model)
    capabilities = provider_capability_matrix(model=route.model)
    tool_unsupported = requires_tools and capabilities["capabilities"]["native_tool_calls"] == "unsupported"
    low_reliability = int(performance["samples"]) >= settings.model_min_observation_samples and float(performance["success_rate"]) < settings.model_min_success_rate
    if (tool_unsupported or low_reliability) and route.tier != "strong":
        reason = "模型未通过原生工具调用能力验证" if tool_unsupported else f"近期成功率 {float(performance['success_rate']):.0%} 低于门槛"
        return escalate_route(route, reason)
    return route


def apply_manual_override(route: ModelRoute, *, preferred_model: str | None = None, reasoning_effort: str = "auto") -> ModelRoute:
    effort_tier = {"low": "light", "medium": "medium", "high": "strong"}.get(reasoning_effort)
    result = route_for_tier(effort_tier, task_type=route.task_type, confidence=1.0, reason=f"用户手动选择 {reasoning_effort} 推理强度") if effort_tier else route
    if preferred_model:
        result = replace(result, model=preferred_model.strip(), reason=f"用户手动指定模型 {preferred_model.strip()}", confidence=1.0)
    return result


def route_for_tier(tier: str, *, task_type: str, confidence: float, reason: str) -> ModelRoute:
    normalized = tier if tier in ROUTE_TIERS else "medium"
    return ModelRoute(
        tier=normalized,
        model=model_for_tier(normalized),
        task_type=task_type,
        confidence=max(0.0, min(confidence, 1.0)),
        reason=reason,
        max_output_tokens=max_output_tokens_for_tier(normalized),
    )


def escalate_route(route: ModelRoute, reason: str) -> ModelRoute:
    if not settings.model_escalation_enabled:
        return route
    index = ROUTE_TIERS.index(route.tier) if route.tier in ROUTE_TIERS else 1
    if index >= len(ROUTE_TIERS) - 1:
        return route
    target = ROUTE_TIERS[index + 1]
    return route_for_tier(
        target,
        task_type=route.task_type,
        confidence=max(route.confidence, 0.7),
        reason=f"{route.tier} 升级到 {target}：{reason}",
    )


def route_for_phase(route: ModelRoute, phase: str, *, failures: int = 0, repair_attempt: int = 0) -> ModelRoute:
    result = route
    if route.confidence < settings.model_low_confidence_threshold and route.tier == "light":
        result = escalate_route(result, "任务分类置信度不足")
    if failures > 0:
        result = escalate_route(result, "工具或模型调用失败")
    if repair_attempt > 0 or phase == "repair":
        result = escalate_route(result, "独立验证要求返工")
    return result


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    prices = settings.model_pricing.get(model)
    if not prices:
        return 0.0
    cost = (max(prompt_tokens, 0) * prices["input"] + max(completion_tokens, 0) * prices["output"]) / 1_000_000
    return round(cost, 8)


def routing_policy() -> dict[str, object]:
    return {
        "enabled": settings.model_routing_enabled,
        "escalation_enabled": settings.model_escalation_enabled,
        "data_routing_enabled": settings.model_data_routing_enabled,
        "models": settings.model_routes,
        "max_output_tokens": {
            tier: max_output_tokens_for_tier(tier)
            for tier in ROUTE_TIERS
        },
        "low_confidence_threshold": settings.model_low_confidence_threshold,
        "pricing_models": sorted(settings.model_pricing),
        "model_performance": {model: model_performance(model) for model in sorted(set(settings.model_routes.values()))},
    }
