import json
import uuid
from pathlib import Path

from app.database import connect, now_iso
from app.sandbox import execute_tool
from app.verification import detect_project, verify_task


def prepare_task(tmp_path: Path, prompt: str = "修改文件") -> tuple[int, str]:
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    now = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "verification", str(tmp_path), "agent", now, now),
        )
        db.execute(
            "INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", prompt, now, now),
        )
    return conversation_id, task_id


def test_answer_only_task_is_verified_without_mutating_workspace(tmp_path: Path) -> None:
    _, task_id = prepare_task(tmp_path, "解释这个概念")
    report = verify_task(task_id, str(tmp_path), "解释这个概念", "有效回答")
    assert report["status"] == "passed"
    assert "content" not in report["verifier_input"]["response"]
    assert report["requirements_failed"] == []
    assert not (tmp_path / ".agent-backups").exists()


def test_read_only_project_summary_may_mention_tests_without_running_them(tmp_path: Path) -> None:
    conversation_id, task_id = prepare_task(tmp_path, "说明项目结构和测试位置，不要修改文件")
    (tmp_path / "README.md").write_text("tests live in tests/", encoding="utf-8")
    now = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO tool_runs(conversation_id, task_id, source, risk, confirmed, tool, status, input, output, started_at, finished_at, duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (conversation_id, task_id, "builtin", "low", 0, "read_file", "ok", json.dumps({"path": "README.md"}), json.dumps({"data": {"content": "tests live in tests/"}}), now, now, 1),
        )
    report = verify_task(task_id, str(tmp_path), "说明项目结构和测试位置，不要修改文件", "测试位于 tests 目录")
    assert report["status"] == "passed"


def test_text_change_is_verified_by_current_file_hash(tmp_path: Path) -> None:
    _, task_id = prepare_task(tmp_path)
    execute_tool(str(tmp_path), "agent", "create_file", {"path": "README.md", "content": "done"}, task_id=task_id, tool_call_id="write")
    report = verify_task(task_id, str(tmp_path), "修改说明", "完成")
    assert report["status"] == "passed"
    assert report["modified_files"] == ["README.md"]


def test_code_change_requires_a_real_verification_command(tmp_path: Path) -> None:
    conversation_id, task_id = prepare_task(tmp_path)
    execute_tool(str(tmp_path), "agent", "create_file", {"path": "main.py", "content": "print('ok')"}, task_id=task_id, tool_call_id="write")
    partial = verify_task(task_id, str(tmp_path), "修改代码", "完成")
    assert partial["status"] == "partially_passed"

    now = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO tool_runs(conversation_id, task_id, source, risk, confirmed, tool, status, input, output, started_at, finished_at, duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (conversation_id, task_id, "builtin", "critical", 1, "run_command", "ok", json.dumps({"command": "python", "args": ["-m", "pytest"]}), json.dumps({"exit_code": 0}), now, now, 20),
        )
    passed = verify_task(task_id, str(tmp_path), "修改代码", "完成")
    assert passed["status"] == "passed"


def test_confirmation_request_is_not_counted_as_failed_command(tmp_path: Path) -> None:
    conversation_id, task_id = prepare_task(tmp_path)
    execute_tool(str(tmp_path), "agent", "create_file", {"path": "main.py", "content": "print('ok')"}, task_id=task_id, tool_call_id="write")
    now = now_iso()
    command = json.dumps({"command": "python", "args": ["-m", "pytest"]})
    with connect() as db:
        db.execute(
            "INSERT INTO tool_runs(conversation_id, task_id, source, risk, confirmed, tool, status, input, output, started_at, finished_at, duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (conversation_id, task_id, "builtin", "critical", 0, "run_command", "confirmation_required", command, "{}", now, now, 1),
        )
        db.execute(
            "INSERT INTO tool_runs(conversation_id, task_id, source, risk, confirmed, tool, status, input, output, started_at, finished_at, duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (conversation_id, task_id, "builtin", "critical", 1, "run_command", "ok", command, json.dumps({"exit_code": 0}), now, now, 20),
        )
    report = verify_task(task_id, str(tmp_path), "修改代码并运行测试", "完成")
    command_checks = [item for item in report["checks"] if item["kind"] == "verification_command"]
    assert report["status"] == "passed"
    assert len(command_checks) == 1
    assert command_checks[0]["status"] == "passed"


def test_project_detector_reads_known_commands(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest","build":"vite build"}}', encoding="utf-8")
    project = detect_project(str(tmp_path))
    assert project["types"] == ["node"]
    assert [item["name"] for item in project["commands"]] == ["test", "build"]


def test_unavailable_hardware_is_blocked_without_false_completion(tmp_path: Path) -> None:
    _, task_id = prepare_task(tmp_path, "读取当前未连接的专用硬件温度并写入 result.json；没有设备接口时诚实阻塞。")
    report = verify_task(task_id, str(tmp_path), "读取当前未连接的专用硬件温度并写入 result.json；没有设备接口时诚实阻塞。", "没有可用硬件接口。")
    assert report["status"] == "blocked"
    assert report["retry_recommended"] is False
    assert not (tmp_path / "result.json").exists()


def test_strict_scope_detects_unrelated_file_changes(tmp_path: Path) -> None:
    _, task_id = prepare_task(tmp_path, "只修改 config.json，不要修改其他文件。")
    execute_tool(str(tmp_path), "agent", "create_file", {"path": "config.json", "content": "{}"}, task_id=task_id, tool_call_id="config")
    execute_tool(str(tmp_path), "agent", "create_file", {"path": "unrelated.txt", "content": "oops"}, task_id=task_id, tool_call_id="extra")
    report = verify_task(task_id, str(tmp_path), "只修改 config.json，不要修改其他文件。", "完成")
    assert report["status"] == "failed"
    assert report["side_effects"] == ["unrelated.txt"]
    assert "scope_control" in report["retry_scope"]
