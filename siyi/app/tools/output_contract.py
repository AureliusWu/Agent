"""Opt-in output contracts at the trusted Executor boundary.

These are JSON Schema subset contracts, not arbitrary validator callbacks.
An invalid post-dispatch result does not prove that its side effect failed.
"""

from __future__ import annotations

from typing import Any

from app.providers.schema_validation import (
    StructuredOutputError, prepare_schema, snapshot_output, validate_output,
)


# File reads repeat content in data and at the top level. A valid 200k-character
# Unicode read can exceed the Provider's distinct 1 MiB response limit.
MAX_TOOL_OUTPUT_BYTES = 2 * 1024 * 1024
CONTRACT_TOOLS = frozenset({"read_file", "read_file_range", "create_file", "write_file", "run_command"})


def envelope_schema() -> dict[str, Any]:
    """Version-one envelope; additional legacy/tool-specific fields are retained."""
    return {
        "type": "object", "required": ["success", "status"],
        "properties": {
            "success": {"type": "boolean"},
            "status": {"type": "string", "minLength": 1},
            "data": {"type": "object"},
            "error_code": {"type": ["string", "null"]},
            "error_message": {"type": ["string", "null"]},
            "retryable": {"type": "boolean"},
            "truncated": {"type": "boolean"},
            "metadata": {
                "type": "object",
                "properties": {"duration_ms": {"type": "integer", "minimum": 0}},
            },
        },
    }


class ToolOutputContractError(RuntimeError):
    """Fixed, non-sensitive error; never quote malformed tool output or schema."""

    def __init__(self, reason_code: str, *, dispatch_started: bool) -> None:
        self.reason_code = reason_code
        self.dispatch_started = dispatch_started
        super().__init__(
            "工具执行后的输出未通过契约校验；任务已停止，操作结果需要核对，不能自动重试。"
            if dispatch_started else "工具输出契约配置无效；任务已停止，本次工具未执行。"
        )


def prepare_tool_output_schema(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    try:
        return prepare_schema(schema)
    except StructuredOutputError as exc:
        raise ToolOutputContractError(exc.code, dispatch_started=False) from None


def validate_tool_output(
    result: Any, schema: dict[str, Any] | None, *, tool_name: str = "",
) -> dict[str, Any]:
    if schema is None:
        return result
    try:
        detached = snapshot_output(result, max_bytes=MAX_TOOL_OUTPUT_BYTES)
        # A custom schema may narrow this contract, never remove the host's
        # envelope requirements (including fields consumed by receipt code).
        validate_output(detached, envelope_schema())
        validate_output(detached, schema)
        if (detached.get("success") is True and (
            detached.get("status") != "ok" or detached.get("error_code")
        )) or (detached.get("success") is False and detached.get("status") == "ok"):
            raise StructuredOutputError("schema_validation_failed")
        # These fields feed the host's receipt even in flattened legacy results.
        data = detached.get("data", detached)
        for field in ("exit_code", "total_bytes"):
            value = data.get(field)
            if value is not None and type(value) is not int:
                raise StructuredOutputError("schema_validation_failed")
        if tool_name == "run_command":
            for container in (data, detached):
                exit_code = container.get("exit_code")
                if exit_code is not None and (
                    type(exit_code) is not int or (detached["success"] and exit_code != 0)
                ):
                    raise StructuredOutputError("schema_validation_failed")
        return detached
    except StructuredOutputError as exc:
        raise ToolOutputContractError(exc.code, dispatch_started=True) from None
