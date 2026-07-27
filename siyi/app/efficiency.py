from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


READ_ONLY_CACHE_TOOLS = {
    "list_files",
    "list_directory",
    "search_files",
    "search_text",
    "read_file",
    "read_file_range",
    "file_metadata",
    "file_diff",
    "compare_files",
    "list_file_changes",
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

_IGNORED_SOURCE_PARTS = {
    ".git", ".venv", "node_modules", "target", "build", "dist",
    "__pycache__", ".pytest_cache",
}
_FILE_SOURCE_TOOLS = {"read_file", "read_file_range", "file_metadata", "file_diff"}
_DIRECTORY_SOURCE_TOOLS = {"list_files", "list_directory", "search_files", "search_text"}
_GIT_SOURCE_TOOLS = {"list_file_changes", "list_worktrees"}


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_source_path(root: Path, value: object) -> Path | None:
    candidate = (root / str(value or ".")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _tree_fingerprint(path: Path) -> tuple[str, dict[str, Any]]:
    digest = hashlib.sha256()
    file_count = 0
    if not path.exists():
        return hashlib.sha256(b"missing").hexdigest(), {"exists": False, "files": 0}
    if path.is_file():
        stat = path.stat()
        sha256 = _hash_file(path)
        digest.update(f"file\0{stat.st_size}\0{stat.st_mtime_ns}\0{sha256}".encode())
        return digest.hexdigest(), {
            "exists": True,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": sha256,
            "files": 1,
        }
    for child in sorted(path.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = child.relative_to(path)
        if any(part in _IGNORED_SOURCE_PARTS for part in relative.parts):
            continue
        try:
            stat = child.stat()
        except OSError:
            digest.update(f"unreadable\0{relative.as_posix()}".encode())
            continue
        kind = "d" if child.is_dir() else "f"
        digest.update(f"{kind}\0{relative.as_posix()}\0{stat.st_size}\0{stat.st_mtime_ns}\0".encode())
        if child.is_file():
            try:
                digest.update(_hash_file(child).encode())
            except OSError:
                digest.update(b"unreadable")
            file_count += 1
        digest.update(b"\0")
    return digest.hexdigest(), {"exists": True, "files": file_count}


@dataclass(frozen=True)
class SourceVersion:
    kind: str
    fingerprint: str
    workspace_generation: int
    evidence: dict[str, Any] = field(compare=True)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "fingerprint": self.fingerprint,
            "workspace_generation": self.workspace_generation,
            **copy.deepcopy(self.evidence),
        }

    @classmethod
    def capture(
        cls,
        workspace: Path,
        tool: str,
        arguments: dict[str, Any],
        workspace_generation: int,
    ) -> SourceVersion | None:
        paths: list[Path] = []
        kind = "workspace"
        if tool in _FILE_SOURCE_TOOLS:
            path = _safe_source_path(workspace, arguments.get("path"))
            if path is None:
                return None
            paths = [path]
            kind = "file"
        elif tool == "compare_files":
            left = _safe_source_path(workspace, arguments.get("left"))
            right = _safe_source_path(workspace, arguments.get("right"))
            if left is None or right is None:
                return None
            paths = [left, right]
            kind = "files"
        elif tool in _DIRECTORY_SOURCE_TOOLS:
            path = _safe_source_path(workspace, arguments.get("path", "."))
            if path is None:
                return None
            paths = [path]
            kind = "directory"
        else:
            paths = [workspace]

        digest = hashlib.sha256()
        evidence_paths: list[dict[str, Any]] = []
        for path in paths:
            fingerprint, evidence = _tree_fingerprint(path)
            relative = "." if path == workspace else path.relative_to(workspace).as_posix()
            digest.update(f"{relative}\0{fingerprint}\0".encode())
            evidence_paths.append({"path": relative, **evidence, "fingerprint": fingerprint})
        git_head = ""
        if tool in _GIT_SOURCE_TOOLS or tool.startswith("git_"):
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True,
                text=True, encoding="utf-8", errors="replace", check=False,
            )
            git_head = result.stdout.strip() if result.returncode == 0 else "not-a-repository"
            digest.update(f"git\0{git_head}\0".encode())
            kind = "git"
        digest.update(f"generation\0{workspace_generation}".encode())
        return cls(
            kind,
            digest.hexdigest(),
            workspace_generation,
            {"paths": evidence_paths, **({"git_head": git_head} if git_head else {})},
        )


@dataclass
class TokenBudget:
    total_limit: int
    phase_limit: int
    call_limit: int
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    uncached_input_tokens: int = 0
    cache_write_tokens: int = 0
    phase_tokens: dict[str, int] = field(default_factory=dict)
    phase_usage: dict[str, dict[str, int]] = field(default_factory=dict)
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
        cached = max(
            0,
            int(
                usage.get("prompt_cache_hit_tokens")
                or usage.get("cache_read_input_tokens")
                or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                or 0
            ),
        )
        cache_write = max(
            0,
            int(usage.get("cache_creation_input_tokens") or usage.get("prompt_cache_write_tokens") or 0),
        )
        explicit_uncached = usage.get("prompt_cache_miss_tokens")
        uncached = max(0, int(explicit_uncached)) if explicit_uncached is not None else max(0, prompt - cached)
        self.input_tokens += prompt
        self.output_tokens += completion
        self.cached_input_tokens += cached
        self.uncached_input_tokens += uncached
        self.cache_write_tokens += cache_write
        self.total_tokens += total
        self.phase_tokens[phase] = self.phase_tokens.get(phase, 0) + total
        phase_usage = self.phase_usage.setdefault(
            phase,
            {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "uncached_input_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        )
        phase_usage["input_tokens"] += prompt
        phase_usage["cached_input_tokens"] += cached
        phase_usage["uncached_input_tokens"] += uncached
        phase_usage["cache_write_tokens"] += cache_write
        phase_usage["output_tokens"] += completion
        phase_usage["total_tokens"] += total
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
        cache_denominator = self.cached_input_tokens + self.uncached_input_tokens
        return {
            "total_tokens": self.total_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_hit_rate": round(self.cached_input_tokens / cache_denominator, 4) if cache_denominator else 0.0,
            "phase_tokens": dict(self.phase_tokens),
            "phase_usage": copy.deepcopy(self.phase_usage),
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
    def __init__(self, ttl_seconds: int, workspace: str | Path | None = None) -> None:
        self.ttl_seconds = max(0, ttl_seconds)
        self.workspace = Path(workspace).resolve() if workspace else None
        self.workspace_generation = 0
        self._items: dict[str, tuple[float, dict[str, Any], SourceVersion]] = {}

    @staticmethod
    def key(tool: str, arguments: dict[str, Any]) -> str:
        return f"{tool}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)}"

    def get(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        if tool not in READ_ONLY_CACHE_TOOLS or self.ttl_seconds <= 0 or self.workspace is None:
            return None
        key = self.key(tool, arguments)
        item = self._items.get(key)
        if item is None:
            return None
        created_at, result, stored_version = item
        if time.monotonic() - created_at > self.ttl_seconds:
            self._items.pop(key, None)
            return None
        current_version = self.observe(tool, arguments)
        if current_version is None or current_version != stored_version:
            self._items.pop(key, None)
            return None
        cached = copy.deepcopy(result)
        metadata = dict(cached.get("metadata") or {})
        metadata["cache_hit"] = True
        metadata["source_version_validated"] = True
        metadata["source_version"] = current_version.as_dict()
        cached["metadata"] = metadata
        return cached

    def get_context_reference(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        cached = self.get(tool, arguments)
        if cached is None:
            return None
        encoded = json.dumps(cached, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        result_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        reference: dict[str, Any] = {
            "success": cached.get("success", True),
            "status": cached.get("status", "ok"),
            "metadata": {
                **dict(cached.get("metadata") or {}),
                "cache_hit": True,
                "content_unchanged": True,
            },
            "content_reference": {
                "sha256": result_hash,
                "tool": tool,
                "arguments_hash": hashlib.sha256(
                    json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
                "note": "The full result already exists earlier in the current context segment and has not changed.",
                "source_version": dict(cached.get("metadata", {}).get("source_version") or {}),
            },
        }
        for key in ("path", "start_line", "end_line", "total_lines", "total", "truncated"):
            if key in cached:
                reference[key] = cached[key]
        return reference

    def observe(self, tool: str, arguments: dict[str, Any]) -> SourceVersion | None:
        if tool not in READ_ONLY_CACHE_TOOLS or self.workspace is None:
            return None
        return SourceVersion.capture(
            self.workspace,
            tool,
            arguments,
            self.workspace_generation,
        )

    def set(
        self,
        tool: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        *,
        observed_before: SourceVersion | None = None,
    ) -> None:
        if tool in READ_ONLY_CACHE_TOOLS and self.ttl_seconds > 0 and result.get("success"):
            observed_after = self.observe(tool, arguments)
            if observed_after is None:
                return
            if observed_before is not None and observed_before != observed_after:
                return
            self._items[self.key(tool, arguments)] = (
                time.monotonic(),
                copy.deepcopy(result),
                observed_after,
            )

    def bump_workspace_generation(self) -> int:
        self.workspace_generation += 1
        return self.workspace_generation

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
