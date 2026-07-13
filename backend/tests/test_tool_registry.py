import pytest

from app.tool_registry import ToolValidationError, requires_confirmation, validate_arguments


def test_tool_schema_rejects_unknown_and_missing_arguments() -> None:
    with pytest.raises(ToolValidationError, match="缺少必填参数"):
        validate_arguments("read_file", {})
    with pytest.raises(ToolValidationError, match="不允许的参数"):
        validate_arguments("read_file", {"path": "a.txt", "shell": True})


def test_permission_risk_matrix() -> None:
    assert requires_confirmation("ask", "medium") is True
    assert requires_confirmation("agent", "medium") is False
    assert requires_confirmation("agent", "high") is True
    assert requires_confirmation("full", "high") is False
    assert requires_confirmation("full", "critical") is True
