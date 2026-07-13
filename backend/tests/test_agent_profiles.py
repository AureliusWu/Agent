from app.agent_profiles import apply_profile_to_plan, filter_profile_tools, get_agent_profile, list_agent_profiles
from app.planning import build_task_plan
from app.tool_registry import BASE_TOOLS


def test_builtin_professional_profiles_are_available() -> None:
    profiles = {item.id: item for item in list_agent_profiles()}
    assert {"general", "coding", "data", "documents", "file_organizer"} <= set(profiles)
    assert profiles["coding"].verifier_id == "coding"
    assert profiles["data"].default_permission == "ask"
    assert profiles["file_organizer"].allow_mcp is False


def test_profile_filters_tools_without_expanding_permissions() -> None:
    profile = get_agent_profile("file_organizer")
    assert profile is not None
    names = {item["function"]["name"] for item in filter_profile_tools(BASE_TOOLS, profile)}
    assert {"list_files", "move_file", "delete_file"} <= names
    assert "write_file" not in names
    assert "run_command" not in names


def test_professional_profile_adds_deterministic_scope_criterion() -> None:
    profile = get_agent_profile("coding")
    assert profile is not None
    plan = apply_profile_to_plan(build_task_plan("task", "修改 app.py 并运行测试", ("read_file", "write_file", "run_command")), profile)
    criterion = next(item for item in plan.acceptance_criteria if item.id == "profile_tool_scope")
    assert criterion.parameters["profile_id"] == "coding"
    assert "run_command" in criterion.parameters["tool_allowlist"]

