from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import sandbox
from app.workspace import file_recovery


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows recovery commit contract")


def _changed_file(root: Path) -> tuple[Path, Path]:
    target = root / "review.txt"
    target.write_text("original", encoding="utf-8")
    changed = sandbox.execute_tool(str(root), "full", "write_file", {
        "path": "review.txt", "content": "agent edit",
        "expected_version_token": sandbox.file_version_token(target),
    })
    assert changed["success"], changed
    return target, root / ".agent-backups" / changed["change_id"]


def test_revoke_during_staging_must_prevent_recovery_commit(tmp_path: Path, monkeypatch) -> None:
    target, folder = _changed_file(tmp_path)
    allowed = True

    def check_authority() -> None:
        if not allowed:
            raise file_recovery.RecoveryError("recovery_authority_changed", "test revocation")

    copy = file_recovery.shutil.copy2

    def revoke_after_copy(source, destination, *args, **kwargs):
        nonlocal allowed
        result = copy(source, destination, *args, **kwargs)
        if Path(destination).name.startswith("recovery-staged-"):
            allowed = False
        return result

    monkeypatch.setattr(file_recovery.shutil, "copy2", revoke_after_copy)
    with pytest.raises(file_recovery.RecoveryError) as failure:
        file_recovery.restore_plan(tmp_path, [folder], authority_check=check_authority)
    assert not allowed, "probe must revoke authority during staging"
    assert failure.value.code == "recovery_authority_changed"
    assert target.read_text(encoding="utf-8") == "agent edit"
    assert (folder / "0.bak").read_text(encoding="utf-8") == "original"


def test_in_place_parent_reparse_at_commit_cannot_redirect_native_rename(tmp_path: Path, monkeypatch) -> None:
    """Unlike parent replacement, setting a reparse tag may retain its inode."""
    import ctypes
    import struct
    from ctypes import wintypes

    root = tmp_path / "workspace"
    parent = root / "parent"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = parent / "review.txt"
    target.write_text("original", encoding="utf-8")
    changed = sandbox.execute_tool(str(root), "full", "write_file", {
        "path": "parent/review.txt", "content": "agent edit",
        "expected_version_token": sandbox.file_version_token(target),
    })
    assert changed["success"], changed
    folder = root / ".agent-backups" / changed["change_id"]
    native_rename = file_recovery._rename_no_replace
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    ioctl = kernel.DeviceIoControl
    ioctl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                     ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    ioctl.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    attempted = False
    reparse_set = False

    def reparse_control(code: int, data: bytes) -> bool:
        handle = create(str(parent), 0x40000000, 7, None, 3, 0x02000000 | 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            return False
        try:
            buffer = ctypes.create_string_buffer(data)
            returned = wintypes.DWORD()
            return bool(ioctl(handle, code, buffer, len(data), None, 0, ctypes.byref(returned), None))
        finally:
            close(handle)

    def reparse_before_commit(source, destination):
        nonlocal attempted, reparse_set
        if Path(source).name.startswith("recovery-staged-"):
            attempted = True
            substitute = ("\\??\\" + str(outside)).encode("utf-16-le")
            printable = str(outside).encode("utf-16-le")
            names = substitute + b"\0\0" + printable + b"\0\0"
            data = struct.pack("<LHHHHHH", 0xA0000003, 8 + len(names), 0,
                               0, len(substitute), len(substitute) + 2, len(printable)) + names
            reparse_set = reparse_control(0x000900A4, data)
        return native_rename(source, destination)

    monkeypatch.setattr(file_recovery, "_rename_no_replace", reparse_before_commit)
    try:
        try:
            file_recovery.restore_plan(root, [folder])
        except file_recovery.RecoveryError:
            pass
        assert attempted
        assert reparse_set, "probe must successfully inject the in-place reparse tag to test root-relative rename"
        assert not (outside / "review.txt").exists(), "native root-relative rename followed an in-place reparse tag"
        assert (folder / "0.bak").read_text(encoding="utf-8") == "original"
    finally:
        if reparse_set:
            # Remove only the test-owned junction tag, never recurse through it.
            assert reparse_control(0x000900AC, struct.pack("<LHH", 0xA0000003, 0, 0))


def test_staging_tamper_after_hash_must_not_be_reported_restored(tmp_path: Path, monkeypatch) -> None:
    _target, folder = _changed_file(tmp_path)
    rename = file_recovery._rename_no_replace
    injected = False

    def tamper_staged_before_rename(source, destination):
        nonlocal injected
        if Path(source).name.startswith("recovery-staged-"):
            Path(source).write_text("tampered staging", encoding="utf-8")
            injected = True
        return rename(source, destination)

    monkeypatch.setattr(file_recovery, "_rename_no_replace", tamper_staged_before_rename)
    with pytest.raises(file_recovery.RecoveryError):
        file_recovery.restore_plan(tmp_path, [folder])
    assert injected
    assert (folder / "0.bak").read_text(encoding="utf-8") == "original"
    assert any(item.read_text(encoding="utf-8") == "agent edit" for item in folder.glob("recovery-displaced-*"))


def test_parent_junction_swap_at_commit_must_not_write_outside_workspace(tmp_path: Path, monkeypatch) -> None:
    import _winapi

    root = tmp_path / "workspace"
    parent = root / "parent"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = parent / "review.txt"
    target.write_text("original", encoding="utf-8")
    changed = sandbox.execute_tool(str(root), "full", "write_file", {
        "path": "parent/review.txt", "content": "agent edit",
        "expected_version_token": sandbox.file_version_token(target),
    })
    assert changed["success"], changed
    folder = root / ".agent-backups" / changed["change_id"]
    rename = file_recovery._rename_no_replace
    attempted = False

    def swap_parent_before_commit(source, destination):
        nonlocal attempted
        if Path(source).name.startswith("recovery-staged-"):
            attempted = True
            # Both the selected workspace and the outside sentinel are owned
            # by this test. No user paths, permission prompt or app is used.
            parent.rename(root / "retained-parent")
            _winapi.CreateJunction(str(outside), str(parent))
        return rename(source, destination)

    monkeypatch.setattr(file_recovery, "_rename_no_replace", swap_parent_before_commit)
    try:
        file_recovery.restore_plan(root, [folder])
    except file_recovery.RecoveryError:
        pass
    assert attempted, "probe must reach the parent replacement commit window"
    assert not (outside / "review.txt").exists(), "recovery followed a raced junction outside the workspace"
    assert (folder / "0.bak").read_text(encoding="utf-8") == "original"
