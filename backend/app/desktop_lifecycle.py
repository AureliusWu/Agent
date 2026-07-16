from __future__ import annotations

from collections.abc import Callable
from threading import Lock


_handler_lock = Lock()
_shutdown_handler: Callable[[], None] | None = None


def install_shutdown_handler(handler: Callable[[], None] | None) -> None:
    """Register the process-level shutdown hook supplied by the sidecar host."""
    global _shutdown_handler
    with _handler_lock:
        _shutdown_handler = handler


def request_shutdown() -> bool:
    with _handler_lock:
        handler = _shutdown_handler
    if handler is None:
        return False
    handler()
    return True
