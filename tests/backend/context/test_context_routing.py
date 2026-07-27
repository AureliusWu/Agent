from pathlib import Path

from app.tools.skills import skill_context
from app.tools.registry import BASE_TOOLS, select_model_tools


def _skill(root: Path, name: str, description: str, body: str) -> None:
    target = root / ".agent" / "skills" / name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}", encoding="utf-8")


def test_skill_context_only_loads_relevant_skill(tmp_path: Path) -> None:
    _skill(tmp_path, "python-tests", "运行 Python pytest 测试", "PYTHON_ONLY_SENTINEL")
    _skill(tmp_path, "image-editor", "编辑图片像素", "IMAGE_ONLY_SENTINEL")
    context = skill_context(str(tmp_path), "请运行 Python pytest 测试")
    assert "PYTHON_ONLY_SENTINEL" in context
    assert "IMAGE_ONLY_SENTINEL" not in context


def test_tool_context_is_bounded_and_routes_relevant_mcp() -> None:
    mcp_tools = [
        {"type": "function", "function": {"name": "mcp__1__search_mail", "description": "搜索邮件", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "mcp__1__create_calendar", "description": "创建日历事件", "parameters": {"type": "object", "properties": {}}}},
    ]
    selected = select_model_tools("修改 app.py 后运行 pytest，并搜索邮件", ["write_file", "run_command"], mcp_tools)
    names = [item["function"]["name"] for item in selected]
    assert len(names) < len(BASE_TOOLS) + len(mcp_tools)
    assert {"read_file", "write_file", "run_command", "mcp__1__search_mail"} <= set(names)
    assert "mcp__1__create_calendar" not in names


def test_tool_context_routes_workspace_intelligence_tools() -> None:
    selected = select_model_tools("查找 calculate 的定义、引用、调用链和相关测试", [], [])
    names = {item["function"]["name"] for item in selected}

    assert {
        "find_symbol",
        "find_definition",
        "find_references",
        "find_related_tests",
        "get_call_chain",
    } <= names

    repo_selected = select_model_tools("先给我这个仓库的代码地图", [], [])
    assert "get_repo_map" in {item["function"]["name"] for item in repo_selected}
