"""Stable, redacted errors for the Artifact Engine.

Format adapters must never surface document content, credentials, or full machine
paths in an error response.  ``ArtifactError`` therefore accepts only structured
details and applies a final defensive redaction before exposing them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import PurePath
from typing import Any

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "content",
    "credential",
    "document",
    "password",
    "secret",
    "text",
    "token",
)
_PATH_KEY_PARTS = ("file", "filename", "path", "source")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_PATH = re.compile(r"^(?:\\\\|//)")
_MAX_SAFE_STRING = 240


def _safe_scalar(value: Any, *, key: str = "") -> str | int | float | bool | None:
    lowered = key.casefold()
    if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, PurePath):
        value = value.name
    rendered = str(value)
    if any(part in lowered for part in _PATH_KEY_PARTS):
        rendered = PurePath(rendered.replace("\\", "/")).name
    elif _WINDOWS_ABSOLUTE.match(rendered) or _UNC_PATH.match(rendered):
        rendered = PurePath(rendered.replace("\\", "/")).name
    if len(rendered) > _MAX_SAFE_STRING:
        rendered = f"{rendered[:_MAX_SAFE_STRING]}..."
    return rendered


def sanitize_details(value: Any, *, _key: str = "", _depth: int = 0) -> Any:
    """Return a bounded JSON-compatible copy suitable for user-visible errors."""

    if _depth >= 5:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= 30:
                safe["_truncated"] = True
                break
            key = str(raw_key)[:80]
            safe[key] = sanitize_details(raw_value, _key=key, _depth=_depth + 1)
        return safe
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)
        safe_items = [
            sanitize_details(item, _key=_key, _depth=_depth + 1)
            for item in items[:30]
        ]
        if len(items) > 30:
            safe_items.append("[TRUNCATED]")
        return safe_items
    return _safe_scalar(value, key=_key)


class ArtifactError(Exception):
    """A stable Artifact Engine failure with a safe public payload."""

    def __init__(
        self,
        error_code: str,
        phase: str,
        message: str | None = None,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.error_code = str(error_code)
        self.phase = str(phase)
        self.message = message or self.error_code.replace("_", " ")
        sanitized = sanitize_details(details or {})
        self.safe_details: dict[str, Any] = (
            sanitized if isinstance(sanitized, dict) else {"value": sanitized}
        )
        super().__init__(self.message)

    @property
    def code(self) -> str:
        """Compatibility alias for consumers that use ``code``."""

        return self.error_code

    @property
    def details(self) -> dict[str, Any]:
        """Compatibility alias that remains redacted."""

        return dict(self.safe_details)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "phase": self.phase,
            "message": self.message,
            "details": dict(self.safe_details),
        }
