from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from app.config import settings
from app.data_flow import record_data_flow
from app.database import audit
from app.providers.base import LLMProvider
from app.providers.provider import ProviderError
from app.providers.registry import get_provider
from app.vision.images import ImageValidationError, PreparedImage, prepare_workspace_image
from app.vision.providers import configured_local_provider, configured_remote_provider


VisionOcrBackend = Callable[[PreparedImage], Awaitable[str] | str]


class VisionError(ValueError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


ACTION_PROMPTS = {
    "describe": "描述图片中可直接观察到的主体、场景、文字和空间关系。",
    "extract_text": "提取图片中可见文字，保持阅读顺序；不可辨认处使用 [不确定]，不得补写。",
    "analyze_chart": "读取图表标题、坐标、图例、可见数值和趋势；区分直接读数与推断。",
    "compare": "按 image_id 比较多张图片，列出相同点、差异和无法确认的部分。",
    "inspect_ui": "诊断 UI 截图：先写可见现象，再写可能原因和建议；原因必须标记为推断。",
    "classify": "依据可见证据分类，并给出候选类别、置信度和不确定因素。",
}


def _provider_for_mode(mode: str, api_key: str | None) -> LLMProvider:
    if mode == "remote":
        return configured_remote_provider(api_key)
    if mode == "local":
        return configured_local_provider()
    return get_provider()


def _safe_result_payload(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        return {
            "observations": parsed.get("observations") or parsed.get("text") or content,
            "inferences": parsed.get("inferences") or [],
            "uncertainties": parsed.get("uncertainties") or ["模型未提供明确不确定性说明"],
            "confidence": parsed.get("confidence"),
        }
    return {
        "observations": content,
        "inferences": [],
        "uncertainties": ["模型返回非结构化结果，置信度未知"],
        "confidence": None,
    }


class VisionService:
    def __init__(self, *, ocr_backend: VisionOcrBackend | None = None) -> None:
        self.ocr_backend = ocr_backend

    async def analyze(
        self,
        *,
        workspace: str,
        paths: list[str],
        action: str,
        prompt: str = "",
        provider_mode: str = "active",
        api_key: str | None = None,
        conversation_id: int | None = None,
        task_id: str | None = None,
        provider_override: LLMProvider | None = None,
    ) -> dict[str, Any]:
        if action not in ACTION_PROMPTS:
            raise VisionError("未知视觉动作", "invalid_action")
        if not 1 <= len(paths) <= settings.vision_max_images:
            raise VisionError(f"图片数量必须在 1 到 {settings.vision_max_images} 之间", "image_count_limit")
        if action == "compare" and len(paths) < 2:
            raise VisionError("多图比较至少需要两张图片", "compare_requires_multiple_images")
        if action != "compare" and len(paths) > 1:
            raise VisionError("只有 compare 接口接受多张图片", "multiple_images_not_allowed")
        try:
            prepared = [
                prepare_workspace_image(workspace, path, f"image-{index}")
                for index, path in enumerate(paths, start=1)
            ]
        except ImageValidationError as exc:
            raise VisionError(str(exc), "invalid_image") from exc
        if sum(item.source_bytes for item in prepared) > settings.vision_max_total_bytes:
            raise VisionError("图片总字节数超过视觉请求安全上限", "image_total_bytes_limit")
        if sum(item.source_width * item.source_height for item in prepared) > settings.vision_max_total_pixels:
            raise VisionError("图片总像素超过视觉请求安全上限", "image_total_pixels_limit")

        try:
            provider = provider_override or _provider_for_mode(provider_mode, api_key)
            supported = provider.get_capabilities().vision
            if supported is not True:
                if action == "extract_text" and self.ocr_backend is not None:
                    values: list[str] = []
                    for image in prepared:
                        value = self.ocr_backend(image)
                        if hasattr(value, "__await__"):
                            value = await value  # type: ignore[misc]
                        values.append(str(value))
                    return {
                        "status": "degraded",
                        "action": action,
                        "provider": "ocr-fallback",
                        "fallback": "ocr",
                        "result": {
                            "observations": "\n".join(values),
                            "inferences": [],
                            "uncertainties": ["OCR 后备不具备通用视觉理解能力"],
                            "confidence": None,
                        },
                        "images": [item.public_metadata() for item in prepared],
                    }
                return {
                    "status": "unsupported",
                    "action": action,
                    "provider": provider.id,
                    "error_code": "vision_unsupported",
                    "message": "当前模型不支持视觉；未生成图片描述",
                    "ocr_fallback": "unavailable" if self.ocr_backend is None else "available_for_extract_text",
                    "images": [item.public_metadata() for item in prepared],
                }
        except ProviderError as exc:
            return {
                "status": "unsupported" if exc.error_type == "unsupported_capability" else "blocked",
                "action": action,
                "error_code": exc.error_type,
                "message": str(exc),
                "images": [item.public_metadata() for item in prepared],
            }

        text = (
            f"{ACTION_PROMPTS[action]}\n"
            "输出 JSON 对象，字段为 observations、inferences、uncertainties、confidence。"
            "只把图片中直接可见的内容写入 observations；推测只能写入 inferences；"
            "无法确认的文字、数值、遮挡和因果关系写入 uncertainties。"
        )
        if prompt.strip():
            text += f"\n用户补充要求：{prompt.strip()}"
        content_parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for item in prepared:
            for part_id, part in zip(item.part_ids, item.parts, strict=True):
                content_parts.append({"type": "text", "text": f"image_id: {part_id}"})
                content_parts.append(part)
        record_data_flow(
            source="workspace_image",
            sink=f"vision_provider:{provider.id}",
            classification="private",
            fields=("image_sha256", "dimensions", "sanitized_pixels"),
            redactions=1,
            allowed=True,
            reason="explicit vision request after local metadata removal",
            conversation_id=conversation_id,
            task_id=task_id,
        )
        response = await provider.vision(
            [{"role": "user", "content": content_parts}],
            images=None,
            max_tokens=min(settings.model_max_tokens, 4096),
            phase="vision",
            conversation_id=conversation_id,
            task_id=task_id,
        )
        content = str(response.get("content") or "").strip()
        if not content:
            raise VisionError("视觉 Provider 返回空结果", "empty_response")
        result = {
            "status": "ok",
            "action": action,
            "provider": provider.id,
            "model": getattr(provider, "model", ""),
            "fallback": None,
            "result": _safe_result_payload(content),
            "images": [item.public_metadata() for item in prepared],
        }
        audit(
            conversation_id,
            f"vision.{action}",
            ",".join(item.sha256[:12] for item in prepared),
            "ok",
            {
                "provider": provider.id,
                "image_count": len(prepared),
                "metadata_removed": True,
                "content_logged": False,
            },
        )
        return result
