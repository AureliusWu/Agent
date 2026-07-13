from pathlib import Path

from app.skills import discover_skills, install_skill, skill_context


def test_skill_install_discovery_and_prompt_selection(tmp_path: Path) -> None:
    install_skill(str(tmp_path), "review", "---\nname: review\ndescription: 检查代码风险\n---\n请先读取测试。")
    discovered = discover_skills(str(tmp_path), include_content=True)
    assert discovered[0]["name"] == "review"
    assert discovered[0]["enabled"] is True
    assert "请先读取测试" in skill_context(str(tmp_path), "请 review 当前代码")
