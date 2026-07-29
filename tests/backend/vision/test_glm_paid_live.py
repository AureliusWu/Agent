from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.config import settings
from app.providers.registry import assert_paid_api_allowed
from app.vision.providers import OpenAICompatibleVisionProvider
from app.vision.service import VisionService


pytestmark = [
    pytest.mark.paid_model,
    pytest.mark.skipif(
        os.getenv("SIYI_TEST_PROVIDER") != "glm-vision"
        or os.getenv("SIYI_ALLOW_PAID_API") != "true"
        or not os.getenv("SIYI_VISION_TEST_API_KEY"),
        reason="requires explicit paid GLM vision acceptance permission and a process-scoped key",
    ),
]


def _provider() -> OpenAICompatibleVisionProvider:
    return OpenAICompatibleVisionProvider(
        provider_id="glm-vision-live",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-4.6v",
        api_key=os.environ["SIYI_VISION_TEST_API_KEY"],
        local=False,
    )


def _text(result: dict) -> str:
    return json.dumps(result.get("result") or {}, ensure_ascii=False).lower()


def _has_any(value: str, candidates: tuple[str, ...]) -> bool:
    return any(candidate.lower() in value for candidate in candidates)


def _create_fixtures(workspace: Path) -> None:
    canvas = Image.new("RGB", (900, 520), "white")
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((80, 100, 330, 350), fill="#e53935")
    draw.ellipse((500, 100, 750, 350), fill="#1e88e5")
    draw.text((310, 430), "VISION 42", fill="black")
    canvas.save(workspace / "scene.png")

    before = Image.new("RGB", (720, 360), "white")
    draw = ImageDraw.Draw(before)
    draw.rectangle((80, 80, 280, 280), fill="#e53935")
    draw.text((335, 165), "BEFORE", fill="black")
    before.save(workspace / "before.png")

    after = Image.new("RGB", (720, 360), "white")
    draw = ImageDraw.Draw(after)
    draw.ellipse((80, 80, 280, 280), fill="#43a047")
    draw.text((335, 165), "AFTER", fill="black")
    after.save(workspace / "after.png")

    chart = Image.new("RGB", (900, 600), "white")
    draw = ImageDraw.Draw(chart)
    draw.text((330, 30), "QUARTERLY SALES", fill="black")
    draw.line((100, 500, 820, 500), fill="black", width=3)
    for x, label, value, height in (
        (180, "A 10", 10, 140),
        (410, "B 30", 30, 360),
        (640, "C 20", 20, 250),
    ):
        draw.rectangle((x, 500 - height, x + 120, 500), fill="#3949ab")
        draw.text((x + 30, 520), label, fill="black")
        draw.text((x + 48, 470 - height), str(value), fill="black")
    chart.save(workspace / "chart.png")

    ui = Image.new("RGB", (1000, 620), "#f5f5f5")
    draw = ImageDraw.Draw(ui)
    draw.rectangle((0, 0, 1000, 72), fill="#263238")
    draw.text((35, 25), "SYSTEM DASHBOARD", fill="white")
    draw.rectangle((80, 130, 920, 270), fill="#ffcdd2", outline="#c62828", width=4)
    draw.text((150, 175), "ERROR 503 - SERVICE UNAVAILABLE", fill="#b71c1c")
    draw.rectangle((385, 360, 615, 440), fill="#1976d2")
    draw.text((460, 390), "RETRY", fill="white")
    ui.save(workspace / "ui.png")


def test_glm_vision_real_acceptance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert_paid_api_allowed("glm-vision")
    monkeypatch.setattr(settings, "model_max_tokens", 1024)
    _create_fixtures(tmp_path)
    provider = _provider()
    service = VisionService()
    selected_case = os.getenv("SIYI_VISION_LIVE_CASE", "all").strip().lower()
    if selected_case not in {"all", "describe", "compare", "chart", "ui"}:
        pytest.fail(f"unknown SIYI_VISION_LIVE_CASE: {selected_case}")

    async def run() -> dict[str, dict]:
        results: dict[str, dict] = {}
        if selected_case in {"all", "describe"}:
            results["describe"] = await service.analyze(
                workspace=str(tmp_path),
                paths=["scene.png"],
                action="describe",
                prompt="请准确报告颜色、形状和可见文字。",
                provider_override=provider,
                task_id="v960-glm-describe",
            )
        if selected_case in {"all", "compare"}:
            results["compare"] = await service.analyze(
                workspace=str(tmp_path),
                paths=["before.png", "after.png"],
                action="compare",
                prompt="请指出颜色、形状和文字发生的变化。",
                provider_override=provider,
                task_id="v960-glm-compare",
            )
        if selected_case in {"all", "chart"}:
            results["chart"] = await service.analyze(
                workspace=str(tmp_path),
                paths=["chart.png"],
                action="analyze_chart",
                prompt="请读取 A、B、C 的值并说明趋势。",
                provider_override=provider,
                task_id="v960-glm-chart",
            )
        if selected_case in {"all", "ui"}:
            results["ui"] = await service.analyze(
                workspace=str(tmp_path),
                paths=["ui.png"],
                action="inspect_ui",
                prompt="请报告错误码、错误文字和可操作按钮。",
                provider_override=provider,
                task_id="v960-glm-ui",
            )
        return results

    results = asyncio.run(run())
    assert all(item["status"] == "ok" for item in results.values())

    if "describe" in results:
        describe_text = _text(results["describe"])
        assert _has_any(describe_text, ("red", "红"))
        assert _has_any(describe_text, ("blue", "蓝"))
        assert _has_any(describe_text, ("42",))

    if "compare" in results:
        compare_text = _text(results["compare"])
        assert _has_any(compare_text, ("red", "红"))
        assert _has_any(compare_text, ("green", "绿"))
        assert _has_any(compare_text, ("before", "after", "变化", "差异"))

    if "chart" in results:
        chart_text = _text(results["chart"])
        assert all(value in chart_text for value in ("10", "30", "20"))
        assert _has_any(chart_text, ("上升", "increase"))
        assert _has_any(chart_text, ("下降", "decrease"))

    if "ui" in results:
        ui_text = _text(results["ui"])
        assert "503" in ui_text
        assert _has_any(ui_text, ("service unavailable", "服务不可用"))
        assert _has_any(ui_text, ("retry", "重试"))
