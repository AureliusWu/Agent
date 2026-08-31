from __future__ import annotations

import asyncio
from collections import deque
import json
import os
import subprocess
from typing import Any, Callable

from app.mcp.permissions import McpSecretBindingError, normalize_secret_binding, redact_bound_value, stdio_environment, validate_stdio_server_binding
from app.mcp.protocol import initialize_params, notification_payload, request_payload
from app.mcp.rpc import McpProtocolError, McpRpcResponse, McpTransportError, raise_for_tool_result
from app.process_supervisor import register_process, terminate_process_tree, unregister_process


MAX_STDIO_MESSAGE_BYTES = 4 * 1024 * 1024


class StdioMcpTransport:
    """One initialized, persistent JSON-lines stdio MCP session."""

    def __init__(
        self,
        command: str,
        args: list[str],
        *,
        timeout: float = 45.0,
        secret_binding: str | None = None,
    ) -> None:
        validate_stdio_server_binding(command, args, secret_binding=secret_binding)
        self.command = command
        self.args = list(args)
        self.timeout = timeout
        self.secret_binding = normalize_secret_binding(secret_binding)
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._next_id = 1
        self._initialized = False
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._stderr_task: asyncio.Task[None] | None = None
        self._registered_pid: int | None = None
        self.authorization_check: Callable[[], None] | None = None
        self._bound_secret_value: str | None = None

    @property
    def initialized(self) -> bool:
        return self._initialized and self._process is not None and self._process.returncode is None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            self._stderr_tail.append(chunk.decode("utf-8", errors="replace").rstrip())

    async def _spawn_unlocked(self) -> None:
        if self.authorization_check is not None:
            self.authorization_check()
        creationflags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
        try:
            environment = stdio_environment(self.secret_binding)
            self._bound_secret_value = environment.get(self.secret_binding.removeprefix("env:")) if self.secret_binding else None
            self._process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
                env=environment,
                limit=MAX_STDIO_MESSAGE_BYTES,
            )
        except McpSecretBindingError as exc:
            raise McpTransportError("MCP 密钥引用不可用") from exc
        except OSError as exc:
            raise McpTransportError("无法启动 stdio MCP 进程") from exc
        self._registered_pid = self._process.pid
        register_process(self._process.pid, None, self.command, self.args, self._process)
        assert self._process.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._process.stderr))

    async def _write_unlocked(self, payload: dict[str, Any]) -> None:
        if self.authorization_check is not None:
            self.authorization_check()
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise McpTransportError("stdio MCP 进程未运行")
        try:
            process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            raise McpTransportError("stdio MCP 输入通道已关闭") from exc

    async def _request_unlocked(self, method: str, params: dict[str, Any]) -> McpRpcResponse:
        process = self._process
        if process is None or process.stdout is None:
            raise McpTransportError("stdio MCP 进程未运行")
        request_id = self._next_id
        self._next_id += 1
        await self._write_unlocked(request_payload(request_id, method, params))
        while True:
            try:
                raw = await asyncio.wait_for(process.stdout.readline(), timeout=self.timeout)
            except TimeoutError as exc:
                raise McpTransportError("stdio MCP 调用超时") from exc
            except (ValueError, asyncio.LimitOverrunError) as exc:
                raise McpProtocolError("stdio MCP 单条响应超过 4 MiB 安全上限") from exc
            if not raw:
                # External stderr can echo arbitrary credentials and paths.
                raise McpTransportError("stdio MCP 进程提前退出")
            try:
                decoded = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            messages = decoded if isinstance(decoded, list) else [decoded]
            if not messages or not all(isinstance(item, dict) for item in messages):
                raise McpProtocolError("stdio MCP 返回了无效 JSON-RPC batch")
            for value in messages:
                if "method" in value and "id" in value:
                    # Empty client capabilities mean only ping is expected.
                    # Always respond so a server request cannot deadlock the
                    # response to our outstanding request.
                    server_response = {
                        "jsonrpc": "2.0",
                        "id": value.get("id"),
                        **(
                            {"result": {}}
                            if value.get("method") == "ping"
                            else {"error": {"code": -32601, "message": "Method not supported by client"}}
                        ),
                    }
                    await self._write_unlocked(server_response)
                    continue
                if value.get("id") != request_id or ("result" not in value and "error" not in value):
                    # Notifications and unrelated responses do not complete this call.
                    continue
                parsed = McpRpcResponse.parse(redact_bound_value(value, self._bound_secret_value), expected_id=request_id)
                parsed.raise_for_error()
                return raise_for_tool_result(method, parsed)

    async def _open_unlocked(self) -> McpRpcResponse:
        if self.initialized:
            raise McpProtocolError("stdio MCP session 已初始化")
        await self._spawn_unlocked()
        try:
            initialized = await self._request_unlocked("initialize", initialize_params())
            await self._write_unlocked(notification_payload("notifications/initialized"))
            self._initialized = True
            return initialized
        except BaseException:
            await self._terminate_unlocked("initialization_failed")
            raise

    async def open(self) -> McpRpcResponse:
        async with self._lock:
            try:
                async with asyncio.timeout(self.timeout):
                    return await self._open_unlocked()
            except TimeoutError as exc:
                await self._terminate_unlocked("initialization_timeout")
                raise McpTransportError("stdio MCP 初始化超时") from exc

    async def request(self, method: str, params: dict[str, Any]) -> McpRpcResponse:
        async with self._lock:
            try:
                # This is a total request deadline, not a fresh timeout for
                # each notification/read. Notifications cannot extend it.
                async with asyncio.timeout(self.timeout):
                    if not self.initialized:
                        await self._open_unlocked()
                    return await self._request_unlocked(method, params)
            except TimeoutError as exc:
                await self._terminate_unlocked("request_timeout")
                raise McpTransportError("stdio MCP 调用总时限已用尽") from exc
            except asyncio.CancelledError:
                await self._terminate_unlocked("cancelled")
                raise
            except McpTransportError:
                await self._terminate_unlocked("transport_error")
                raise
            except McpProtocolError:
                await self._terminate_unlocked("protocol_error")
                raise

    async def _terminate_unlocked(self, status: str) -> None:
        process, self._process = self._process, None
        self._initialized = False
        self._bound_secret_value = None
        if process is not None and process.returncode is None:
            terminate_process_tree(process.pid)
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._registered_pid is not None:
            unregister_process(self._registered_pid, status)
            self._registered_pid = None
        if self._stderr_task is not None:
            if not self._stderr_task.done():
                self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

    async def close(self) -> None:
        async with self._lock:
            process = self._process
            if process is None:
                return
            status = "exited"
            if process.returncode is None and process.stdin is not None:
                # MCP defines no shutdown/exit RPC. Closing stdin is the
                # graceful stdio shutdown signal; only escalate after a short
                # bounded wait.
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.stdin.wait_closed(), timeout=min(self.timeout, 3.0))
                except (TimeoutError, AttributeError, BrokenPipeError, ConnectionError, OSError):
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=min(self.timeout, 3.0))
                except TimeoutError:
                    status = "shutdown_timeout"
            if process.returncode is None:
                await self._terminate_unlocked(status)
                return
            self._process = None
            self._initialized = False
            if self._registered_pid is not None:
                unregister_process(self._registered_pid, status)
                self._registered_pid = None
            if self._stderr_task is not None:
                await asyncio.gather(self._stderr_task, return_exceptions=True)
                self._stderr_task = None
