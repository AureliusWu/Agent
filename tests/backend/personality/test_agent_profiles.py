from app.personality.agent_profiles import apply_profile_to_plan, filter_profile_tools, get_agent_profile, list_agent_profiles
from app.cognition.planning import build_task_plan
from app.tools.registry import BASE_TOOLS


def test_builtin_professional_agents_are_exposed() -> None:
    profiles = {item.id: item for item in list_agent_profiles()}
    assert {"general", "coding", "data", "documents", "file_organizer"} <= set(profiles)
    assert profiles["general"].name == "基础Agent"


def test_file_organizer_profile_has_scoped_tools() -> None:
    profile = get_agent_profile("file_organizer")
    assert profile is not None
    names = {item["function"]["name"] for item in filter_profile_tools(BASE_TOOLS, profile)}
    assert {"list_files", "move_file", "delete_file"} <= names
    assert "write_file" not in names
    assert "run_command" not in names


def test_professional_profile_adds_scope_criterion() -> None:
    profile = get_agent_profile("coding")
    assert profile is not None
    plan = apply_profile_to_plan(build_task_plan("task", "修改 app.py 并运行测试", ("read_file", "write_file", "run_command")), profile)
    assert any(item.id == "profile_tool_scope" for item in plan.acceptance_criteria)
