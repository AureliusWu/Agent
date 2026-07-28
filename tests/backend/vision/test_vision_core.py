from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.config import settings
from app.providers.base import LLMProvider, ProviderCapabilities
from app.tools.registry import REGISTRY, select_model_tools
from app.tools.runtime_tools import execute_runtime_tool
from app.vision.images import ImageValidationError, prepare_workspace_image
from app.vision.providers import OpenAICompatibleVisionProvider
from app.vision.service import VisionError, VisionService


class CapturingVisionProvider(LLMProvider):
    id = "capturing-vision"
    name = "Capturing Vision"
    model = "test-vision"

    def __init__(self, *, vision: bool = True) -> None:
        self.vision_enabled = vision
        self.messages: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:
        self.messages = messages
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "observations": ["可见矩形与文字"],
                    "inferences": ["可能是界面截图"],
                    "uncertainties": ["小字不可确认"],
                    "confidence": 0.72,
                },
                ensure_ascii=False,
            ),
        }

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            streaming=False,
            native_tool_calls=False,
            structured_output=False,
            vision=self.vision_enabled,
            reasoning=False,
            json_mode=False,
            embeddings=False,
            context_window=16_384,
            default_max_output_tokens=2048,
            source="test",
        )

    async def health_check(self, api_key: str | None = None) -> dict[str, Any]:
        del api_key
        return {"status": "ok"}

    def profile(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "models": [self.model]}


def _save_image(path: Path, size: tuple[int, int] = (640, 480), *, metadata: bool = True) -> None:
    image = Image.new("RGB", size, color=(245, 245, 245))
    exif = Image.Exif()
    if metadata:
        exif[0x010E] = "private description"
        exif[0x013B] = "private author"
    image.save(path, format="JPEG", quality=90, exif=exif)


def _decoded_parts(prepared) -> list[bytes]:
    return [
        base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
        for part in prepared.parts
    ]


def test_preprocessing_removes_metadata_and_never_exposes_source_bytes(tmp_path: Path) -> None:
    source = tmp_path / "private-photo.jpg"
    _save_image(source)

    prepared = prepare_workspace_image(str(tmp_path), source.name, "image-1")

    assert prepared.preprocessing["metadata_removed"] is True
    assert prepared.sha256
    for content in _decoded_parts(prepared):
        with Image.open(io.BytesIO(content)) as cleaned:
            assert cleaned.format == "JPEG"
            assert dict(cleaned.getexif()) == {}
            assert "private description" not in repr(cleaned.info)


def test_large_image_generates_bounded_overview_and_tiles(tmp_path: Path) -> None:
    source = tmp_path / "large-chart.png"
    Image.new("RGB", (3300, 900), color=(255, 255, 255)).save(source, format="PNG")

    prepared = prepare_workspace_image(str(tmp_path), source.name, "chart")

    assert prepared.preprocessing["tiled"] is True
    assert 1 <= prepared.preprocessing["tile_count"] <= 6
    assert len(prepared.parts) == prepared.preprocessing["tile_count"] + 1
    assert prepared.preprocessing["overview_size"][0] <= 2048


def test_invalid_signature_and_animation_are_rejected(tmp_path: Path) -> None:
    invalid = tmp_path / "fake.png"
    invalid.write_bytes(b"not an image")
    with pytest.raises(ImageValidationError):
        prepare_workspace_image(str(tmp_path), invalid.name, "invalid")

    animation = tmp_path / "animation.webp"
    frames = [Image.new("RGB", (20, 20), color=color) for color in ("red", "blue")]
    frames[0].save(animation, format="WEBP", save_all=True, append_images=frames[1:], duration=100)
    with pytest.raises(ImageValidationError, match="多帧"):
        prepare_workspace_image(str(tmp_path), animation.name, "animation")


def test_multi_image_request_has_a_total_pixel_limit(tmp_path: Path, monkeypatch) -> None:
    for name in ("one.png", "two.png"):
        Image.new("RGB", (800, 800), color="white").save(tmp_path / name, format="PNG")
    monkeypatch.setattr(settings, "vision_max_total_pixels", 1_000_000)

    with pytest.raises(VisionError, match="总像素"):
        asyncio.run(
            VisionService().analyze(
                workspace=str(tmp_path),
                paths=["one.png", "two.png"],
                action="compare",
                provider_override=CapturingVisionProvider(),
            )
        )


