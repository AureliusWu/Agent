from pathlib import Path

from app.database import connect, now_iso
from app.skills import discover_skills, install_skill, skill_context


def test_skill_install_discovery_and_prompt_selection(tmp_path: Path) -> None:
    install_skill(str(tmp_path), "review", "---\nname: review\ndescription: 检查代码风险\n---\n请先读取测试。")
    discovered = discover_skills(str(tmp_path), include_content=True)
    assert discovered[0]["name"] == "review"
    assert discovered[0]["enabled"] is True
    assert "请先读取测试" in skill_context(str(tmp_path), "请 review 当前代码")


def test_skills_are_not_loaded_without_relevance_and_usage_is_recorded(tmp_path: Path) -> None:
    install_skill(str(tmp_path), "review", "---\nname: review\ndescription: 检查代码风险\n---\n只在命中时加载。")
    assert "本轮相关 Skill 指令" not in skill_context(str(tmp_path), "写一首诗")
    now = now_iso()
    with connect() as db:
        conversation = db.execute("INSERT INTO conversations(title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?)", ("skill", str(tmp_path), "ask", now, now)).lastrowid
        db.execute("INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)", ("skill-task", conversation, "running", "review", now, now))
    skill_context(str(tmp_path), "review 当前代码", "skill-task")
    with connect() as db:
        run = db.execute("SELECT name FROM skill_runs WHERE task_id='skill-task'").fetchone()
    assert run["name"] == "review"
