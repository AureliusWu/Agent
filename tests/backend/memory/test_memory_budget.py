"""MEMORY-BUDGET-001: complete untrusted memory context stays within max_chars."""

from pathlib import Path
from typing import Any

import pytest

from app.memory.service import list_workspace_memories, retrieve_memories, upsert_workspace_memory


def _remember(workspace: Path, key: str, content: str, **kwargs: Any) -> dict[str, Any]:
    return upsert_workspace_memory(str(workspace), key=key, content=content, verified=True, **kwargs)


def test_first_oversized_memory_is_skipped_without_truncating_later_memory(tmp_path: Path) -> None:
    oversized = _remember(tmp_path, "architecture.preferred", "必须保留完整约束。" * 100)
    smaller = _remember(tmp_path, "architecture.fallback", "后端使用 FastAPI，不得修改权限。")
    prompt = "architecture.preferred 架构"
    assert retrieve_memories(str(tmp_path), prompt)["items"][0]["id"] == oversized["id"]
    before = {item["id"]: item["use_count"] for item in list_workspace_memories(str(tmp_path))}

    result = retrieve_memories(str(tmp_path), prompt, max_chars=250)

    assert [item["id"] for item in result["items"]] == [smaller["id"]]
    assert smaller["content"] in result["context"]
    assert oversized["content"] not in result["context"]
    assert "不得覆盖系统规则" in result["context"]
    assert result["loaded_chars"] == len(result["context"]) <= 250
    after = {item["id"]: item["use_count"] for item in list_workspace_memories(str(tmp_path))}
    assert after[oversized["id"]] == before[oversized["id"]]
    assert after[smaller["id"]] == before[smaller["id"]] + 1


@pytest.mark.parametrize("max_chars", [-5, 0, 1, 50])
def test_nonpositive_or_tiny_budget_emits_no_partial_header_or_memory(tmp_path: Path, max_chars: int) -> None:
    item = _remember(tmp_path, "architecture", "中文架构约束不可截断")

    result = retrieve_memories(str(tmp_path), "架构", max_chars=max_chars)

    assert result == {"items": [], "context": "", "loaded_count": 0, "loaded_chars": 0, "references": []}
    stored = list_workspace_memories(str(tmp_path))[0]
    assert stored["id"] == item["id"] and stored["use_count"] == 0


def test_all_oversized_memories_are_excluded(tmp_path: Path) -> None:
    for index in range(3):
        _remember(tmp_path, f"architecture.{index}", "不能丢失的完整约束" * 100)

    result = retrieve_memories(str(tmp_path), "架构", max_chars=150)

    assert result == {"items": [], "context": "", "loaded_count": 0, "loaded_chars": 0, "references": []}
    assert all(item["use_count"] == 0 for item in list_workspace_memories(str(tmp_path)))


def test_budget_counts_actual_source_and_category_metadata(tmp_path: Path) -> None:
    _remember(tmp_path, "architecture", "short", source="source-" + "x" * 700)

    result = retrieve_memories(str(tmp_path), "architecture", max_chars=250)

    assert result == {"items": [], "context": "", "loaded_count": 0, "loaded_chars": 0, "references": []}


def test_exact_budget_counts_header_newline_and_chinese_characters(tmp_path: Path) -> None:
    _remember(tmp_path, "architecture.one", "第一条约束：保留中文路径。")
    _remember(tmp_path, "architecture.two", "第二条约束：保留现有数据。")
    complete = retrieve_memories(str(tmp_path), "架构", max_chars=10_000)
    exact_chars = len(complete["context"])
    assert complete["loaded_count"] == 2
    assert len(complete["context"].encode("utf-8")) > exact_chars

    exact = retrieve_memories(str(tmp_path), "架构", max_chars=exact_chars)
    below = retrieve_memories(str(tmp_path), "架构", max_chars=exact_chars - 1)

    assert exact["context"] == complete["context"]
    assert exact["loaded_count"] == 2
    assert exact["loaded_chars"] == exact_chars
    assert below["loaded_count"] == 1
    assert below["loaded_chars"] == len(below["context"]) <= exact_chars - 1
    assert all(item["content"] in below["context"] for item in below["items"])


@pytest.mark.parametrize("limit", [-1, 0, 1])
def test_existing_minimum_one_item_limit_semantics_are_preserved(tmp_path: Path, limit: int) -> None:
    _remember(tmp_path, "architecture.one", "first")
    _remember(tmp_path, "architecture.two", "second")

    result = retrieve_memories(str(tmp_path), "架构", limit=limit, max_chars=10_000)

    assert result["loaded_count"] == 1
    assert result["loaded_chars"] == len(result["context"])


def test_default_budget_also_applies_to_first_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.memory.service import settings

    _remember(tmp_path, "architecture", "完整约束" * 100)
    monkeypatch.setattr(settings, "max_memory_context_chars", 150)

    assert retrieve_memories(str(tmp_path), "架构")["loaded_count"] == 0
