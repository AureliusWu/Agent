from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any


READ_ONLY_CACHE_TOOLS = {
    "list_files",
    "list_directory",
    "search_files",
    "search_text",
    "read_file",
    "read_file_range",
    "file_diff",
    "compare_files",
    "list_file_changes",
    "list_workspace_memories",
    "get_repo_map",
    "find_symbol",
    "find_definition",
    "find_references",
    "list_module_dependencies",
    "find_related_tests",
    "get_call_chain",
    "inspect_diagnostics",
    "lsp_query",
    "list_worktrees",
}
PARALLEL_READ_TOOLS = READ_ONLY_CACHE_TOOLS - {"list_file_changes", "list_workspace_memories", "lsp_query"}


@dataclass
class TokenBudget:
    total_limit: int
    phase_limit: int
    call_limit: int
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    phase_tokens: dict[str, int] = field(default_factory=dict)
    hard_limit: bool = True

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.total_limit - self.total_tokens)

    def max_output_tokens(self, phase: str, route_limit: int, *, estimated_input_tokens: int = 0) -> int:
        if not self.hard_limit:
            return max(0, route_limit)
        total_remaining = self.total_limit - self.total_tokens - max(0, estimated_input_tokens)
        phase_remaining = self.phase_limit - self.phase_tokens.get(phase, 0) - max(0, estimated_input_tokens)
        return max(0, min(route_limit, self.call_limit, total_remaining, phase_remaining))

    def preflight(self, phase: str, estimated_input_tokens: int, route_limit: int) -> tuple[int, str | None]:
        output_limit = self.max_output_tokens(phase, route_limit, estimated_input_tokens=estimated_input_tokens)
        if not self.hard_limit:
            return output_limit, None
        if output_limit < 256:
            return 0, (
                f"Token 预算已用 {self.total_tokens}/{self.total_limit}，"
                f"预计下次输入需要 {estimated_input_tokens}，已在超限前停止"
            )
        return output_limit, None

    def record(self, phase: str, usage: dict[str, Any]) -> str | None:
        prompt = max(0, int(usage.get("prompt_tokens") or 0))
        completion = max(0, int(usage.get("completion_tokens") or 0))
        total = max(0, int(usage.get("total_tokens") or prompt + completion))
        self.input_tokens += prompt
        self.output_tokens += completion
        self.total_tokens += total
        self.phase_tokens[phase] = self.phase_tokens.get(phase, 0) + total
        if not self.hard_limit:
            return None
        if total > self.call_limit:
            return f"单次模型调用 Token 用量 {total} 超过上限 {self.call_limit}"
        if self.phase_tokens[phase] > self.phase_limit:
            return f"阶段 {phase} Token 用量 {self.phase_tokens[phase]} 超过上限 {self.phase_limit}"
        if self.total_tokens > self.total_limit:
            return f"任务 Token 用量 {self.total_tokens} 超过上限 {self.total_limit}"
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "phase_tokens": dict(self.phase_tokens),
            "limit": self.total_limit,
            "remaining_tokens": self.remaining_tokens,
            "percent": round((self.total_tokens / self.total_limit) * 100, 2) if self.total_limit else 100.0,
            "hard_limit": self.hard_limit,
        }


