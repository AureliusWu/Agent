import sys

# A frozen sidecar re-enters this same executable for cancellable local STT.
# Keep the branch before runtime layout, logging, Uvicorn, and FastAPI setup so
# the child owns no server port and cannot run sidecar startup side effects.
if "--stt-worker" in sys.argv:
    from app.stt.worker import main as stt_worker_main

    worker_index = sys.argv.index("--stt-worker")
    raise SystemExit(stt_worker_main(sys.argv[worker_index + 1 :]))

import asyncio
import ctypes
import logging
import os
import threading

import uvicorn

from app.runtime_paths import ensure_runtime_layout, migrate_legacy_root_database, runtime_layout

layout = ensure_runtime_layout(runtime_layout())
migrate_legacy_root_database(layout)
os.environ.setdefault("AGENT_DATABASE_PATH", str(layout.database))
os.environ.setdefault("AGENT_ALLOW_LOCAL_MCP", "true")
os.environ.setdefault("AGENT_LOG_PATH", str(layout.logs / "agent.log"))
os.environ.setdefault("AGENT_EXTENSION_DIRECTORY", str(layout.extensions))

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
