from __future__ import annotations

from dataclasses import dataclass

from .config import settings


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
    return ModelRoute(
        tier=tier,
        model=model_for_tier(tier),
        task_type=task_type,
        confidence=confidence,
        reason=reason,
        max_output_tokens=max_output_tokens_for_tier(tier),
    )


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
        "models": settings.model_routes,
        "max_output_tokens": {
            tier: max_output_tokens_for_tier(tier)
            for tier in ROUTE_TIERS
        },
        "low_confidence_threshold": settings.model_low_confidence_threshold,
        "pricing_models": sorted(settings.model_pricing),
    }
