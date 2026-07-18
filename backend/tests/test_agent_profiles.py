from app.agent_profiles import apply_profile_to_plan, filter_profile_tools, get_agent_profile, list_agent_profiles
from app.planning import build_task_plan
from app.tool_registry import BASE_TOOLS


def test_only_base_agent_is_exposed() -> None:
    profiles = {item.id: item for item in list_agent_profiles()}
    assert set(profiles) == {"general"}
    assert profiles["general"].name == "基础Agent"


def test_legacy_profile_alias_maps_to_base_agent_tools() -> None:
    profile = get_agent_profile("file_organizer")
    assert profile is not None
    names = {item["function"]["name"] for item in filter_profile_tools(BASE_TOOLS, profile)}
    assert {"list_files", "move_file", "delete_file"} <= names
    assert "write_file" in names
    assert "run_command" in names


def test_legacy_profile_alias_does_not_add_profile_scope_criterion() -> None:
    profile = get_agent_profile("coding")
    assert profile is not None
    plan = apply_profile_to_plan(build_task_plan("task", "修改 app.py 并运行测试", ("read_file", "write_file", "run_command")), profile)
    assert not any(item.id == "profile_tool_scope" for item in plan.acceptance_criteria)
