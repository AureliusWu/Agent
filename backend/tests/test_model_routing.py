from app.model_routing import classify_task, escalate_route, estimate_cost_usd, route_for_phase


def test_task_classification_uses_three_configurable_tiers(monkeypatch) -> None:
    monkeypatch.setattr("app.model_routing.settings.model_light_name", "light-model")
    monkeypatch.setattr("app.model_routing.settings.model_medium_name", "medium-model")
    monkeypatch.setattr("app.model_routing.settings.model_strong_name", "strong-model")

    light = classify_task("总结并分类这些文件")
    medium = classify_task("修改代码并运行测试")
    strong = classify_task("全面审计架构并定位复杂并发问题")

    assert (light.tier, light.model) == ("light", "light-model")
    assert (medium.tier, medium.model) == ("medium", "medium-model")
    assert (strong.tier, strong.model) == ("strong", "strong-model")


def test_route_escalates_after_failure_and_repair() -> None:
    initial = classify_task("总结文件")
    after_failure = route_for_phase(initial, "analysis", failures=1)
    after_repair = route_for_phase(after_failure, "repair", repair_attempt=1)

    assert initial.tier == "light"
    assert after_failure.tier == "medium"
    assert after_repair.tier == "strong"
    assert escalate_route(after_repair, "already strongest") == after_repair


def test_cost_estimate_requires_explicit_pricing(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.model_routing.settings.model_pricing_json",
        '{"priced-model":{"input":1.0,"output":2.0}}',
    )

    assert estimate_cost_usd("priced-model", 1_000_000, 500_000) == 2.0
    assert estimate_cost_usd("unknown-model", 1_000_000, 500_000) == 0.0