@pytest.mark.parametrize(
    "action",
    ["describe", "extract_text", "analyze_chart", "inspect_ui", "classify"],
)
def test_single_image_actions_use_clean_provider_neutral_contract(tmp_path: Path, action: str) -> None:
    source = tmp_path / f"{action}.png"
    Image.new("RGB", (320, 180), color=(255, 255, 255)).save(source, format="PNG")
    provider = CapturingVisionProvider()

    result = asyncio.run(
        VisionService().analyze(
            workspace=str(tmp_path),
            paths=[source.name],
            action=action,
            provider_override=provider,
        )
    )

    assert result["status"] == "ok"
    assert result["result"]["observations"] == ["可见矩形与文字"]
    assert result["result"]["inferences"] == ["可能是界面截图"]
    assert result["result"]["uncertainties"] == ["小字不可确认"]
    content = provider.messages[0]["content"]
    assert content[0]["type"] == "text"
    assert content[1] == {"type": "text", "text": "image_id: image-1"}
    assert content[2]["type"] == "image_url"
    assert "private-photo" not in json.dumps(provider.messages)


def test_compare_preserves_stable_image_order(tmp_path: Path) -> None:
    for name, color in (("before.png", "white"), ("after.png", "black")):
        Image.new("RGB", (100, 100), color=color).save(tmp_path / name, format="PNG")
    provider = CapturingVisionProvider()

    result = asyncio.run(
        VisionService().analyze(
            workspace=str(tmp_path),
            paths=["before.png", "after.png"],
            action="compare",
            provider_override=provider,
        )
    )

    assert result["status"] == "ok"
    assert [item["image_id"] for item in result["images"]] == ["image-1", "image-2"]
    labels = [
        item["text"]
        for item in provider.messages[0]["content"]
        if item["type"] == "text" and item["text"].startswith("image_id:")
    ]
    assert labels == ["image_id: image-1", "image_id: image-2"]


def test_unsupported_provider_and_ocr_fallback_are_explicit(tmp_path: Path) -> None:
    source = tmp_path / "text.png"
    Image.new("RGB", (100, 50), color="white").save(source, format="PNG")
    unsupported = CapturingVisionProvider(vision=False)

    result = asyncio.run(
        VisionService().analyze(
            workspace=str(tmp_path),
            paths=[source.name],
            action="describe",
            provider_override=unsupported,
        )
    )
    assert result["status"] == "unsupported"
    assert result["error_code"] == "vision_unsupported"
    assert "描述" not in result

    fallback = asyncio.run(
        VisionService(ocr_backend=lambda _image: "实际 OCR 后备输出").analyze(
            workspace=str(tmp_path),
            paths=[source.name],
            action="extract_text",
            provider_override=unsupported,
        )
    )
    assert fallback["status"] == "degraded"
    assert fallback["fallback"] == "ocr"
    assert fallback["result"]["observations"] == "实际 OCR 后备输出"


