import json
import subprocess
import sys
import time

from app.context.budget import compact_messages_deterministically, model_context_profile, request_budget
from app.efficiency import TaskReadCache, TokenBudget, compact_tool_result, estimate_model_input_tokens, parallel_read_batch


def test_token_budget_enforces_call_phase_and_task_limits() -> None:
    call_budget = TokenBudget(total_limit=100, phase_limit=80, call_limit=20)
    assert "单次模型调用" in str(call_budget.record("analysis", {"total_tokens": 21}))

    phase_budget = TokenBudget(total_limit=100, phase_limit=30, call_limit=40)
    assert phase_budget.record("analysis", {"total_tokens": 20}) is None
    assert "阶段 analysis" in str(phase_budget.record("analysis", {"total_tokens": 15}))

    task_budget = TokenBudget(total_limit=30, phase_limit=100, call_limit=100)
    assert task_budget.record("analysis", {"total_tokens": 20}) is None
    assert "任务 Token" in str(task_budget.record("implementation", {"total_tokens": 15}))


def test_token_budget_preflight_reserves_input_and_stops_before_overrun() -> None:
    budget = TokenBudget(total_limit=1_000, phase_limit=1_000, call_limit=800, total_tokens=700)
    output, reason = budget.preflight("analysis", estimated_input_tokens=250, route_limit=800)
    assert output == 0
    assert "超限前停止" in str(reason)

    estimate = estimate_model_input_tokens([{"role": "user", "content": "分析项目"}], [])
    assert estimate >= 256


def test_soft_token_budget_records_usage_without_stopping_task() -> None:
    budget = TokenBudget(total_limit=100, phase_limit=50, call_limit=20, hard_limit=False)
    assert budget.record("analysis", {"input_tokens": 70, "output_tokens": 30, "total_tokens": 100}) is None
    output, reason = budget.preflight("analysis", estimated_input_tokens=80, route_limit=4_096)
    assert output == 4_096
    assert reason is None
    assert budget.snapshot()["hard_limit"] is False


def test_token_budget_records_provider_cache_usage_by_phase() -> None:
    budget = TokenBudget(total_limit=10_000, phase_limit=10_000, call_limit=10_000)
    budget.record(
        "analysis",
        {
            "prompt_tokens": 1_000,
            "completion_tokens": 100,
            "total_tokens": 1_100,
            "prompt_cache_hit_tokens": 750,
            "prompt_cache_miss_tokens": 250,
        },
    )
    snapshot = budget.snapshot()
    assert snapshot["cached_input_tokens"] == 750
    assert snapshot["uncached_input_tokens"] == 250
    assert snapshot["cache_hit_rate"] == 0.75
    assert snapshot["phase_usage"]["analysis"]["cached_input_tokens"] == 750


def test_token_budget_understands_openai_cached_token_details() -> None:
    budget = TokenBudget(total_limit=10_000, phase_limit=10_000, call_limit=10_000)
    budget.record(
        "verification",
        {
            "prompt_tokens": 800,
            "completion_tokens": 50,
            "total_tokens": 850,
            "prompt_tokens_details": {"cached_tokens": 600},
        },
    )
    snapshot = budget.snapshot()
    assert snapshot["cached_input_tokens"] == 600
    assert snapshot["uncached_input_tokens"] == 200


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


def test_read_cache_returns_copy_and_expires(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    cache = TaskReadCache(ttl_seconds=1, workspace=tmp_path)
    result = {"success": True, "status": "ok", "items": ["a"]}
    cache.set("list_files", {"path": "."}, result)
    cached = cache.get("list_files", {"path": "."})

    assert cached is not None and cached["metadata"]["cache_hit"] is True
    cached["items"].append("b")
    assert cache.get("list_files", {"path": "."})["items"] == ["a"]

    _, stored, version = cache._items[cache.key("list_files", {"path": "."})]
    cache._items[cache.key("list_files", {"path": "."})] = (time.monotonic() - 2, stored, version)
    assert cache.get("list_files", {"path": "."}) is None


def test_unchanged_file_cache_injects_reference_instead_of_full_content(tmp_path) -> None:
    (tmp_path / "large.py").write_text("large source text" * 100, encoding="utf-8")
    cache = TaskReadCache(ttl_seconds=30, workspace=tmp_path)
    arguments = {"path": "large.py"}
    cache.set(
        "read_file",
        arguments,
        {
            "success": True,
            "status": "ok",
            "path": "large.py",
            "content": "large source text" * 100,
            "total_lines": 80,
        },
    )
    reference = cache.get_context_reference("read_file", arguments)
    assert reference is not None
    assert "content" not in reference
    assert reference["metadata"]["content_unchanged"] is True
    assert reference["content_reference"]["sha256"]
    assert reference["content_reference"]["source_version"]["kind"] == "file"
    assert reference["metadata"]["source_version_validated"] is True
    assert reference["path"] == "large.py"


def test_external_file_edit_invalidates_cached_read(tmp_path) -> None:
    path = tmp_path / "module.py"
    path.write_text("value = 1\n", encoding="utf-8")
    cache = TaskReadCache(60, tmp_path)
    arguments = {"path": "module.py"}
    before = cache.observe("read_file", arguments)
    cache.set("read_file", arguments, {"success": True, "content": "value = 1\n"}, observed_before=before)

    path.write_text("value = 2\n", encoding="utf-8")

    assert cache.get_context_reference("read_file", arguments) is None


def test_directory_change_and_command_output_invalidate_cached_listing(tmp_path) -> None:
    (tmp_path / "first.txt").write_text("first", encoding="utf-8")
    cache = TaskReadCache(60, tmp_path)
    arguments = {"path": "."}
    before = cache.observe("list_files", arguments)
    cache.set("list_files", arguments, {"success": True, "items": ["first.txt"]}, observed_before=before)

    subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; Path('generated.txt').write_text('generated', encoding='utf-8')"],
        cwd=tmp_path,
        check=True,
    )

    assert cache.get_context_reference("list_files", arguments) is None


