from __future__ import annotations

import asyncio
import json
import time
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from app.database import connect, now_iso

from .ollama_discovery import DEFAULT_OLLAMA_URL, validate_local_ollama_url
from .resource_coordinator import resource_coordinator


class ModelManagerError(RuntimeError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class ModelManager:
    def __init__(self, base_url: str = DEFAULT_OLLAMA_URL) -> None:
        self.base_url = validate_local_ollama_url(base_url)
        self._download_tasks: dict[str, asyncio.Task[None]] = {}
        self._download_state: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def _json(self, method: str, path: str, payload: dict | None = None, timeout: float = 30) -> dict:
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.request(method, f"{self.base_url}{path}", json=payload)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelManagerError(f"Ollama API request failed: {type(exc).__name__}", "OLLAMA_API_ERROR") from exc
        if not isinstance(data, dict):
            raise ModelManagerError("Ollama returned an invalid JSON object", "OLLAMA_INVALID_RESPONSE")
        return data

    async def list_models(self) -> list[dict[str, Any]]:
        tags, running = await asyncio.gather(self._json("GET", "/api/tags"), self._json("GET", "/api/ps"))
        loaded = {str(item.get("name") or item.get("model") or ""): item for item in running.get("models", []) if isinstance(item, dict)}
        result = []
        for raw in tags.get("models", []):
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or raw.get("model") or "").strip()
            if not name:
                continue
            details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
            active = loaded.get(name) or {}
            result.append({
                "name": name,
                "size": int(raw.get("size") or 0),
                "modified_at": str(raw.get("modified_at") or ""),
                "parameter_size": str(details.get("parameter_size") or ""),
                "quantization": str(details.get("quantization_level") or ""),
                "loaded": bool(active),
                "context_length": int(active.get("context_length") or 0),
                "size_vram": int(active.get("size_vram") or 0),
                "expires_at": active.get("expires_at"),
                "recommended": name == "qwen3:4b",
            })
        with connect() as db:
            for item in result:
                db.execute(
                    "INSERT OR REPLACE INTO local_model_registry(name,status,size_bytes,parameter_size,quantization,context_length,size_vram_bytes,modified_at,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (item["name"], "LOADED" if item["loaded"] else "INSTALLED", item["size"], item["parameter_size"], item["quantization"], item["context_length"], item["size_vram"], item["modified_at"], now_iso()),
                )
        return result

    async def running_models(self) -> list[dict[str, Any]]:
        payload = await self._json("GET", "/api/ps")
        return [item for item in payload.get("models", []) if isinstance(item, dict)]

    def download_preview(self, model: str) -> dict[str, Any]:
        estimates = {"qwen3:4b": 2_497_293_931, "qwen3:8b": 5_200_000_000}
        root = Path(os.environ.get("OLLAMA_MODELS") or (Path.home() / ".ollama" / "models")).resolve()
        return {
            "model": model,
            "estimated_bytes": estimates.get(model),
            "target_directory": str(root),
            "confirmation_required": True,
            "auto_load": False,
        }

    async def preload(self, model: str, keep_alive: str = "5m") -> dict[str, Any]:
        allowed = {"0", "5m", "10m", "-1"}
        if keep_alive not in allowed:
            raise ModelManagerError("keep_alive must be one of 0, 5m, 10m, -1", "INVALID_KEEP_ALIVE")
        async with self._lock:
            running = await self.running_models()
            for item in running:
                other = str(item.get("name") or item.get("model") or "")
                if other and other != model:
                    await self.unload(other)
            before = resource_coordinator.snapshot(active_model=None)
            started = time.perf_counter()
            await self._json("POST", "/api/generate", {"model": model, "prompt": "", "stream": False, "keep_alive": keep_alive}, timeout=180)
            current = await self.running_models()
            if not any(str(item.get("name") or item.get("model") or "") == model for item in current):
                raise ModelManagerError("Ollama did not report the model as loaded", "MODEL_LOAD_UNCONFIRMED")
            after = resource_coordinator.snapshot(active_model=model)
            elapsed = round((time.perf_counter()-started)*1000, 3)
            with connect() as db:
                db.execute("INSERT INTO model_load_records(id,model,action,status,keep_alive,duration_ms,resources_before,resources_after,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, model, "load", "LOADED", keep_alive, elapsed, json.dumps(before), json.dumps(after), now_iso()))
            return {"status": "LOADED", "model": model, "keep_alive": keep_alive, "load_ms": elapsed, "resources_before": before, "resources_after": after}

    async def unload(self, model: str) -> dict[str, Any]:
        from app.providers.ollama import active_ollama_requests
        active_requests = active_ollama_requests(model)
        if active_requests:
            raise ModelManagerError(
                f"Cannot unload {model} while {active_requests} generation request(s) are active",
                "ACTIVE_GENERATION",
            )
        before = resource_coordinator.snapshot(active_model=model)
        await self._json("POST", "/api/generate", {"model": model, "prompt": "", "stream": False, "keep_alive": 0}, timeout=30)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            running = await self.running_models()
            if not any(str(item.get("name") or item.get("model") or "") == model for item in running):
                after = resource_coordinator.snapshot(active_model=None)
                release = _released(before, after)
                with connect() as db:
                    db.execute("INSERT INTO model_load_records(id,model,action,status,duration_ms,resources_before,resources_after,created_at) VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, model, "unload", "UNLOADED", 0, json.dumps(before), json.dumps(after), now_iso()))
                return {"status": "UNLOADED", "model": model, "resources_before": before, "resources_after": after, "resource_release_observed": release}
            await asyncio.sleep(0.25)
        raise ModelManagerError("Model remained loaded after unload request", "MODEL_UNLOAD_UNCONFIRMED")

    async def start_download(self, model: str, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise ModelManagerError("Model download requires explicit user confirmation", "DOWNLOAD_CONFIRMATION_REQUIRED")
        if model in self._download_tasks and not self._download_tasks[model].done():
            return self._download_state[model]
        state = {"id": uuid.uuid4().hex, "model": model, "status": "DOWNLOADING", "completed": 0, "total": 0, "started_at": time.time(), "error": None}
        self._download_state[model] = state
        with connect() as db:
            db.execute("INSERT INTO model_download_records(id,model,status,confirmed,started_at) VALUES(?,?,?,?,?)", (state["id"], model, state["status"], 1, now_iso()))
        self._download_tasks[model] = asyncio.create_task(self._download(model, state))
        return dict(state)

    async def _download(self, model: str, state: dict[str, Any]) -> None:
        try:
            async with httpx.AsyncClient(timeout=None, follow_redirects=False) as client:
                async with client.stream("POST", f"{self.base_url}/api/pull", json={"model": model, "stream": True}) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        payload = json.loads(line)
                        state.update({"status": str(payload.get("status") or "DOWNLOADING"), "completed": int(payload.get("completed") or 0), "total": int(payload.get("total") or 0)})
                        with connect() as db:
                            db.execute("UPDATE model_download_records SET status=?,completed_bytes=?,total_bytes=? WHERE id=?", (state["status"], state["completed"], state["total"], state["id"]))
            state["status"] = "INSTALLED"
            state["finished_at"] = time.time()
        except asyncio.CancelledError:
            state["status"] = "CANCELLED"
            state["finished_at"] = time.time()
            raise
        except Exception as exc:
            state["status"] = "ERROR"
            state["error"] = type(exc).__name__
            state["finished_at"] = time.time()
        finally:
            if state.get("finished_at"):
                with connect() as db:
                    db.execute("UPDATE model_download_records SET status=?,completed_bytes=?,total_bytes=?,error_type=?,finished_at=? WHERE id=?", (state["status"], state["completed"], state["total"], state.get("error"), now_iso(), state["id"]))

    def download_status(self, model: str | None = None) -> dict[str, Any] | list[dict[str, Any]]:
        if model is not None:
            return dict(self._download_state.get(model) or {"model": model, "status": "NOT_STARTED"})
        return [dict(value) for value in self._download_state.values()]

    async def cancel_download(self, model: str) -> dict[str, Any]:
        task = self._download_tasks.get(model)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        state = self._download_state.get(model) or {"model": model, "status": "NOT_STARTED"}
        if state.get("status") == "CANCELLED":
            try:
                installed = {item["name"] for item in await self.list_models()}
                state["incomplete_model_visible"] = model in installed
                state["cleanup"] = "ollama_content_store_transaction"
            except ModelManagerError:
                state["cleanup"] = "unverified_api_unavailable"
        return dict(state)


def _released(before: dict, after: dict) -> dict:
    def delta(key: str) -> int | None:
        left, right = before.get(key), after.get(key)
        return int(left) - int(right) if left is not None and right is not None else None
    return {"ollama_rss_delta_bytes": delta("ollama_rss_bytes"), "gpu_free_delta_bytes": (int(after["gpu_free_bytes"])-int(before["gpu_free_bytes"]) if before.get("gpu_free_bytes") is not None and after.get("gpu_free_bytes") is not None else None)}


model_manager = ModelManager()
