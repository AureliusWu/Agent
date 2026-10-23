"""Cross-tool recovery needs actual, task-bound file and command evidence.

File operations and successful verification commands below use the production
Executor, permission broker, SQLite task store and durable backup manifests.
Deliberately corrupted rows are hostile unit fixtures, never release evidence.
No model, network, credentials, installation or administrator action is used.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import pytest

from app.cognition.planning import build_task_plan
from app.database import connect, now_iso
from app.kernel.adapters import SqliteTaskStore
from app.permissions import authorize
from app.runtime.executor import ExecutorToolCall, LocalWindowsExecutor
from app.runtime.verification import verify_task
from app.sandbox import file_version_token, verify_task_changes
from app.tools.registry import REGISTRY


OLD = "VALUE = 1\n"
NEW = "VALUE = 2\n"
PROMPT = "只修改 main.py 中的代码并运行测试验证，不要修改其他文件。"
TOOLS = ("replace_text", "write_file", "run_command", "undo_file_change")


@dataclass(frozen=True)
class Scenario:
    root: Path
    conversation_id: int
    task_id: str


@dataclass(frozen=True)
class Recorded:
    run_id: int
    execution_id: str
    tool_call_id: str
    result: dict[str, Any]


def _task(root: Path) -> Scenario:
    stamp = now_iso()
    task_id = uuid.uuid4().hex
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("cross-tool-recovery", str(root), "full", stamp, stamp),
        )
        conversation_id = int(cursor.lastrowid)
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", PROMPT, stamp, stamp),
        )
    return Scenario(root, conversation_id, task_id)


def _checker(root: Path, expected: str = NEW) -> None:
    (root / "check.py").write_text(
        "from pathlib import Path\n"
        f"assert Path('main.py').read_text(encoding='utf-8') == {expected!r}\n"
        "print('RECOVERY_CONTENT_CHECK_OK')\n",
        encoding="utf-8",
    )


@pytest.fixture
def scenario(tmp_path: Path) -> Scenario:
    # Fixed LF fixture bytes: Windows write_text would silently seed CRLF.
    (tmp_path / "main.py").write_bytes(OLD.encode("utf-8"))
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'isolated-recovery-fixture'\nversion = '0.0.0'\n",
        encoding="utf-8",
    )
    _checker(tmp_path)
    return _task(tmp_path)


def _execute(scenario: Scenario, tool: str, arguments: dict[str, Any], *, persist: bool = True) -> Recorded:
    """Run the real Executor, acknowledging only this exact fixture operation."""
    call_id, execution_id = uuid.uuid4().hex, uuid.uuid4().hex
    permission = authorize(
        mode="full", risk=REGISTRY[tool].risk, tool=tool, arguments=arguments,
        conversation_id=scenario.conversation_id, task_id=scenario.task_id,
        workspace=str(scenario.root), approval_scope="once",
    )
    tokens: list[str] = []
    if not permission.allowed:
        confirmation = permission.confirmation or {}
        assert confirmation.get("status") == "confirmation_required", confirmation
        assert isinstance(confirmation.get("approval_key"), str)
        tokens.append(confirmation["approval_key"])
    call = ExecutorToolCall(
        workspace=str(scenario.root), mode="full", name=tool, arguments=arguments,
        tool_call_id=call_id, approved_actions=tokens, approval_scope="once",
        conversation_id=scenario.conversation_id, task_id=scenario.task_id,
        mcp_routes={}, available_tool_names=TOOLS,
    )
    started, started_perf = now_iso(), time.perf_counter()
    outcome = asyncio.run(LocalWindowsExecutor().execute_tool(call))
    assert outcome.receipt is not None
    assert outcome.result["receipt"] == outcome.receipt.as_dict()
    assert outcome.receipt.task_id == scenario.task_id
    assert outcome.receipt.tool_call_id == call_id
    run_id = 0
    if persist:
        SqliteTaskStore().record_tool_run(
            conversation_id=scenario.conversation_id, task_id=scenario.task_id,
            tool=tool, arguments=arguments, result=outcome.result,
            started=started, started_perf=started_perf, risk=outcome.risk,
            confirmed=outcome.confirmed, source=outcome.source, execution_id=execution_id,
        )
        with connect() as db:
            row = db.execute("SELECT id FROM tool_runs WHERE execution_id=?", (execution_id,)).fetchone()
            assert row is not None
            run_id = int(row["id"])
            receipt = db.execute(
                "SELECT receipt_json FROM tool_receipts WHERE receipt_id=? AND task_id=?",
                (outcome.receipt.receipt_id, scenario.task_id),
            ).fetchone()
            assert receipt is not None
            assert json.loads(receipt["receipt_json"])["tool_call_id"] == call_id
    return Recorded(run_id, execution_id, call_id, outcome.result)


def _failed_replace(scenario: Scenario, path: str = "./main.py") -> Recorded:
    before = (scenario.root / "main.py").read_bytes()
    version = file_version_token(scenario.root / "main.py")
    failed = _execute(scenario, "replace_text", {
        "path": path, "old_text": "ABSENT_LITERAL", "new_text": "replacement",
        "expected_count": 1, "expected_version_token": version,
    })
    assert failed.result["success"] is False
    assert failed.result["error_code"] == "edit_match_precondition"
    assert failed.result["data"]["version_before"] == version
    assert failed.result["data"]["edit_precondition"] == {
        "schema_version": 1, "kind": "replace_text_match_count", "path": "main.py",
        "expected_count": 1, "actual_count": 0, "side_effect": "not_started",
    }
    assert not failed.result["receipt"]["changed_files"]
    assert failed.result["receipt"]["change_id"] is None
    assert failed.result["receipt"]["success"] is False
    assert (scenario.root / "main.py").read_bytes() == before
    assert file_version_token(scenario.root / "main.py") == version
    assert not (scenario.root / ".agent-backups").exists()
    return failed


def _write(scenario: Scenario, *, path: str = "main.py", content: str = NEW, dry_run: bool = False) -> Recorded:
    target = scenario.root / path
    result = _execute(scenario, "write_file", {
        "path": path, "content": content, "expected_version_token": file_version_token(target),
        **({"dry_run": True} if dry_run else {}),
    })
    assert result.result["success"] is True, result.result
    if not dry_run:
        assert target.read_text(encoding="utf-8") == content
        assert result.result["receipt"]["change_id"]
    return result


def _command(
    scenario: Scenario, *, unrelated: bool = False, affected_paths: list[str] | None = None,
) -> Recorded:
    if unrelated:
        (scenario.root / "test_unrelated.py").write_text("assert 2 + 2 == 4\n", encoding="utf-8")
    declared = (["other.py"] if unrelated else ["main.py"]) if affected_paths is None else affected_paths
    return _execute(scenario, "run_command", {
        "command": "python", "args": ["-B", "test_unrelated.py" if unrelated else "check.py"],
        "cwd": ".", "affected_paths": declared, "timeout": 15,
    })


def _completed(scenario: Scenario) -> tuple[Recorded, Recorded, Recorded]:
    failed = _failed_replace(scenario)
    written = _write(scenario)
    command = _command(scenario)
    assert command.result["success"] is True, command.result
    assert type(command.result["exit_code"]) is int and command.result["exit_code"] == 0
    assert "RECOVERY_CONTENT_CHECK_OK" in command.result["stdout"]
    assert failed.run_id < written.run_id < command.run_id
    return failed, written, command


def _report(scenario: Scenario, *, expects_failure_handling: bool = False) -> dict[str, Any]:
    plan = replace(
        build_task_plan(scenario.task_id, PROMPT, TOOLS),
        expects_failure_handling=expects_failure_handling,
    )
    return verify_task(scenario.task_id, str(scenario.root), plan, "文件和测试证据由独立验证器核对。")


def _trace(report: dict[str, Any], failed: Recorded) -> dict[str, Any]:
    return next(
        item for item in report["verifier_input"]["failure_traces"]
        if item["failed_run_id"] == failed.run_id
    )


def _assert_unresolved(scenario: Scenario, failed: Recorded, *, expects_failure_handling: bool = False) -> None:
    report = _report(scenario, expects_failure_handling=expects_failure_handling)
    trace = _trace(report, failed)
    assert trace["status"] == "unresolved", trace
    assert trace["repaired_by_run_id"] is None
    assert report["status"] != "passed"


def _edit_output(recorded: Recorded, change: Callable[[dict[str, Any]], None], *, receipt_copy: bool = True) -> None:
    """Corrupt only this isolated hostile fixture, optionally keeping both receipt copies equal."""
    with connect() as db:
        row = db.execute("SELECT output FROM tool_runs WHERE id=?", (recorded.run_id,)).fetchone()
        output = json.loads(row["output"])
        change(output)
        db.execute("UPDATE tool_runs SET output=? WHERE id=?", (json.dumps(output), recorded.run_id))
        receipt = output.get("receipt")
        if receipt_copy and isinstance(receipt, dict) and receipt.get("receipt_id"):
            db.execute(
                "UPDATE tool_receipts SET receipt_json=? WHERE receipt_id=?",
                (json.dumps(receipt), receipt["receipt_id"]),
            )


def _manifest(scenario: Scenario, written: Recorded) -> Path:
    return scenario.root / ".agent-backups" / written.result["receipt"]["change_id"] / "manifest.json"


@pytest.mark.parametrize("failure_path", ["main.py", "./main.py"])
def test_cross_tool_recovery_uses_actual_file_receipts_hash_and_later_command(scenario: Scenario, failure_path: str) -> None:
    failed = _failed_replace(scenario, failure_path)
    written = _write(scenario)
    command = _command(scenario)
    assert command.result["success"] is True, command.result
    assert type(command.result["exit_code"]) is int and command.result["exit_code"] == 0
    assert failed.run_id < written.run_id < command.run_id
    version = failed.result["data"]["version_before"]
    assert written.result["receipt"]["version_before"] == version
    manifest = json.loads(_manifest(scenario, written).read_text(encoding="utf-8"))
    assert manifest["id"] == written.result["receipt"]["change_id"]
    assert manifest["task_id"] == scenario.task_id
    assert manifest["tool_call_id"] == written.tool_call_id
    assert manifest["operation"] == "write_file"
    assert len(manifest["entries"]) == 1 and manifest["entries"][0]["path"] == "main.py"
    assert manifest["entries"][0]["before"]["sha256"] == hashlib.sha256(OLD.encode()).hexdigest()
    assert manifest["entries"][0]["after"]["sha256"] == hashlib.sha256(NEW.encode()).hexdigest()
    journal = verify_task_changes(str(scenario.root), scenario.task_id)
    assert journal["status"] == "passed"
    assert journal["checks"][0]["actual"]["sha256"] == hashlib.sha256(NEW.encode()).hexdigest()
    report = _report(scenario)
    trace = _trace(report, failed)
    assert trace["status"] == "resolved"
    assert trace["repaired_by_run_id"] == written.run_id
    assert report["status"] == "passed", report["requirements_failed"]
    checks = {item["requirement_id"]: item for item in report["checks"]}
    assert checks["verify_code"]["status"] == "passed"
    assert checks["verification_command"]["status"] == "passed"


def test_cross_tool_recovery_does_not_relabel_failed_run_as_success(scenario: Scenario) -> None:
    failed, written, _ = _completed(scenario)
    report = _report(scenario)
    assert _trace(report, failed)["repaired_by_run_id"] == written.run_id
    with connect() as db:
        run = db.execute("SELECT status,output FROM tool_runs WHERE id=?", (failed.run_id,)).fetchone()
        output = json.loads(run["output"])
        receipt = db.execute(
            "SELECT status,receipt_json FROM tool_receipts WHERE receipt_id=?",
            (output["receipt"]["receipt_id"],),
        ).fetchone()
    assert run["status"] == "error" and output["success"] is False
    assert output["error_code"] == "edit_match_precondition"
    assert receipt["status"] == "FAILED" and json.loads(receipt["receipt_json"])["success"] is False


def test_cross_tool_recovery_accepts_real_target_assertion_with_empty_affected_paths(scenario: Scenario) -> None:
    # Historical commands may honestly declare no anticipated mutations. Scope
    # is established by the actual target assertion, not this declaration alone.
    failed = _failed_replace(scenario)
    written = _write(scenario)
    command = _command(scenario, affected_paths=[])
    assert command.result["success"] is True, command.result
    assert type(command.result["exit_code"]) is int and command.result["exit_code"] == 0
    report = _report(scenario)
    assert _trace(report, failed)["status"] == "resolved"
    assert _trace(report, failed)["repaired_by_run_id"] == written.run_id
    assert report["status"] == "passed", report["requirements_failed"]


@pytest.mark.parametrize("script", [
    "unused_target = 'main.py'\nassert 2 + 2 == 4\n",
    "if False:\n    import main\nassert True\n",
    "def not_called():\n    import main\n    assert main.VALUE == 2\nassert True\n",
    "from pathlib import Path\nassert Path('main.py')\n",
    "from pathlib import Path\nassert print(Path('main.py')) is None\n",
    "from pathlib import Path\nassert Path('main.py') or Path('main.py').read_text() == 'WRONG'\n",
    "from pathlib import Path\nassert not (1 == 2 == Path('main.py').read_text())\n",
    "import main as p\nfrom pathlib import Path as p\nassert p.cwd\n",
], ids=["unused-string", "dead-import", "uncalled-function", "path-truthiness", "print-argument", "short-circuited-read", "short-circuited-comparison", "rebound-target-alias"])
def test_cross_tool_recovery_rejects_real_exit_zero_without_executed_target_assertion(
    scenario: Scenario, script: str,
) -> None:
    failed = _failed_replace(scenario)
    _write(scenario)
    (scenario.root / "check.py").write_bytes(script.encode("utf-8"))
    command = _command(scenario)
    assert command.result["success"] is True, command.result
    assert type(command.result["exit_code"]) is int and command.result["exit_code"] == 0
    _assert_unresolved(scenario, failed)


def test_cross_tool_recovery_does_not_retroactively_bind_a_rewritten_check_script(scenario: Scenario) -> None:
    # The earlier process really succeeds, but it did not inspect the target.
    # A later recorded script edit cannot change what that process executed.
    (scenario.root / "check.py").write_bytes(b"assert True\n")
    failed = _failed_replace(scenario)
    _write(scenario)
    command = _command(scenario)
    assert command.result["success"] is True, command.result
    assert type(command.result["exit_code"]) is int and command.result["exit_code"] == 0
    script = (
        "from pathlib import Path\n"
        f"assert Path('main.py').read_text(encoding='utf-8') == {NEW!r}\n"
    )
    rewritten = _write(scenario, path="check.py", content=script)
    assert command.run_id < rewritten.run_id
    assert verify_task_changes(str(scenario.root), scenario.task_id)["status"] == "passed"
    _assert_unresolved(scenario, failed)


def test_cross_tool_recovery_rejects_write_from_another_task(scenario: Scenario) -> None:
    failed = _failed_replace(scenario)
    other = _task(scenario.root)
    _write(other)
    assert _command(scenario).result["success"] is True
    _assert_unresolved(scenario, failed)


def test_cross_tool_recovery_rejects_same_basename_in_another_directory(scenario: Scenario) -> None:
    failed = _failed_replace(scenario)
    (scenario.root / "nested").mkdir()
    (scenario.root / "nested/main.py").write_bytes(OLD.encode("utf-8"))
    _write(scenario, path="nested/main.py")
    _assert_unresolved(scenario, failed)


@pytest.mark.parametrize("side", ["failed", "write", "command"])
@pytest.mark.parametrize("source", ["mcp", "extension:untrusted", "plugin:untrusted"])
def test_cross_tool_recovery_rejects_untrusted_source(scenario: Scenario, side: str, source: str) -> None:
    failed, written, command = _completed(scenario)
    item = {"failed": failed, "write": written, "command": command}[side]
    with connect() as db:
        db.execute("UPDATE tool_runs SET source=? WHERE id=?", (source, item.run_id))
    _assert_unresolved(scenario, failed)


@pytest.mark.parametrize("side", ["failed", "write", "command"])
def test_cross_tool_recovery_requires_embedded_and_persisted_receipt(scenario: Scenario, side: str) -> None:
    failed, written, command = _completed(scenario)
    item = {"failed": failed, "write": written, "command": command}[side]
    _edit_output(item, lambda value: value.pop("receipt"), receipt_copy=False)
    _assert_unresolved(scenario, failed)


@pytest.mark.parametrize("side", ["failed", "write", "command"])
def test_cross_tool_recovery_requires_separately_persisted_receipt(scenario: Scenario, side: str) -> None:
    failed, written, command = _completed(scenario)
    item = {"failed": failed, "write": written, "command": command}[side]
    with connect() as db:
        db.execute("DELETE FROM tool_receipts WHERE receipt_id=?", (item.result["receipt"]["receipt_id"],))
    _assert_unresolved(scenario, failed)


@pytest.mark.parametrize("field", ["task_id", "tool_call_id", "tool", "change_id", "version_before"])
def test_cross_tool_recovery_rejects_misbound_write_receipt(scenario: Scenario, field: str) -> None:
    failed, written, _ = _completed(scenario)
    _edit_output(written, lambda value: value["receipt"].__setitem__(field, "wrong-fixture-binding"))
    _assert_unresolved(scenario, failed)


@pytest.mark.parametrize("field", ["task_id", "tool_call_id", "operation", "id", "path", "after_hash"])
def test_cross_tool_recovery_requires_matching_durable_manifest(scenario: Scenario, field: str) -> None:
    failed, written, _ = _completed(scenario)
    path = _manifest(scenario, written)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if field == "path":
        manifest["entries"][0]["path"] = "other.py"
    elif field == "after_hash":
        manifest["entries"][0]["after"]["sha256"] = "0" * 64
    elif field == "id":
        # Remain syntactically valid so the journal reader reaches binding
        # validation rather than failing to parse a timestamp-shaped ID.
        prefix, suffix = manifest["id"].split("-", 1)
        manifest["id"] = prefix + ("-00000000" if suffix != "00000000" else "-11111111")
    else:
        manifest[field] = "wrong-fixture-binding"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    _assert_unresolved(scenario, failed)


def test_cross_tool_recovery_rejects_missing_manifest(scenario: Scenario) -> None:
    failed, written, _ = _completed(scenario)
    _manifest(scenario, written).unlink()
    _assert_unresolved(scenario, failed)


def test_cross_tool_recovery_rejects_file_drift_after_successful_command(scenario: Scenario) -> None:
    failed, _, _ = _completed(scenario)
    (scenario.root / "main.py").write_bytes(b"VALUE = 3\n")
    _assert_unresolved(scenario, failed)


def test_cross_tool_recovery_rejects_dry_run_as_repair(scenario: Scenario) -> None:
    failed = _failed_replace(scenario)
    written = _write(scenario, dry_run=True)
    assert written.result["dry_run"] is True
    assert (scenario.root / "main.py").read_text(encoding="utf-8") == OLD
    _assert_unresolved(scenario, failed)


def test_cross_tool_recovery_rejects_failed_fallback_write(scenario: Scenario) -> None:
    failed = _failed_replace(scenario)
    written = _execute(scenario, "write_file", {
        "path": "main.py", "content": NEW, "expected_version_token": "stale",
    })
    assert written.result["success"] is False and written.result["error_code"] == "version_conflict"
    _assert_unresolved(scenario, failed)


def test_cross_tool_recovery_does_not_adopt_a_later_write_after_undo(scenario: Scenario) -> None:
    failed, written, _ = _completed(scenario)
    undone = _execute(scenario, "undo_file_change", {"change_id": written.result["receipt"]["change_id"]})
    assert undone.result["success"] is True, undone.result
    assert (scenario.root / "main.py").read_text(encoding="utf-8") == OLD
    later = _write(scenario)
    assert later.result["receipt"]["change_id"] != written.result["receipt"]["change_id"]
    assert _command(scenario).result["success"] is True
    assert (scenario.root / "main.py").read_text(encoding="utf-8") == NEW
    _assert_unresolved(scenario, failed)


@pytest.mark.parametrize("field", ["expected_version_token", "version_before"])
def test_cross_tool_recovery_requires_continuous_precondition_version(scenario: Scenario, field: str) -> None:
    failed, written, _ = _completed(scenario)
    if field == "version_before":
        _edit_output(written, lambda value: value["receipt"].__setitem__("version_before", "stale"))
    else:
        with connect() as db:
            row = db.execute("SELECT input FROM tool_runs WHERE id=?", (written.run_id,)).fetchone()
            arguments = json.loads(row["input"])
            arguments["expected_version_token"] = "stale"
            db.execute("UPDATE tool_runs SET input=? WHERE id=?", (json.dumps(arguments), written.run_id))
    _assert_unresolved(scenario, failed)


@pytest.mark.parametrize("corruption", ["side_effect", "changed_files", "change_id", "path", "count_bool", "counts_equal"])
@pytest.mark.parametrize("expects_failure_handling", [False, True])
def test_cross_tool_recovery_does_not_trust_typed_label_without_strict_no_effect_proof(
    scenario: Scenario, corruption: str, expects_failure_handling: bool,
) -> None:
    failed, _, _ = _completed(scenario)
    def corrupt(value: dict[str, Any]) -> None:
        precondition = value["data"]["edit_precondition"]
        if corruption == "side_effect":
            precondition["side_effect"] = "started"
        elif corruption == "changed_files":
            value["receipt"]["changed_files"] = ["main.py"]
        elif corruption == "change_id":
            value["receipt"]["change_id"] = "unexpected-effect"
        elif corruption == "path":
            precondition["path"] = "other.py"
        elif corruption == "count_bool":
            precondition["actual_count"] = False
        else:
            precondition["actual_count"] = precondition["expected_count"]
    _edit_output(failed, corrupt)
    _assert_unresolved(scenario, failed, expects_failure_handling=expects_failure_handling)


@pytest.mark.parametrize("error_code", ["permission_denied", "read_only_mode", "security_block", "version_conflict", "io_error", "tool_error"])
def test_cross_tool_recovery_never_clears_security_or_untyped_error(scenario: Scenario, error_code: str) -> None:
    failed, _, _ = _completed(scenario)
    def corrupt(value: dict[str, Any]) -> None:
        value["error_code"] = error_code
        value["receipt"]["error_code"] = error_code
    _edit_output(failed, corrupt)
    _assert_unresolved(scenario, failed)


@pytest.mark.parametrize("case", ["missing_command", "missing_persisted_receipt", "untrusted_source", "hash_drift"])
def test_cross_tool_recovery_expected_failure_policy_does_not_replace_recovery_evidence(
    scenario: Scenario, case: str,
) -> None:
    failed = _failed_replace(scenario)
    written = _write(scenario)
    if case != "missing_command":
        assert _command(scenario).result["success"] is True
    with connect() as db:
        if case == "missing_persisted_receipt":
            db.execute("DELETE FROM tool_receipts WHERE receipt_id=?", (written.result["receipt"]["receipt_id"],))
        elif case == "untrusted_source":
            db.execute("UPDATE tool_runs SET source='mcp' WHERE id=?", (written.run_id,))
    if case == "hash_drift":
        (scenario.root / "main.py").write_bytes(b"VALUE = 3\n")
    _assert_unresolved(scenario, failed, expects_failure_handling=True)


@pytest.mark.parametrize("case", ["missing", "before_write", "unrelated", "failed", "exit_missing", "exit_bool", "exit_float", "wrong_affected_path"])
def test_cross_tool_recovery_requires_later_independent_successful_bound_command(scenario: Scenario, case: str) -> None:
    failed = _failed_replace(scenario)
    if case == "before_write":
        _checker(scenario.root, OLD)
        assert _command(scenario).result["success"] is True
        _checker(scenario.root)
    _write(scenario)
    command = None
    if case == "failed":
        _checker(scenario.root, "not-the-actual-file\n")
        command = _command(scenario)
        assert command.result["success"] is False and command.result["exit_code"] != 0
    elif case == "unrelated":
        command = _command(scenario, unrelated=True)
        assert command.result["success"] is True
    elif case == "wrong_affected_path":
        # Real execution, not an inconsistent tool-run/receipt edit: explicit
        # other.py coverage contradicts the target being offered for recovery.
        command = _command(scenario, affected_paths=["other.py"])
        assert command.result["success"] is True
    elif case not in {"missing", "before_write"}:
        command = _command(scenario)
        assert command.result["success"] is True
    if case.startswith("exit_"):
        assert command is not None
        def corrupt(value: dict[str, Any]) -> None:
            if case == "exit_missing":
                value.pop("exit_code", None)
                value["data"].pop("exit_code", None)
                value["receipt"]["exit_code"] = None
            else:
                exit_value = False if case == "exit_bool" else 0.0
                value["exit_code"] = value["data"]["exit_code"] = value["receipt"]["exit_code"] = exit_value
        _edit_output(command, corrupt)
    _assert_unresolved(scenario, failed)