def test_git_checkout_invalidates_cached_read(tmp_path) -> None:
    path = tmp_path / "tracked.txt"
    path.write_text("first", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "cache-test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Cache Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "first"], cwd=tmp_path, check=True, capture_output=True)
    path.write_text("second", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "second"], cwd=tmp_path, check=True, capture_output=True)
    cache = TaskReadCache(60, tmp_path)
    arguments = {"path": "tracked.txt"}
    before = cache.observe("read_file", arguments)
    cache.set("read_file", arguments, {"success": True, "content": "second"}, observed_before=before)

    subprocess.run(["git", "checkout", "HEAD~1", "--", "tracked.txt"], cwd=tmp_path, check=True, capture_output=True)

    assert cache.get_context_reference("read_file", arguments) is None


def test_workspace_generation_and_lsp_source_change_invalidate_cache(tmp_path) -> None:
    path = tmp_path / "main.py"
    path.write_text("answer = 1\n", encoding="utf-8")
    cache = TaskReadCache(60, tmp_path)
    arguments = {"operation": "diagnostics", "path": "main.py"}
    before = cache.observe("lsp_query", arguments)
    cache.set("lsp_query", arguments, {"success": True, "diagnostics": []}, observed_before=before)
    cache.bump_workspace_generation()
    assert cache.get("lsp_query", arguments) is None

    after_bump = cache.observe("lsp_query", arguments)
    cache.set("lsp_query", arguments, {"success": True, "diagnostics": []}, observed_before=after_bump)
    path.write_text("answer = 2\n", encoding="utf-8")
    assert cache.get("lsp_query", arguments) is None


def test_read_changed_during_execution_is_not_cached(tmp_path) -> None:
    path = tmp_path / "race.txt"
    path.write_text("before", encoding="utf-8")
    cache = TaskReadCache(60, tmp_path)
    arguments = {"path": "race.txt"}
    before = cache.observe("read_file", arguments)
    path.write_text("after", encoding="utf-8")

    cache.set("read_file", arguments, {"success": True, "content": "before"}, observed_before=before)

    assert cache.get("read_file", arguments) is None


def test_parallel_batch_accepts_only_contiguous_builtin_reads() -> None:
    read = lambda call_id, name="read_file": {"id": call_id, "function": {"name": name, "arguments": "{}"}}
    calls = [read("1"), read("2", "search_files"), read("3", "write_file"), read("4")]
    assert [item["id"] for item in parallel_read_batch(calls, set())] == ["1", "2"]
    assert parallel_read_batch([read("1"), read("2", "mcp__search")], {"mcp__search"}) == []


def test_context_budget_uses_current_model_window_and_reserves_output() -> None:
    profile = model_context_profile(base_url="https://api.deepseek.com", model="deepseek-v4-pro")
    assert profile.context_window_tokens == 1_000_000
    plan = request_budget([{"role": "user", "content": "hello"}], [], model="deepseek-v4-pro", desired_output_tokens=8_192)
    assert plan.reserved_output_tokens == 8_192
    assert plan.effective_input_budget < profile.context_window_tokens


def test_unknown_model_uses_conservative_fallback(monkeypatch) -> None:
    monkeypatch.setattr("app.context.budget.settings.default_model_context_window", 65_536)
    profile = model_context_profile(base_url="https://example.invalid/v1", model="unknown-model")
    assert profile.context_window_tokens == 65_536
    assert profile.capability_source == "conservative_fallback"


def test_compaction_preserves_system_current_user_and_deduplicates_tools() -> None:
    duplicate = '{"result":"same"}'
    messages = [
        {"role": "system", "content": "IDENTITY_KERNEL natsume-kokoro-001"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current task contract"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": duplicate},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "b"}]},
        {"role": "tool", "tool_call_id": "b", "content": duplicate},
    ]
    compacted, report = compact_messages_deterministically(messages, [], target_input_tokens=2_000)
    assert compacted[0]["content"] == messages[0]["content"]
    assert any(item.get("content") == "current task contract" for item in compacted)
    assert report["duplicate_tool_results"] == 1
    assert "duplicate_tool_result" in compacted[-1]["content"]
