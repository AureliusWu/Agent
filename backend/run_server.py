import asyncio
import ctypes
import logging
import os
import threading
from pathlib import Path

import uvicorn

local_data = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AureliusWu" / "Agent"
local_data.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("AGENT_DATABASE_PATH", str(local_data / "agent.db"))
os.environ.setdefault("AGENT_ALLOW_LOCAL_MCP", "true")
os.environ.setdefault("AGENT_LOG_PATH", str(local_data / "logs" / "agent.log"))

from app.main import app  # noqa: E402
from app.config import settings  # noqa: E402
from app.desktop_lifecycle import install_shutdown_handler  # noqa: E402


def _wait_for_windows_process_exit(pid: int) -> bool:
    if os.name != "nt" or pid <= 0:
        return False
    synchronize = 0x00100000
    infinite = 0xFFFFFFFF
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return True
    try:
        return kernel32.WaitForSingleObject(handle, infinite) == 0
    finally:
        kernel32.CloseHandle(handle)


def _watch_parent(server: uvicorn.Server, parent_pid: int, loop: asyncio.AbstractEventLoop) -> None:
    if not _wait_for_windows_process_exit(parent_pid):
        return
    try:
        loop.call_soon_threadsafe(setattr, server, "should_exit", True)
    except RuntimeError:
        return


async def serve() -> None:
    config = uvicorn.Config(
        app,
        host=settings.bind_host,
        port=int(os.environ.get("AGENT_PORT", "8000")),
        log_level="warning",
    )
    server = uvicorn.Server(config)
    install_shutdown_handler(lambda: setattr(server, "should_exit", True))
    parent_pid = int(os.environ.get("AGENT_PARENT_PID", "0") or 0)
    if parent_pid > 0:
        threading.Thread(
            target=_watch_parent,
            args=(server, parent_pid, asyncio.get_running_loop()),
            name="agent-parent-watch",
            daemon=True,
        ).start()
    try:
        await server.serve()
    finally:
        install_shutdown_handler(None)
        logging.getLogger("agent.desktop").info("desktop sidecar stopped")


if __name__ == "__main__":
    asyncio.run(serve())
