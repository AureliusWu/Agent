"""Versioned evaluation identity; historical reports remain readable, not RC proof."""
from __future__ import annotations

import hashlib
import json
import platform
import sys
from typing import Any
from urllib.parse import urlsplit

from app.config import settings
from .models import EvalMode, EvalTaskSpec

MODE_LAYERS = {"scripted_runtime": "deterministic_runtime", "live_model": "autonomous_model", "adversarial": "adversarial"}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def task_contract(tasks: list[EvalTaskSpec], suite: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "suite": suite,
        "task_ids": sorted(task.id for task in tasks),
        "tasks_sha256": digest([task.model_dump(mode="json") for task in sorted(tasks, key=lambda item: item.id)]),
    }


def provider_identity(mode: EvalMode) -> dict[str, Any]:
    if mode != "live_model":
        return {"name": "adversarial-script" if mode == "adversarial" else "deterministic-script"}
    endpoint = urlsplit(settings.model_base_url)
    # Never persist userinfo, query parameters or the raw endpoint path.
    normalized = (endpoint.scheme.lower(), (endpoint.hostname or "").lower(), endpoint.port, endpoint.path.rstrip("/"))
    return {"endpoint_sha256": digest(normalized), "model": settings.model_name}


def comparison_environment() -> dict[str, Any]:
    # Same machine/configuration is required for latency comparisons. No raw
    # hostname is persisted; source revision deliberately differs across runs.
    limits = ("max_duplicate_tool_calls", "max_phase_tokens", "max_model_call_tokens", "max_tool_result_chars", "max_file_snippet_chars", "max_consecutive_failures", "max_no_progress_rounds", "max_repair_attempts")
    return {
        "schema_version": 1,
        "host": digest(platform.node()),
        "os": platform.platform(),
        "machine": platform.machine(),
        "python": list(sys.version_info[:3]),
        "workflow": "isolated-sqlite-runtime-v1",
        "runtime_limits": {name: getattr(settings, name) for name in limits},
    }
