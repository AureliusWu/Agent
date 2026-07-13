from __future__ import annotations

import time
import asyncio
from typing import Any

import httpx

from .config import settings


async def completion(
    messages: list[dict[str, Any]],
    api_key: str | None = None,
    *,
    tools: list[dict[str, Any]] | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    key = api_key or settings.deepseek_api_key
    if not key:
        raise ValueError("未配置模型 API Key")
    payload: dict[str, Any] = {
        "model": model or settings.model_name,
        "messages": messages,
        "temperature": 0.2,
    }
    if tools:
        payload.update({"tools": tools, "tool_choice": "auto"})
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15), follow_redirects=True) as client:
        for attempt in range(3):
            try:
                response = await client.post(
                    (base_url or settings.model_base_url).rstrip("/") + "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=payload,
                )
                if response.status_code in {401, 403}:
                    raise ValueError("模型 API Key 无效或没有访问权限")
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
                response.raise_for_status()
                body = response.json()
                message = body["choices"][0]["message"]
                message["_metrics"] = {"latency_ms": round((time.perf_counter() - started) * 1000), "usage": body.get("usage", {}), "attempts": attempt + 1}
                return message
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** attempt)); continue
                raise ValueError(f"模型服务连接失败：{type(exc).__name__}") from exc
    raise ValueError("模型调用失败，已达到最大重试次数")


async def provider_health(api_key: str | None = None) -> dict[str, Any]:
    key = api_key or settings.deepseek_api_key
    if not key:
        return {"status": "unconfigured", "latency_ms": None, "model": settings.model_name}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            response = await client.get(
                settings.model_base_url.rstrip("/") + "/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            response.raise_for_status()
        return {"status": "ok", "latency_ms": round((time.perf_counter() - started) * 1000), "model": settings.model_name}
    except Exception as exc:
        return {"status": "error", "latency_ms": round((time.perf_counter() - started) * 1000), "model": settings.model_name, "error": str(exc)}
