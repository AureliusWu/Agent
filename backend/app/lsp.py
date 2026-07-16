from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from .sandbox import safe_path, workspace_root
from .workspace_index import find_definition, find_references, find_symbol, inspect_diagnostics


SERVER_COMMANDS: dict[str, tuple[tuple[str, ...], ...]] = {
    ".py": (("pyright-langserver", "--stdio"), ("pylsp",)),
    ".ts": (("typescript-language-server", "--stdio"),),
    ".tsx": (("typescript-language-server", "--stdio"),),
    ".js": (("typescript-language-server", "--stdio"),),
    ".jsx": (("typescript-language-server", "--stdio"),),
    ".rs": (("rust-analyzer",),),
}
LANGUAGE_IDS = {".py": "python", ".ts": "typescript", ".tsx": "typescriptreact", ".js": "javascript", ".jsx": "javascriptreact", ".rs": "rust"}


def lsp_status() -> dict[str, Any]:
    candidates = {
        "python": ("pyright-langserver", "pylsp"),
        "typescript": ("typescript-language-server",),
        "rust": ("rust-analyzer",),
    }
    return {
        "available": {
            language: next((command for command in commands if shutil.which(command)), None)
            for language, commands in candidates.items()
        },
        "fallback": "workspace_index",
    }


def _frame(message: dict[str, Any]) -> bytes:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


async def _send_message(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write(_frame(message))
    await writer.drain()


async def _read_message(reader: asyncio.StreamReader, timeout_seconds: float) -> dict[str, Any]:
    header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout_seconds)
    lines = header[:-4].decode("ascii", errors="ignore").splitlines()
    length = next((int(line.split(":", 1)[1].strip()) for line in lines if line.lower().startswith("content-length:")), 0)
    if length <= 0:
        raise ValueError("Language server returned an invalid Content-Length")
    body = await asyncio.wait_for(reader.readexactly(length), timeout=timeout_seconds)
    value = json.loads(body.decode("utf-8", errors="replace"))
    if not isinstance(value, dict):
        raise ValueError("Language server returned a non-object message")
    return value


async def _read_response(reader: asyncio.StreamReader, request_id: int, timeout_seconds: float) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        message = await _read_message(reader, remaining)
        if message.get("id") == request_id:
            return message


def _parse_messages(raw: bytes) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(raw):
        header_end = raw.find(b"\r\n\r\n", cursor)
        if header_end < 0:
            break
        headers = raw[cursor:header_end].decode("ascii", errors="ignore").splitlines()
        length = next((int(line.split(":", 1)[1].strip()) for line in headers if line.lower().startswith("content-length:")), 0)
        start, end = header_end + 4, header_end + 4 + length
        if length <= 0 or end > len(raw):
            break
        try:
            value = json.loads(raw[start:end].decode("utf-8", errors="replace"))
            if isinstance(value, dict):
                messages.append(value)
        except json.JSONDecodeError:
            pass
        cursor = end
    return messages


def _server_for(path: Path) -> tuple[str, ...] | None:
    for candidate in SERVER_COMMANDS.get(path.suffix.lower(), ()):
        executable = shutil.which(candidate[0])
        if executable:
            return (executable, *candidate[1:])
    return None


def _normalize_location(value: Any, root: Path) -> Any:
    if isinstance(value, list):
        normalized_items = [_normalize_location(item, root) for item in value]
        return [item for item in normalized_items if item is not None]
    if not isinstance(value, dict):
        return value
    normalized = {key: _normalize_location(item, root) for key, item in value.items()}
    uri = normalized.get("uri") or normalized.get("targetUri")
    if isinstance(uri, str) and uri.startswith("file:"):
        path = Path(url2pathname(urlparse(uri).path)).resolve()
        try:
            normalized["path"] = path.relative_to(root).as_posix()
        except ValueError:
            return None
        normalized.pop("uri", None)
        normalized.pop("targetUri", None)
    return normalized


def _fallback(workspace: str, path: str, operation: str, symbol: str | None) -> dict[str, Any]:
    if operation == "definition" and symbol:
        result = find_definition(workspace, symbol)
    elif operation == "references" and symbol:
        result = find_references(workspace, symbol)
    elif operation == "symbols" and symbol:
        result = find_symbol(workspace, symbol)
    else:
        result = inspect_diagnostics(workspace, path)
    return {"success": True, "status": "ok", "source": "workspace-index-fallback", "lsp_available": False, "result": result}


async def query_lsp(
    workspace: str,
    path: str,
    operation: str,
    *,
    line: int = 0,
    character: int = 0,
    symbol: str | None = None,
    timeout_seconds: float = 15,
) -> dict[str, Any]:
    root = workspace_root(workspace)
    target = safe_path(root, path)
    if not target.is_file():
        raise FileNotFoundError(path)
    command = _server_for(target)
    if command is None or operation not in {"definition", "references", "symbols"}:
        return _fallback(workspace, path, operation, symbol)
    uri = target.as_uri()
    method = {"definition": "textDocument/definition", "references": "textDocument/references", "symbols": "textDocument/documentSymbol"}[operation]
    params: dict[str, Any] = {"textDocument": {"uri": uri}}
    if operation != "symbols":
        params["position"] = {"line": max(0, line), "character": max(0, character)}
    if operation == "references":
        params["context"] = {"includeDeclaration": True}
    process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise RuntimeError("Language server pipes are unavailable")
    try:
        await _send_message(process.stdin, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"processId": os.getpid(), "rootUri": root.as_uri(), "capabilities": {}}})
        initialized = await _read_response(process.stdout, 1, timeout_seconds)
        if "error" in initialized:
            raise ValueError(f"Language server initialization failed: {initialized['error']}")
        await _send_message(process.stdin, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
        await _send_message(process.stdin, {"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {"textDocument": {"uri": uri, "languageId": LANGUAGE_IDS.get(target.suffix.lower(), "plaintext"), "version": 1, "text": target.read_text(encoding="utf-8", errors="replace")}}})
        await _send_message(process.stdin, {"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
        response = await _read_response(process.stdout, 2, timeout_seconds)
        await _send_message(process.stdin, {"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": None})
        await _read_response(process.stdout, 3, min(timeout_seconds, 5))
        await _send_message(process.stdin, {"jsonrpc": "2.0", "method": "exit", "params": None})
        process.stdin.close()
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, json.JSONDecodeError, ValueError) as exc:
        if process.returncode is None:
            process.kill()
        await process.wait()
        code = "lsp_timeout" if isinstance(exc, TimeoutError) else "lsp_protocol_error"
        fallback = _fallback(workspace, path, operation, symbol)
        fallback.update({
            "degraded": True,
            "degradation_reason": code,
            "language_server_error": "Language server timed out" if code == "lsp_timeout" else type(exc).__name__,
        })
        return fallback
    if "error" in response:
        return {"success": False, "status": "error", "error_code": "lsp_failed", "error_message": str(response["error"])}
    return {"success": True, "status": "ok", "source": "language-server", "lsp_available": True, "server": Path(command[0]).name, "result": _normalize_location(response.get("result"), root)}
