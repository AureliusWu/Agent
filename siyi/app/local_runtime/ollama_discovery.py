from __future__ import annotations

import os
import shutil
import socket
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


@dataclass(frozen=True)
class OllamaInstallation:
    installed: bool
    executable: str | None
    source: str


def validate_local_ollama_url(base_url: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Ollama lifecycle management only accepts a local HTTP loopback address")
    if parsed.port is not None and not 1024 <= parsed.port <= 65535:
        raise ValueError("Ollama lifecycle management requires a non-privileged local port")
    return base_url.rstrip("/")


def discover_ollama(configured_path: str | None = None) -> OllamaInstallation:
    candidates: list[tuple[str, Path]] = []
    if configured_path:
        candidates.append(("configured", Path(configured_path).expanduser()))
    located = shutil.which("ollama")
    if located:
        candidates.append(("path", Path(located)))
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    program_files = os.environ.get("ProgramFiles", "").strip()
    if local_app_data:
        candidates.append(("local_app_data", Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe"))
    if program_files:
        candidates.append(("program_files", Path(program_files) / "Ollama" / "ollama.exe"))
    seen: set[str] = set()
    for source, candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        key = str(resolved).casefold()
        if key in seen or not resolved.is_file():
            continue
        seen.add(key)
        return OllamaInstallation(True, str(resolved), source)
    return OllamaInstallation(False, None, "not_found")


def listener_pid(port: int = 11434) -> int | None:
    result = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP" or fields[3].upper() != "LISTENING":
            continue
        endpoint = fields[1]
        if endpoint.rsplit(":", 1)[-1] == str(port):
            try:
                return int(fields[-1])
            except ValueError:
                return None
    return None


def port_open(host: str = "127.0.0.1", port: int = 11434, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def probe_ollama(base_url: str = DEFAULT_OLLAMA_URL, timeout_seconds: float = 2.0) -> dict:
    resolved = validate_local_ollama_url(base_url)
    parsed = urlsplit(resolved)
    port = parsed.port or 11434
    installation = discover_ollama()
    started = __import__("time").perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            response = await client.get(f"{resolved}/api/version")
        response.raise_for_status()
        payload = response.json()
        version = str(payload.get("version") or "").strip() if isinstance(payload, dict) else ""
        if not version:
            raise ValueError("missing Ollama version")
        return {
            **asdict(installation),
            "api_healthy": True,
            "version": version,
            "base_url": resolved,
            "listener_pid": listener_pid(port),
            "latency_ms": round((__import__("time").perf_counter() - started) * 1000, 3),
            "error": None,
        }
    except (httpx.HTTPError, ValueError) as exc:
        occupied = port_open(port=port)
        return {
            **asdict(installation),
            "api_healthy": False,
            "version": None,
            "base_url": resolved,
            "listener_pid": listener_pid(port),
            "latency_ms": round((__import__("time").perf_counter() - started) * 1000, 3),
            "error": "PORT_CONFLICT" if occupied else "SERVICE_STOPPED",
            "detail": type(exc).__name__,
        }
