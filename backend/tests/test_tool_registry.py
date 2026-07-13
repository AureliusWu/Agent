import pytest

from app.tool_registry import REGISTRY, ToolValidationError, requires_confirmation, validate_arguments


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


def test_extended_schema_validates_boolean_enum_and_string_length() -> None:
    validate_arguments("search_text", {"query": "needle", "regex": True, "max_results": 10})
    validate_arguments("read_file", {"path": "a.txt", "encoding": "gb18030"})
    with pytest.raises(ToolValidationError, match="允许范围"):
        validate_arguments("read_file", {"path": "a.txt", "encoding": "invalid"})
    with pytest.raises(ToolValidationError, match="长度"):
        validate_arguments("search_text", {"query": "x" * 1001})


def test_tool_catalog_exposes_runtime_contract() -> None:
    catalog = REGISTRY["remember_workspace"].catalog()
    assert catalog["source"] == "builtin"
    assert catalog["risk_level"] == "medium"
    assert catalog["timeout"] > 0
    assert catalog["input_schema"]["additionalProperties"] is False