def estimate_model_input_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> int:
    """Conservative preflight estimate for OpenAI-compatible chat payloads.

    Chinese text and tool schemas often tokenize more densely than English. The
    2 chars/token estimate plus a fixed envelope intentionally errs high so the
    provider-reported total cannot casually cross the task hard limit.
    """
    encoded = json.dumps({"messages": messages, "tools": tools or []}, ensure_ascii=False, default=str)
    return max(256, (len(encoded.encode("utf-8")) + 1) // 2 + 512)


class TaskReadCache:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = max(0, ttl_seconds)
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def key(tool: str, arguments: dict[str, Any]) -> str:
        return f"{tool}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)}"

    def get(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        if tool not in READ_ONLY_CACHE_TOOLS or self.ttl_seconds <= 0:
            return None
        key = self.key(tool, arguments)
        item = self._items.get(key)
        if item is None:
            return None
        created_at, result = item
        if time.monotonic() - created_at > self.ttl_seconds:
            self._items.pop(key, None)
            return None
        cached = copy.deepcopy(result)
        metadata = dict(cached.get("metadata") or {})
        metadata["cache_hit"] = True
        cached["metadata"] = metadata
        return cached

    def set(self, tool: str, arguments: dict[str, Any], result: dict[str, Any]) -> None:
        if tool in READ_ONLY_CACHE_TOOLS and self.ttl_seconds > 0 and result.get("success"):
            self._items[self.key(tool, arguments)] = (time.monotonic(), copy.deepcopy(result))

    def clear(self) -> None:
        self._items.clear()


def _truncate_text(value: str, limit: int, *, prefer_tail: bool = False) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    marker = f"\n[TRUNCATED: 原始 {len(value)} 字符，仅保留关键片段]\n"
    room = max(0, limit - len(marker))
    if room == 0:
        return marker[:limit], True
    if prefer_tail:
        return marker + value[-room:], True
    head = room * 2 // 3
    tail = room - head
    suffix = value[-tail:] if tail else ""
    return value[:head] + marker + suffix, True


def _compact_value(value: Any, string_limit: int, list_limit: int = 40) -> tuple[Any, bool]:
    if isinstance(value, str):
        return _truncate_text(value, string_limit)
    if isinstance(value, list):
        compacted: list[Any] = []
        changed = len(value) > list_limit
        for item in value[:list_limit]:
            normalized, item_changed = _compact_value(item, string_limit)
            compacted.append(normalized)
            changed = changed or item_changed
        if len(value) > list_limit:
            compacted.append({"_truncated_items": len(value) - list_limit})
        return compacted, changed
    if isinstance(value, dict):
        compacted_dict: dict[str, Any] = {}
        changed = False
        for key, item in value.items():
            normalized, item_changed = _compact_value(item, string_limit)
            compacted_dict[str(key)] = normalized
            changed = changed or item_changed
        return compacted_dict, changed
    return value, False


def compact_tool_result(tool: str, result: dict[str, Any], *, max_chars: int, file_chars: int) -> dict[str, Any]:
    compacted = copy.deepcopy(result)
    changed = False
    data = compacted.get("data")
    if isinstance(data, dict) and data and all(key in compacted for key in data):
        compacted.pop("data", None)
        changed = True

    if tool in {"read_file", "read_file_range"} and isinstance(compacted.get("content"), str):
        compacted["content"], content_changed = _truncate_text(compacted["content"], file_chars)
        changed = changed or content_changed
    if tool in {"search_files", "search_text"} and isinstance(compacted.get("matches"), list):
        unique: list[Any] = []
        seen: set[str] = set()
        for item in compacted["matches"]:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                unique.append(item)
        if len(unique) != len(compacted["matches"]):
            changed = True
        compacted["matches"] = unique
    if tool == "run_command":
        failed = not bool(compacted.get("success"))
        for key in ("stdout", "stderr"):
            if isinstance(compacted.get(key), str):
                limit = min(6000 if failed else 2500, max_chars // 2)
                compacted[key], stream_changed = _truncate_text(compacted[key], limit, prefer_tail=failed)
                changed = changed or stream_changed

    compacted, recursively_changed = _compact_value(compacted, min(file_chars, max_chars // 2))
    changed = changed or recursively_changed
    encoded = json.dumps(compacted, ensure_ascii=False, default=str)
    if len(encoded) > max_chars:
        changed = True
    if changed or result.get("truncated"):
        compacted["_truncated"] = True
        compacted["_truncation_note"] = "工具原始结果保留在审计记录中；模型仅接收压缩后的关键字段。"
    encoded = json.dumps(compacted, ensure_ascii=False, default=str)
    if len(encoded) > max_chars:
        base = {
            "success": result.get("success"),
            "status": result.get("status"),
            "error_code": result.get("error_code"),
            "error_message": result.get("error_message"),
            "_truncated": True,
            "_truncation_note": "工具原始结果保留在审计记录中；模型仅接收压缩后的关键字段。",
            "summary": "",
        }
        overhead = len(json.dumps(base, ensure_ascii=False, default=str))
        summary, _ = _truncate_text(encoded, max(0, max_chars - overhead))
        base["summary"] = summary
        while summary and len(json.dumps(base, ensure_ascii=False, default=str)) > max_chars:
            excess = len(json.dumps(base, ensure_ascii=False, default=str)) - max_chars
            summary = summary[:-excess] if excess < len(summary) else ""
            base["summary"] = summary
        compacted = base
    return compacted


def parallel_read_batch(calls: list[dict[str, Any]], mcp_tool_names: set[str]) -> list[dict[str, Any]]:
    batch: list[dict[str, Any]] = []
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        name = str((function or {}).get("name") or "")
        if name in mcp_tool_names or name not in PARALLEL_READ_TOOLS:
            break
        batch.append(call)
    return batch if len(batch) > 1 else []