def test_remote_api_requires_explicit_confirmation_without_exposing_key(tmp_path: Path) -> None:
    source = tmp_path / "ui.png"
    Image.new("RGB", (100, 50), color="white").save(source, format="PNG")
    with TestClient(app) as client:
        conversation = client.post(
            "/api/conversations",
            json={"workspace": str(tmp_path), "permission_mode": "ask"},
        ).json()
        response = client.post(
            "/api/vision/analyze",
            headers={"X-Vision-Model-Api-Key": "test-secret-that-must-not-return"},
            json={
                "conversation_id": conversation["id"],
                "workspace": str(tmp_path),
                "permission_mode": "ask",
                "action": "inspect_ui",
                "paths": [source.name],
                "provider_mode": "remote",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "confirmation_required"
    assert payload["permission"] == "network.request"
    assert "test-secret-that-must-not-return" not in response.text


def test_confirmed_remote_api_executes_with_request_scoped_key(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "confirmed.png"
    _save_image(source)
    captured: dict[str, Any] = {}

    async def fake_transport(messages, api_key=None, **kwargs):
        captured["messages"] = messages
        captured["api_key"] = api_key
        return {
            "role": "assistant",
            "content": '{"observations":["confirmed"],"inferences":[],"uncertainties":[],"confidence":0.8}',
        }

    monkeypatch.setattr(settings, "vision_remote_base_url", "https://vision.invalid/v1")
    monkeypatch.setattr(settings, "vision_remote_model", "vision-test")
    monkeypatch.setattr("app.vision.providers.transport_completion", fake_transport)
    secret = "confirmed-request-only-key"
    with TestClient(app) as client:
        conversation = client.post(
            "/api/conversations",
            json={"workspace": str(tmp_path), "permission_mode": "ask"},
        ).json()
        request = {
            "conversation_id": conversation["id"],
            "workspace": str(tmp_path),
            "permission_mode": "ask",
            "action": "describe",
            "paths": [source.name],
            "provider_mode": "remote",
        }
        pending = client.post(
            "/api/vision/analyze",
            headers={"X-Vision-Model-Api-Key": secret},
            json=request,
        ).json()
        request["approval_tokens"] = [pending["approval_key"]]
        response = client.post(
            "/api/vision/analyze",
            headers={"X-Vision-Model-Api-Key": secret},
            json=request,
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert captured["api_key"] == secret
    assert secret not in response.text
    assert "private description" not in json.dumps(captured["messages"], ensure_ascii=False)


def test_readonly_mode_denies_remote_image_export_before_provider_call(tmp_path: Path) -> None:
    source = tmp_path / "denied.png"
    Image.new("RGB", (100, 50), color="white").save(source, format="PNG")
    with TestClient(app) as client:
        conversation = client.post(
            "/api/conversations",
            json={"workspace": str(tmp_path), "permission_mode": "readonly"},
        ).json()
        response = client.post(
            "/api/vision/analyze",
            headers={"X-Vision-Model-Api-Key": "unused-request-key"},
            json={
                "conversation_id": conversation["id"],
                "workspace": str(tmp_path),
                "permission_mode": "readonly",
                "action": "describe",
                "paths": [source.name],
                "provider_mode": "remote",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert response.json()["error_code"] == "read_only_mode"


def test_in_flight_vision_request_is_cancellable(tmp_path: Path) -> None:
    source = tmp_path / "cancel.png"
    Image.new("RGB", (100, 50), color="white").save(source, format="PNG")

    class SlowProvider(CapturingVisionProvider):
        async def chat(self, messages: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:
            self.messages = messages
            await asyncio.Event().wait()
            return {"role": "assistant", "content": "never"}

    async def scenario() -> None:
        task = asyncio.create_task(
            VisionService().analyze(
                workspace=str(tmp_path),
                paths=[source.name],
                action="describe",
                provider_override=SlowProvider(),
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(scenario(), timeout=1))


def test_six_vision_interfaces_are_registered_and_selected_for_image_tasks() -> None:
    expected = {
        "vision.describe",
        "vision.extract_text",
        "vision.analyze_chart",
        "vision.compare",
        "vision.inspect_ui",
        "vision.classify",
    }
    assert expected <= set(REGISTRY)
    selected = {
        item["function"]["name"]
        for item in select_model_tools("比较两张 UI 截图并分析图表差异", [], [])
    }
    assert expected <= selected


def test_runtime_tool_degrades_explicitly_when_local_vision_is_unconfigured(tmp_path: Path) -> None:
    source = tmp_path / "screen.png"
    Image.new("RGB", (100, 50), color="white").save(source, format="PNG")

    outcome = asyncio.run(
        execute_runtime_tool(
            workspace=str(tmp_path),
            mode="full",
            name="vision.inspect_ui",
            arguments={"path": source.name},
            tool_call_id="vision-call",
            approved_actions=[],
            approval_scope="once",
            conversation_id=0,
            task_id="vision-task",
            mcp_routes={},
            allow_local_mcp=False,
        )
    )

    assert outcome.result["status"] == "unsupported"
    assert outcome.result["error_code"] == "unsupported_capability"
    assert "不会自动下载" in outcome.result["message"]


def test_remote_provider_uses_request_key_and_only_cleaned_pixels(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "remote.jpg"
    _save_image(source)
    captured: dict[str, Any] = {}

    async def fake_transport(messages, api_key=None, **kwargs):
        captured["messages"] = messages
        captured["api_key"] = api_key
        captured.update(kwargs)
        return {
            "role": "assistant",
            "content": '{"observations":["clean"],"inferences":[],"uncertainties":[],"confidence":0.9}',
        }

    monkeypatch.setattr("app.vision.providers.transport_completion", fake_transport)
    provider = OpenAICompatibleVisionProvider(
        provider_id="remote-test",
        base_url="https://vision.invalid/v1",
        model="vision-test",
        api_key="request-only-key",
        local=False,
    )

    result = asyncio.run(
        VisionService().analyze(
            workspace=str(tmp_path),
            paths=[source.name],
            action="describe",
            provider_override=provider,
        )
    )

    assert result["status"] == "ok"
    assert captured["api_key"] == "request-only-key"
    serialized = json.dumps(captured["messages"], ensure_ascii=False)
    assert "private description" not in serialized
    assert "private author" not in serialized
    assert "request-only-key" not in json.dumps(result, ensure_ascii=False)
