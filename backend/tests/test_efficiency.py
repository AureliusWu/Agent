import json
import time

from app.efficiency import TaskReadCache, TokenBudget, compact_tool_result, parallel_read_batch


def test_token_budget_enforces_call_phase_and_task_limits() -> None:
    call_budget = TokenBudget(total_limit=100, phase_limit=80, call_limit=20)
    assert "单次模型调用" in str(call_budget.record("analysis", {"total_tokens": 21}))

    phase_budget = TokenBudget(total_limit=100, phase_limit=30, call_limit=40)
    assert phase_budget.record("analysis", {"total_tokens": 20}) is None
    assert "阶段 analysis" in str(phase_budget.record("analysis", {"total_tokens": 15}))

    task_budget = TokenBudget(total_limit=30, phase_limit=100, call_limit=100)
    assert task_budget.record("analysis", {"total_tokens": 20}) is None
    assert "任务 Token" in str(task_budget.record("implementation", {"total_tokens": 15}))


def test_tool_compaction_marks_truncation_and_keeps_failure_tail() -> None:
    result = {
        "success": False,
        "status": "error",
        "exit_code": 1,
        "stdout": "x" * 9000,
        "stderr": "prefix\n" + "y" * 9000 + "\nFAILED test_example.py::test_case",
        "error_code": "command_failed",
    }

    compacted = compact_tool_result("run_command", result, max_chars=4000, file_chars=2500)

    assert compacted["_truncated"] is True
    assert "FAILED test_example.py::test_case" in str(compacted)
    assert len(str(compacted)) < len(str(result))
    assert len(json.dumps(compacted, ensure_ascii=False)) <= 4000


def test_read_cache_returns_copy_and_expires() -> None:
    cache = TaskReadCache(ttl_seconds=1)
    result = {"success": True, "status": "ok", "items": ["a"]}
    cache.set("list_files", {"path": "."}, result)
    cached = cache.get("list_files", {"path": "."})

    assert cached is not None and cached["metadata"]["cache_hit"] is True
    cached["items"].append("b")
    assert cache.get("list_files", {"path": "."})["items"] == ["a"]

    cache._items[cache.key("list_files", {"path": "."})] = (time.monotonic() - 2, result)
    assert cache.get("list_files", {"path": "."}) is None


def test_parallel_batch_accepts_only_contiguous_builtin_reads() -> None:
    read = lambda call_id, name="read_file": {"id": call_id, "function": {"name": name, "arguments": "{}"}}
    calls = [read("1"), read("2", "search_files"), read("3", "write_file"), read("4")]
    assert [item["id"] for item in parallel_read_batch(calls, set())] == ["1", "2"]
    assert parallel_read_batch([read("1"), read("2", "mcp__search")], {"mcp__search"}) == []
