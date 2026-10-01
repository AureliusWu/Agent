"""Bounded file evidence and conservative Windows workspace recovery.

Permission decisions remain with the caller. This module never grants authority.
Recovery displaces, rather than deletes, the previous target into the same-volume
recovery folder. Neither a failed restore nor a successful restore erases that
last copy; retention/cleanup is a separate, explicitly authorized operation.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


FORMAT_VERSION = 2
HASH_CHUNK_BYTES = 1024 * 1024
BACKUP_FREE_RESERVE_BYTES = 64 * 1024 * 1024
_pinned_handles: ContextVar[dict[Path, Any]] = ContextVar("recovery_pinned_handles", default={})
_recovery_actor: ContextVar[tuple[str | None, str | None]] = ContextVar("recovery_actor", default=(None, None))
_restore_sequence: ContextVar[dict[str, Any] | None] = ContextVar("restore_sequence", default=None)


@contextmanager
def recovery_actor(task_id: str | None, tool_call_id: str | None):
    """Metadata identity only; this does not grant any restore authority."""
    token = _recovery_actor.set((task_id, tool_call_id))
    try:
        yield
    finally:
        _recovery_actor.reset(token)


class RecoveryError(ValueError):
    def __init__(self, code: str, message: str, *, paths: list[str] | None = None,
                 restored: list[str] | None = None) -> None:
        self.code = code
        self.paths = paths or []
        self.restored = restored or []
        super().__init__(message)


@dataclass
class ScanBudget:
    max_entries: int = 10_000
    max_bytes: int = 4 * 1024 * 1024 * 1024
    timeout_seconds: float = 30.0
    cancelled: Callable[[], bool] | None = None
    entries: int = 0
    byte_count: int = 0
    started: float = 0.0

    def __post_init__(self) -> None:
        self.started = time.monotonic()

    def check(self, *, entries: int = 0, byte_count: int = 0) -> None:
        self.entries += entries
        self.byte_count += byte_count
        if self.cancelled and self.cancelled():
            raise RecoveryError("recovery_scan_cancelled", "文件证据扫描已取消；未开始恢复")
        if (self.entries > self.max_entries or self.byte_count > self.max_bytes
                or time.monotonic() - self.started > self.timeout_seconds):
            raise RecoveryError("recovery_scan_limit", "文件证据扫描超过条目、字节或时间预算；保留备份等待处理")


def _identity(metadata: os.stat_result) -> dict[str, int]:
    result = {"device": metadata.st_dev, "inode": metadata.st_ino}
    if stat.S_ISREG(metadata.st_mode):
        result["mtime_ns"] = metadata.st_mtime_ns
    return result


def _linklike(path: Path, metadata: os.stat_result) -> bool:
    return (stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & 0x400))


def file_state(path: Path, budget: ScanBudget | None = None) -> dict[str, Any]:
    """Complete subtree evidence with streaming hashes, never directory mtime only."""
    budget = budget or ScanBudget()
    budget.check(entries=1)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {"exists": False, "type": None, "size": 0, "sha256": None}
    if _linklike(path, before):
        raise RecoveryError("recovery_unsafe_path", "文件证据拒绝符号链接或 reparse 路径")
    if stat.S_ISDIR(before.st_mode):
        # Bound enumeration before sorting; do not first materialize a huge tree.
        names: list[str] = []
        with os.scandir(path) as entries:
            for entry in entries:
                budget.check()
                if len(names) + budget.entries >= budget.max_entries:
                    raise RecoveryError("recovery_scan_limit", "目录证据超过条目预算")
                names.append(entry.name)
        children = {name: file_state(path / name, budget) for name in sorted(names)}
        after = path.lstat()
        if _identity(before) != _identity(after) or before.st_mtime_ns != after.st_mtime_ns:
            raise RecoveryError("recovery_conflict", "目录在证据扫描期间变化")
        return {"exists": True, "type": "directory", "size": 0, "sha256": None,
                "identity": _identity(after), "children": children}
    if not stat.S_ISREG(before.st_mode):
        raise RecoveryError("recovery_unsafe_path", "文件证据仅支持普通文件与目录")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if _identity(before) != _identity(opened):
            raise RecoveryError("recovery_conflict", "文件在打开时已被替换")
        while chunk := stream.read(HASH_CHUNK_BYTES):
            budget.check(byte_count=len(chunk))
            digest.update(chunk)
        after_handle = os.fstat(stream.fileno())
    after = path.lstat()
    if (before.st_size != after.st_size or before.st_size != after_handle.st_size
            or _identity(before) != _identity(after) or _identity(before) != _identity(after_handle)):
        raise RecoveryError("recovery_conflict", "文件在证据扫描期间变化")
    return {"exists": True, "type": "file", "size": after.st_size,
            "sha256": digest.hexdigest(), "identity": _identity(after)}


def content_state(state: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in state.items() if key not in {"identity", "children"}}
    if state.get("type") == "directory":
        result["children"] = {key: content_state(value) for key, value in state.get("children", {}).items()}
    return result


def state_bytes(state: dict[str, Any]) -> int:
    if state.get("type") == "directory":
        return sum(state_bytes(child) for child in state.get("children", {}).values())
    return int(state.get("size") or 0)


def require_disk_space(directory: Path, required_bytes: int) -> None:
    try:
        free = shutil.disk_usage(directory).free
    except OSError as exc:
        raise RecoveryError("recovery_space_unknown", "无法确认恢复区可用磁盘空间；未开始复制") from exc
    if free < required_bytes + BACKUP_FREE_RESERVE_BYTES:
        raise RecoveryError("recovery_space_insufficient", "恢复区空间不足；保留安全余量，未开始复制")


def copy_bounded(source: Path, destination: Path, budget: ScanBudget) -> None:
    """Copy with the caller's cumulative budget, preserving failed partial copies.

    Native bulk-copy functions have no cooperative cancellation point. Check
    each bounded read/write here and create names exclusively; the caller still
    validates the complete copied content before any workspace mutation.
    """
    budget.check(entries=1)
    before = source.lstat()
    if _linklike(source, before):
        raise RecoveryError("recovery_unsafe_path", "备份复制拒绝链接或 reparse 路径")
    if stat.S_ISDIR(before.st_mode):
        destination.mkdir()
        with os.scandir(source) as entries:
            for entry in entries:
                budget.check()
                copy_bounded(source / entry.name, destination / entry.name, budget)
        after = source.lstat()
        if _identity(before) != _identity(after) or before.st_mtime_ns != after.st_mtime_ns:
            raise RecoveryError("recovery_conflict", "目录在备份复制期间变化；保留部分备份")
    elif stat.S_ISREG(before.st_mode):
        with source.open("rb") as origin, destination.open("xb", buffering=0) as copied:
            if _identity(os.fstat(origin.fileno())) != _identity(before):
                raise RecoveryError("recovery_conflict", "文件在备份打开期间被替换")
            while True:
                budget.check()
                chunk = origin.read(HASH_CHUNK_BYTES)
                if not chunk:
                    break
                budget.check(byte_count=len(chunk))
                if copied.write(chunk) != len(chunk):
                    raise RecoveryError("recovery_copy_incomplete", "备份未完整写入；保留部分备份")
            copied.flush()
            os.fsync(copied.fileno())
            after_handle = os.fstat(origin.fileno())
        after = source.lstat()
        if (before.st_size != after.st_size or before.st_size != after_handle.st_size
                or _identity(before) != _identity(after) or _identity(before) != _identity(after_handle)):
            raise RecoveryError("recovery_conflict", "文件在备份复制期间变化；保留部分备份")
    else:
        raise RecoveryError("recovery_unsafe_path", "备份复制仅支持普通文件与目录")
    budget.check()
    shutil.copystat(source, destination, follow_symlinks=False)


def state_token(state: dict[str, Any]) -> str:
    if not state["exists"]:
        return "missing"
    if state["type"] == "file":
        return f"file:{state['size']}:{state['sha256']}"
    digest = hashlib.sha256(json.dumps(content_state(state), sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"directory:tree-v2:{digest}"


def checked_path(root: Path, relative: str) -> Path:
    raw = Path(relative)
    if (not relative or raw.is_absolute() or raw.drive or ".." in raw.parts
            or relative.replace("\\", "/").startswith("/") or "\x00" in relative):
        raise RecoveryError("recovery_unsafe_path", "恢复记录包含无效路径")
    path = root
    for part in raw.parts:
        path = path / part
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if _linklike(path, metadata):
            raise RecoveryError("recovery_unsafe_path", "恢复路径包含链接或 reparse 节点")
    if path == root or not path.resolve().is_relative_to(root.resolve()):
        raise RecoveryError("recovery_unsafe_path", "恢复路径超出工作区")
    return path


def write_manifest(folder: Path, manifest: dict[str, Any]) -> None:
    temporary = folder / f".manifest-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, folder / "manifest.json")
    finally:
        if temporary.exists():
            temporary.unlink()


def _valid_state(state: Any) -> bool:
    if not isinstance(state, dict) or type(state.get("exists")) is not bool:
        return False
    if not state["exists"]:
        return state == {"exists": False, "type": None, "size": 0, "sha256": None}
    if not isinstance(state.get("identity"), dict):
        return False
    if state.get("type") == "directory":
        return isinstance(state.get("children"), dict) and all(
            isinstance(name, str) and name not in {".", ".."} and not any(c in name for c in "/\\")
            and _valid_state(child) for name, child in state["children"].items())
    return (state.get("type") == "file" and type(state.get("size")) is int
            and state["size"] >= 0 and isinstance(state.get("sha256"), str)
            and len(state["sha256"]) == 64)


def _load(root: Path, folder: Path) -> dict[str, Any]:
    checked_path(root, folder.relative_to(root).as_posix())
    record = checked_path(root, (folder / "manifest.json").relative_to(root).as_posix())
    if record.stat().st_size > 16 * 1024 * 1024:
        raise RecoveryError("recovery_evidence_missing", "恢复记录超出大小限制")
    manifest = json.loads(record.read_text(encoding="utf-8"))
    if (manifest.get("format_version") != FORMAT_VERSION or manifest.get("id") != folder.name
            or not isinstance(manifest.get("entries"), list) or not manifest["entries"]
            or manifest.get("recovery", {}).get("status") in {"restored", "restoring", "needs_attention"}):
        raise RecoveryError("recovery_evidence_missing", "恢复证据不完整、版本未知或上次恢复未完成；需要人工处理")
    seen: set[str] = set()
    for entry in manifest["entries"]:
        relative = entry.get("path")
        if (not isinstance(relative, str) or relative.casefold() in seen
                or relative.replace("\\", "/").split("/", 1)[0].casefold() == ".agent-backups"):
            raise RecoveryError("recovery_unsafe_path", "恢复记录包含重复或内部路径")
        seen.add(relative.casefold())
        checked_path(root, relative)
        if not _valid_state(entry.get("before")) or not _valid_state(entry.get("after")):
            raise RecoveryError("recovery_evidence_missing", "缺少完整的操作前后证据；不会覆盖当前文件", paths=[relative])
        if entry.get("existed") != entry["before"]["exists"]:
            raise RecoveryError("recovery_evidence_missing", "恢复记录存在性不一致", paths=[relative])
        _verify_backup(root, folder, entry)
    return manifest


def _verify_backup(root: Path, folder: Path, entry: dict[str, Any]) -> Path | None:
    name = entry.get("backup")
    if not entry["before"]["exists"]:
        if name is not None:
            raise RecoveryError("recovery_backup_invalid", "不存在的原路径有异常备份")
        return None
    if not isinstance(name, str) or Path(name).name != name or name in {".", "..", "manifest.json"}:
        raise RecoveryError("recovery_backup_invalid", "恢复备份路径无效")
    path = checked_path(root, (folder / name).relative_to(root).as_posix())
    if content_state(file_state(path)) != content_state(entry["before"]):
        raise RecoveryError("recovery_backup_invalid", "备份缺失或内容已变化；保留现场", paths=[entry["path"]])
    return path


def _overlap(left: str, right: str) -> bool:
    a, b = Path(left), Path(right)
    return a == b or a in b.parents or b in a.parents


def _overlay(state: dict[str, Any], path: str, changes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result = copy.deepcopy(state)
    target = Path(path)
    for changed, replacement in changes.items():
        origin = Path(changed)
        if origin == target or origin in target.parents:
            result = copy.deepcopy(replacement)
            for name in target.relative_to(origin).parts:
                result = copy.deepcopy(result.get("children", {}).get(name, {"exists": False, "type": None, "size": 0, "sha256": None}))
        elif target in origin.parents:
            node = result
            parts = origin.relative_to(target).parts
            for name in parts[:-1]:
                node = node.setdefault("children", {}).setdefault(name, {})
            if replacement["exists"]:
                node.setdefault("children", {})[parts[-1]] = copy.deepcopy(replacement)
            else:
                node.setdefault("children", {}).pop(parts[-1], None)
    return result


def preflight(root: Path, folders: list[Path]) -> tuple[list[tuple[Path, dict[str, Any]]], dict[str, dict[str, Any]]]:
    manifests = [(folder, _load(root, folder)) for folder in folders]
    current = {entry["path"]: file_state(checked_path(root, entry["path"]))
               for _, manifest in manifests for entry in manifest["entries"]}
    changes: dict[str, dict[str, Any]] = {}
    for _, manifest in manifests:
        for entry in reversed(manifest["entries"]):
            path = entry["path"]
            actual = _overlay(current[path], path, changes)
            virtual = any(_overlap(path, changed) for changed in changes)
            if (content_state(actual) != content_state(entry["after"]) if virtual else actual != entry["after"]):
                raise RecoveryError("recovery_conflict", "文件已被其他操作修改；整个撤销计划未执行", paths=[path])
            changes.pop(path, None)
            changes[path] = entry["before"]
    return manifests, current


@contextmanager
def restore_sequence(root: Path, folders: list[Path]):
    """Share exact evidence across separately authorized batch compensations.

    This context never grants permission: every tool still authorizes its own
    rollback proof. It only models identity changes caused by earlier restores.
    """
    if _restore_sequence.get() is not None:
        raise RecoveryError("recovery_sequence_conflict", "不能嵌套恢复计划")
    plans, expected = preflight(root, folders)
    token = _restore_sequence.set({"root": root, "plans": plans, "expected": expected, "failed": False})
    try:
        yield
    finally:
        _restore_sequence.reset(token)


def _rename_no_replace(source: Path, destination: Path) -> None:
    # Both names are relative to pinned native directory handles during recovery;
    # never reopen the destination by path after checking its ancestors.
    if os.name != "nt":
        raise RecoveryError("recovery_platform_unsupported", "安全恢复提交当前仅支持 Windows 的不覆盖重命名")
    parents = _pinned_handles.get()
    if destination.parent not in parents:
        os.rename(source, destination)
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    native = ctypes.WinDLL("ntdll")

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

    class UnicodeString(ctypes.Structure):
        _fields_ = [("length", wintypes.USHORT), ("maximum", wintypes.USHORT), ("buffer", wintypes.LPWSTR)]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.ULONG), ("root", wintypes.HANDLE),
                    ("name", ctypes.POINTER(UnicodeString)), ("attributes", wintypes.ULONG),
                    ("security", ctypes.c_void_p), ("quality", ctypes.c_void_p)]

    convert = native.RtlNtStatusToDosError
    convert.argtypes = [wintypes.LONG]
    convert.restype = wintypes.ULONG
    source_name = ctypes.create_unicode_buffer(source.name)
    encoded_length = len(source.name.encode("utf-16-le"))
    unicode_name = UnicodeString(encoded_length, encoded_length + 2, ctypes.cast(source_name, wintypes.LPWSTR))
    attributes = ObjectAttributes(ctypes.sizeof(ObjectAttributes), parents[source.parent], ctypes.pointer(unicode_name), 0x40, None, None)
    handle = wintypes.HANDLE()
    completion = IoStatusBlock()
    create = native.NtCreateFile
    create.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.ULONG, ctypes.POINTER(ObjectAttributes),
                       ctypes.POINTER(IoStatusBlock), ctypes.c_void_p, wintypes.ULONG,
                       wintypes.ULONG, wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG]
    create.restype = wintypes.LONG
    status = create(ctypes.byref(handle), 0x10000 | 0x80 | 0x100000, ctypes.byref(attributes),
                    ctypes.byref(completion), None, 0, 7, 1, 0x00200000 | 0x20 | 0x4000, None, 0)
    if status < 0:
        raise ctypes.WinError(convert(status))
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    try:
        name = destination.name
        name_length = len(name.encode("utf-16-le"))

        class RenameInformation(ctypes.Structure):
            _fields_ = [("replace", wintypes.BOOL), ("root", wintypes.HANDLE),
                        ("name_length", wintypes.DWORD), ("name", wintypes.WCHAR * (name_length // 2 + 1))]

        info = RenameInformation()
        info.replace = False
        info.root = parents[destination.parent]
        info.name_length = name_length
        info.name = name
        rename = native.NtSetInformationFile
        rename.argtypes = [wintypes.HANDLE, ctypes.POINTER(IoStatusBlock), ctypes.c_void_p,
                           wintypes.ULONG, ctypes.c_int]
        rename.restype = wintypes.LONG
        completion = IoStatusBlock()
        status = rename(handle, ctypes.byref(completion), ctypes.byref(info), ctypes.sizeof(info), 10)
        if status < 0:
            raise ctypes.WinError(convert(status))
    finally:
        close(handle)


@contextmanager
def _pinned_directories(*directories: Path):
    """Pin every Windows parent name against rename/reparse replacement.

    Rechecking a path cannot close a junction-swap race. Directory handles are
    opened without FILE_SHARE_DELETE and held through commit. Name replacement
    is prohibited. Child commits use these directory objects, not a new path
    traversal, so a raced reparse attribute cannot redirect a commit elsewhere.
    FILE_SHARE_WRITE is necessary for ordinary child commits on Windows.
    """
    if os.name != "nt":
        raise RecoveryError("recovery_platform_unsupported", "安全恢复提交仅支持 Windows")

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
            ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
            ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    inspect = kernel.GetFileInformationByHandle
    inspect.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    inspect.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    paths = {ancestor for directory in directories for ancestor in (directory, *directory.parents)}
    handles: list[Any] = []
    pinned: dict[Path, Any] = {}
    token = None
    try:
        for path in sorted(paths, key=lambda value: (len(value.parts), str(value).casefold())):
            # FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES, FILE_SHARE_READ|WRITE,
            # OPEN_EXISTING. Attributes-only handles do not participate in
            # Windows share-access enforcement and cannot pin a directory.
            # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT.
            access = 0x87 if path in directories else 0x81
            handle = create(str(path), access, 0x3, None, 3, 0x02000000 | 0x00200000, None)
            if handle == wintypes.HANDLE(-1).value:
                raise RecoveryError("recovery_parent_locked", "无法锁定恢复父目录；未提交新的文件变更")
            handles.append(handle)
            pinned[path] = handle
            information = ByHandleFileInformation()
            if (not inspect(handle, ctypes.byref(information))
                    or information.attributes & 0x400 or not information.attributes & 0x10):
                raise RecoveryError("recovery_unsafe_path", "恢复父目录不是稳定普通目录；停止提交")
        token = _pinned_handles.set(pinned)
        yield
    finally:
        if token is not None:
            _pinned_handles.reset(token)
        for handle in reversed(handles):
            close(handle)


def _restore_entry(root: Path, folder: Path, entry: dict[str, Any], expected: dict[str, Any],
                   authority_check: Callable[[], None] | None = None) -> dict[str, Any]:
    target = checked_path(root, entry["path"])
    with _pinned_directories(target.parent, folder):
        return _restore_pinned_entry(root, folder, entry, expected, authority_check)


def _restore_pinned_entry(root: Path, folder: Path, entry: dict[str, Any], expected: dict[str, Any],
                          authority_check: Callable[[], None] | None = None) -> dict[str, Any]:
    target = checked_path(root, entry["path"])
    backup = _verify_backup(root, folder, entry)
    nonce = uuid.uuid4().hex
    staged = folder / f"recovery-staged-{nonce}"
    displaced = folder / f"recovery-displaced-{nonce}"
    restored_state = {"exists": False, "type": None, "size": 0, "sha256": None}
    if backup:
        require_disk_space(folder, state_bytes(entry["before"]))
        if backup.is_dir():
            shutil.copytree(backup, staged)
        else:
            shutil.copy2(backup, staged)
        restored_state = file_state(staged)
        if content_state(restored_state) != content_state(entry["before"]):
            raise RecoveryError("recovery_backup_invalid", "恢复暂存副本校验失败")
    # Revalidate after potentially slow staging, immediately before commit.
    target = checked_path(root, entry["path"])
    if file_state(target) != expected:
        raise RecoveryError("recovery_conflict", "文件在恢复提交前变化；未覆盖当前内容", paths=[entry["path"]])
    if backup and file_state(staged) != restored_state:
        raise RecoveryError("recovery_backup_invalid", "恢复暂存副本在提交前变化")
    if authority_check:
        authority_check()
    if expected["exists"]:
        _rename_no_replace(target, displaced)
        if file_state(displaced) != expected:
            # Atomic rename captures what actually occupied the name. Preserve
            # unexpected data and restore the name only if it remains vacant.
            try:
                _rename_no_replace(displaced, target)
            except OSError:
                pass
            raise RecoveryError("recovery_conflict", "提交时检测到并发修改；内容保留在原路径或恢复区", paths=[entry["path"]])
    if backup:
        # The original parent must still exist; do not create extra unapproved
        # ancestors or follow a parent that was exchanged for a link.
        checked_path(root, entry["path"])
        if file_state(staged) != restored_state:
            raise RecoveryError("recovery_backup_invalid", "恢复暂存副本在提交期间变化；现场已保留")
        if authority_check:
            authority_check()
        _rename_no_replace(staged, target)
        if file_state(target) != restored_state:
            raise RecoveryError("recovery_conflict", "恢复内容在提交期间变化；现场已保留", paths=[entry["path"]])
    return restored_state


def restore_plan(root: Path, folders: list[Path], *, authority_check: Callable[[], None] | None = None) -> list[dict[str, Any]]:
    sequence = _restore_sequence.get()
    if sequence is None:
        plans, expected = preflight(root, folders)
    else:
        plans = sequence["plans"][:len(folders)]
        if sequence["failed"] or sequence["root"] != root or [folder for folder, _ in plans] != folders:
            raise RecoveryError("recovery_sequence_conflict", "恢复计划已停止或顺序不匹配；不会继续修改")
        expected = sequence["expected"]
    results: list[dict[str, Any]] = []
    restored: list[str] = []
    for folder, manifest in plans:
        # Manifest/backup edits after whole-plan preflight must also fail closed.
        try:
            if _load(root, folder) != manifest:
                raise RecoveryError("recovery_evidence_changed", "恢复记录在预检后变化")
        except RecoveryError as exc:
            if sequence is not None:
                sequence["failed"] = True
            exc.restored = restored
            raise
        actor_task, actor_call = _recovery_actor.get()
        manifest["recovery"] = {"status": "restoring", "restored": [], "started_at": time.time(),
                                "task_id": actor_task or manifest.get("task_id"),
                                "tool_call_id": actor_call, "after": {}}
        entry = manifest["entries"][-1]
        try:
            write_manifest(folder, manifest)
            for entry in reversed(manifest["entries"]):
                if authority_check:
                    authority_check()
                path = entry["path"]
                restored_state = _restore_entry(root, folder, entry, expected[path], authority_check)
                restored.append(path)
                manifest["recovery"]["restored"].append(path)
                manifest["recovery"]["after"][path] = restored_state
                write_manifest(folder, manifest)
                # Capture effects of OUR restore before starting the next one.
                # This accommodates repeated paths and parent/child dependencies
                # without ever weakening the next step's identity comparison.
                for related in expected:
                    if _overlap(path, related):
                        expected[related] = _overlay(expected[related], related, {path: restored_state})
            manifest["recovery"]["status"] = "restored"
            manifest["recovery"]["completed_at_ns"] = time.time_ns()
            write_manifest(folder, manifest)
            results.append({"change_id": manifest["id"], "task_id": manifest.get("task_id"),
                            "restored": manifest["recovery"]["restored"], "backup_retained": True})
            if sequence is not None:
                sequence["plans"].pop(0)
        except Exception as exc:
            if sequence is not None:
                sequence["failed"] = True
            manifest["recovery"].update(status="needs_attention", error_code=getattr(exc, "code", "recovery_io_error"))
            try:
                write_manifest(folder, manifest)
            except OSError:
                pass
            if isinstance(exc, RecoveryError):
                exc.restored = restored
                raise
            raise RecoveryError("recovery_io_error", "恢复未完成；原备份及移出的当前内容均保留", paths=[entry["path"]], restored=restored) from exc
    return results
