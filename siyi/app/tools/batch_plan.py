"""Bounded, in-memory preflight for sequential file plans (never a workspace copy)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.permissions import permission_denial
from app.sandbox import (
    MAX_ATOMIC_WRITE_BYTES, FileVersionError, SandboxError, _apply_unified_patch,
    _read_text, file_version_token, safe_path, workspace_root,
)
from app.tools.registry import REGISTRY, validate_arguments


@dataclass
class _State:
    version: str
    origin: Path | None = None
    text: str | None = None
    encoding: str = "utf-8"
    writer: int | None = None


@dataclass(frozen=True)
class PlannedStep:
    operation: str
    tool: str
    arguments: dict[str, Any]
    before: dict[str, str]
    after: dict[str, str]


class BatchPlanError(ValueError):
    def __init__(self, index: int, code: str, message: str):
        self.index, self.code = index, code
        super().__init__(message)


class FileBatchPlan:
    def __init__(self, workspace: str):
        self.root = workspace_root(workspace)
        self.steps: list[PlannedStep] = []
        self.initial: dict[str, str] = {}
        self.states: dict[str, _State] = {}
        self._planned_bytes = 0

    def path(self, raw: str) -> str:
        relative = safe_path(self.root, raw).relative_to(self.root).as_posix()
        # Revalidate the canonical spelling too (including internal backup
        # protection when the caller supplied an absolute or dot-segment path).
        safe_path(self.root, relative)
        return relative

    def state(self, path: str) -> _State:
        if path not in self.states:
            target = safe_path(self.root, path)
            if target.is_file() and target.stat().st_size > MAX_ATOMIC_WRITE_BYTES:
                raise SandboxError("批操作预检单个文件不能超过 20 MiB")
            version = file_version_token(target)
            self.initial[path] = version
            self.states[path] = _State(version, origin=target)
        return self.states[path]

    def require_version(self, arguments: dict[str, Any], field: str, path: str) -> str:
        state = self.state(path)
        expected = arguments.get(field)
        if not isinstance(expected, str) or not expected:
            raise FileVersionError("version_token_required", f"Missing file version token: {field}")
        if expected.startswith("batch:"):
            try:
                referenced_index = int(expected.removeprefix("batch:"))
            except ValueError as exc:
                raise FileVersionError("version_conflict", "批操作版本引用无效") from exc
            if state.writer != referenced_index or referenced_index < 0:
                raise FileVersionError("version_conflict", "批操作版本引用必须绑定同一路径最近的前序变更")
            expected = state.version
        if expected != state.version:
            raise FileVersionError("version_conflict", f"File changed after it was read: {Path(path).name}")
        arguments[field] = expected
        return expected

    def text(self, state: _State) -> tuple[str, str]:
        if state.text is not None:
            return state.text, state.encoding
        if state.version == "missing":
            return "", "utf-8"
        if not state.version.startswith("file:") or state.origin is None:
            raise SandboxError("目标不是文件")
        return _read_text(state.origin)

    def append(self, operation: str, tool: str, values: dict[str, Any], mode: str) -> None:
        index = len(self.steps)
        arguments = dict(values)
        if arguments.pop("dry_run", False):
            raise SandboxError("请使用批操作顶层 dry_run，不允许子操作混入预览")
        spec = validate_arguments(tool, arguments)
        denial = permission_denial(mode=mode, risk=spec.risk, tool=tool, workspace=str(self.root))
        if denial:
            raise BatchPlanError(index, str(denial.confirmation["error_code"]),
                                 str(denial.confirmation.get("error_message") or "权限已拒绝"))
        before: dict[str, str] = {}
        after: dict[str, str] = {}
        for key in ("path", "source", "destination"):
            if key in arguments:
                path = self.path(str(arguments[key]))
                arguments[key] = path
                before[path] = self.state(path).version
                for parent in (self.root / path).parents:
                    if parent == self.root:
                        break
                    if parent.exists() and not parent.is_dir():
                        raise SandboxError("目标父路径不是目录")
        if tool in {"write_file", "apply_patch"}:
            path = arguments["path"]
            if path == ".":
                raise SandboxError("禁止将工作区根目录作为文件目标")
            self.require_version(arguments, "expected_version_token", path)
            old, detected_encoding = self.text(self.state(path))
            encoding = str(arguments.get("encoding", "auto"))
            encoding = detected_encoding if encoding == "auto" else encoding
            content = str(arguments["content"]) if tool == "write_file" else _apply_unified_patch(old, str(arguments["patch"]))
            if "\r\n" in old and "\r\n" not in content:
                content = content.replace("\n", "\r\n")
            payload = content.encode(encoding)
            self._planned_bytes += len(payload)
            if len(payload) > MAX_ATOMIC_WRITE_BYTES or self._planned_bytes > 2 * MAX_ATOMIC_WRITE_BYTES:
                raise SandboxError("批操作预检内容超出 20 MiB 单文件 / 40 MiB 总量限制")
            version = f"file:{len(payload)}:{hashlib.sha256(payload).hexdigest()}"
            self.states[path] = _State(version, text=content, encoding=encoding, writer=index)
            after[path] = version
        elif tool in {"copy_file", "move_file", "rename_file"}:
            source, destination = arguments["source"], arguments["destination"]
            source_version = self.require_version(arguments, "expected_version_token", source)
            destination_version = self.require_version(arguments, "expected_destination_version_token", destination)
            if not source_version.startswith("file:"):
                raise SandboxError("批操作仅移动或复制单个文件；目录移动请单独执行")
            if destination_version.startswith("directory:") or source == destination or destination == ".":
                raise SandboxError("批操作目标必须是独立文件路径")
            state = self.state(source)
            self.states[destination] = _State(source_version, origin=state.origin, text=state.text,
                                               encoding=state.encoding, writer=index)
            after[destination] = source_version
            if tool != "copy_file":
                self.states[source] = _State("missing", writer=index)
                after[source] = "missing"
        elif tool == "delete_file":
            path = arguments["path"]
            if not self.require_version(arguments, "expected_version_token", path).startswith("file:"):
                raise SandboxError("目标不是文件")
            self.states[path] = _State("missing", writer=index)
            after[path] = "missing"
        elif tool == "create_directory":
            path = arguments["path"]
            if path == ".":
                raise SandboxError("工作区根目录已存在")
            if self.state(path).version not in {"missing"} and not self.state(path).version.startswith("directory:"):
                raise SandboxError("目标已存在且不是目录")
            # Directory timestamps cannot be predicted; only file dependencies
            # support batch:N. Actual execution still uses the sandbox.
            self.states[path] = _State("directory:planned", writer=index)
            after[path] = "directory:planned"
        elif spec.risk != "low":
            raise SandboxError("此操作不支持批事务，请通过独立文件工具执行并单独确认")
        elif any(self.state(path).writer is not None for path in before):
            raise SandboxError("批操作读取请在事务完成后执行，以免混淆预览与实际文件状态")
        self.steps.append(PlannedStep(operation, tool, arguments, before, after))

    def verify_initial(self) -> None:
        for path, expected in self.initial.items():
            if file_version_token(safe_path(self.root, path)) != expected:
                raise FileVersionError("version_conflict", f"批操作预检后路径已变化：{path}")

    def verify_step(self, index: int) -> None:
        for path, expected in self.steps[index].before.items():
            if expected == "directory:planned":
                raise FileVersionError("version_conflict", "目录依赖需要在前一事务完成后重新读取版本")
            if file_version_token(safe_path(self.root, path)) != expected:
                raise FileVersionError("version_conflict", f"批操作执行前路径已变化：{path}")

    def preview(self) -> list[dict[str, Any]]:
        return [{"success": True, "status": "ok", "dry_run": True, "operation": step.operation,
                 "paths": list(step.before), "version_before": step.before, "version_after": step.after}
                for step in self.steps]
