from __future__ import annotations

import json
import platform
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .build_info import sidecar_build_info
from .config import settings
from .database import database_backups, database_status, rows
from .deployment import validate_deployment_security
from .kernel.services import kernel_manifest
from .hooks import hook_catalog
from .lsp import lsp_status
from .mcp import MCP_CONNECTIONS
from .trust import redact_payload


MAX_LOG_BYTES = 512_000
MAX_DIAGNOSTIC_BUNDLES = 10
LOCAL_PATH_PATTERNS = (
    re.compile(r"(?i)\b[A-Z]:\\(?:[^\\\s\"']+\\)*[^\s\"']*"),
    re.compile(r"\\\\[^\\\s]+\\[^\s\"']+"),
    re.compile(r"(?<![\w:])/(?:home|Users|var|tmp|opt)/[^\s\"']+"),
)


def _redact_local_paths(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_local_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_local_paths(item) for item in value]
    if isinstance(value, str):
        cleaned = value
        for pattern in LOCAL_PATH_PATTERNS:
            cleaned = pattern.sub("[LOCAL_PATH]", cleaned)
        return cleaned
    return value


def _safe_json(value: Any) -> bytes:
    cleaned, _ = redact_payload(value)
    cleaned = _redact_local_paths(cleaned)
    return (json.dumps(cleaned, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")


def _safe_log_tail(path: Path) -> bytes:
    if not path.is_file():
        return b""
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - MAX_LOG_BYTES))
        text = handle.read(MAX_LOG_BYTES).decode("utf-8", errors="replace")
    cleaned, _ = redact_payload(text)
    return _redact_local_paths(cleaned).encode("utf-8")


def diagnostic_manifest() -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": __version__,
        "build": sidecar_build_info(),
        "runtime": {
            "python": platform.python_version(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "deployment": validate_deployment_security(),
        "database": database_status(),
        "kernel": kernel_manifest(),
        "capabilities": {
            "hooks": hook_catalog(),
            "lsp": lsp_status(),
            "mcp": MCP_CONNECTIONS.status(),
            "managed_worktrees": True,
        },
        "provider": {
            "model": settings.model_name,
            "base_url_host": urlsplit(settings.model_base_url).hostname or "invalid",
            "configured": bool(settings.deepseek_api_key),
        },
        "limits": {
            "max_agent_rounds": settings.max_agent_rounds,
            "max_tool_calls": settings.max_tool_calls,
            "task_timeout_seconds": settings.task_timeout_seconds,
            "max_concurrent_tasks": settings.max_concurrent_tasks,
        },
        "backup_count": len(database_backups()),
    }


def create_diagnostic_bundle() -> dict[str, Any]:
    folder = Path(settings.database_path).parent / "diagnostics"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = folder / f"agent-diagnostics-{__version__}-{stamp}.zip"
    audit_rows = rows("SELECT action, target, status, details, created_at FROM audit_logs ORDER BY id DESC LIMIT 200")
    data_flows = rows(
        "SELECT source, sink, classification, fields, redactions, allowed, reason, created_at "
        "FROM data_flow_events ORDER BY id DESC LIMIT 200"
    )
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", _safe_json(diagnostic_manifest()))
        bundle.writestr("audit-recent.json", _safe_json(audit_rows))
        bundle.writestr("data-flows-recent.json", _safe_json(data_flows))
        log_tail = _safe_log_tail(Path(settings.log_path))
        if log_tail:
            bundle.writestr("agent.log", log_tail)
    for stale in sorted(folder.glob("agent-diagnostics-*.zip"), reverse=True)[MAX_DIAGNOSTIC_BUNDLES:]:
        stale.unlink(missing_ok=True)
    return {"path": target, "name": target.name, "size": target.stat().st_size}
