import pytest

from app.tools.registry import BASE_TOOLS, REGISTRY, ToolValidationError, filter_readonly_tools, requires_confirmation, validate_arguments


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


def test_v12_tool_catalog_declares_reliability_contract() -> None:
    write = REGISTRY["write_file"].catalog()
    command = REGISTRY["run_command"].catalog()

    assert write["version"] == "1.0"
    assert write["idempotent"] is False
    assert write["rollback_support"] is True
    assert {"exists", "hash", "diff"} <= set(write["verification_support"])
    assert write["permission_level"] == "L1_WORKSPACE_WRITE"
    assert command["permission_level"] == "L2_PROCESS_EXECUTION"
    assert command["verification_support"] == ["process_exit", "test_command"]


def test_workspace_index_tools_have_low_risk_contracts() -> None:
    for name in (
        "get_repo_map",
        "find_symbol",
        "find_definition",
        "find_references",
        "list_module_dependencies",
        "find_related_tests",
        "get_call_chain",
        "inspect_diagnostics",
    ):
        assert REGISTRY[name].risk == "low"


def test_readonly_catalog_contains_reads_and_excludes_every_mutation() -> None:
    names = {
        item["function"]["name"]
        for item in filter_readonly_tools(BASE_TOOLS)
    }

    assert {"list_files", "search_files", "read_file"} <= names
    assert not {
        "create_file",
        "write_file",
        "replace_text",
        "apply_patch",
        "delete_file",
        "run_command",
        "remember_workspace",
    } & names
