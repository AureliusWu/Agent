"""Bounded, retained-handle ShellLink reads. No resolve, launch or MSI repair.

Returned paths and descriptor fields are private observations, never public
diagnostics. The installed lifecycle still decides exact package ownership.
"""
from __future__ import annotations

import ctypes
import hashlib
import ntpath
import os
from pathlib import Path
import re
import struct
import uuid

from rc_owned_desktop import ordinary

MAX_LINK_BYTES = 1024 * 1024
MAX_TEXT = 32768
PROTOCOL = "native-shell-link-readonly-v1"
CODES = frozenset({"FILE_INPUT_INVALID", "FILE_LOCK_FAILED", "FILE_UNSAFE", "FILE_READ_FAILED", "FILE_CHANGED",
    "FILE_CLOSE_FAILED", "LNK_HEADER_INVALID", "LNK_FLAGS_UNKNOWN", "DARWIN_BYTES_PRESENT", "COM_INIT_FAILED",
    "COM_CREATE_FAILED", "COM_QUERY_FAILED", "COM_LOAD_FAILED", "COM_PATH_FAILED", "COM_ARGUMENTS_FAILED",
    "COM_IDLIST_FAILED", "PIDL_NOT_FILESYSTEM", "PATH_CONFLICT", "TARGET_UNKNOWN", "MSI_DESCRIPTOR_PRESENT",
    "MSI_STATUS_UNKNOWN", "WSH_CONFLICT", "NATIVE_OBSERVATION_INVALID", "COM_CLEANUP_FAILED"})


class ShortcutReadError(ValueError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in CODES else "NATIVE_OBSERVATION_INVALID"
        super().__init__(self.code)


def need(condition, code):
    if not condition:
        raise ShortcutReadError(code)


def link_header(data):
    """Reject Darwin hints conservatively, without inventing an LNK parser."""
    need(type(data) is bytes and 76 <= len(data) <= MAX_LINK_BYTES, "LNK_HEADER_INVALID")
    need(data[:4] == b"L\0\0\0" and data[4:20] == uuid.UUID("00021401-0000-0000-c000-000000000046").bytes_le
         and data[66:76] == b"\0" * 10, "LNK_HEADER_INVALID")
    flags = struct.unpack_from("<I", data, 20)[0]
    need(flags & 0xF8000000 == 0, "LNK_FLAGS_UNKNOWN")
    # A malformed descriptor must not become ordinary merely because MSI API
    # returns an error. A signature anywhere is deliberately conservative.
    need(not flags & 0x1000 and b"\x06\0\0\xa0" not in data, "DARWIN_BYTES_PRESENT")
    return {"has_idlist": bool(flags & 1), "has_link_info": bool(flags & 2), "darwin_bytes_absent": True}


def selected_identity(value):
    """Choose only actual, consistent filesystem observations, never expected paths."""
    need(isinstance(value, dict), "NATIVE_OBSERVATION_INVALID")
    raw, pidl, arguments = (value.get(name) for name in ("raw_target", "pidl_target", "arguments"))
    need(all(type(item) is str and len(item) < MAX_TEXT - 1 and "\0" not in item for item in (raw, arguments)), "NATIVE_OBSERVATION_INVALID")
    need(type(value.get("pidl_present")) is bool and type(value.get("msi_status")) is int, "NATIVE_OBSERVATION_INVALID")
    need(value["msi_status"] != 0, "MSI_DESCRIPTOR_PRESENT")
    # 1627 is the measured ordinary-link failure, not proof of no descriptor;
    # it is accepted only together with the independently checked locked bytes.
    need(value["msi_status"] == 1627 and value.get("descriptor_fields_empty") is True, "MSI_STATUS_UNKNOWN")
    need(value.get("darwin_bytes_absent") is True, "DARWIN_BYTES_PRESENT")
    need(type(value.get("raw_status")) is int and value["raw_status"] in {0, 1}
         and (value["raw_status"] == 0) == bool(raw), "COM_PATH_FAILED")
    need(value.get("arguments_status") == 0 and type(value.get("arguments_status")) is int, "COM_ARGUMENTS_FAILED")
    need(type(value.get("pidl_status")) is int and value["pidl_status"] in {0, 1}
         and (value["pidl_status"] == 0) == value["pidl_present"], "COM_IDLIST_FAILED")
    if value["pidl_present"]:
        need(type(pidl) is str and 0 < len(pidl) < MAX_TEXT - 1 and "\0" not in pidl and value.get("pidl_filesystem") is True,
             "PIDL_NOT_FILESYSTEM")
    else:
        need(pidl is None and value.get("pidl_filesystem") is None, "NATIVE_OBSERVATION_INVALID")
    normalize = lambda text: ntpath.normcase(ntpath.normpath(text)).rstrip("\\/")
    if raw and pidl:
        need(normalize(raw) == normalize(pidl), "PATH_CONFLICT")
    target = raw or pidl
    drive, tail = ntpath.splitdrive(target) if type(target) is str else ("", "")
    need(type(target) is str and bool(target) and ntpath.isabs(target)
         and re.fullmatch(r"[A-Za-z]:", drive) is not None and tail.startswith(("\\", "/"))
         and not target.startswith(("\\\\", "//")) and ".." not in re.split(r"[\\/]", target), "TARGET_UNKNOWN")
    return target, arguments


class _Guid(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class _Time(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _FileInfo(ctypes.Structure):
    _fields_ = [("attributes", ctypes.c_uint32), ("created", _Time), ("accessed", _Time), ("written", _Time),
                ("volume", ctypes.c_uint32), ("size_high", ctypes.c_uint32), ("size_low", ctypes.c_uint32),
                ("links", ctypes.c_uint32), ("index_high", ctypes.c_uint32), ("index_low", ctypes.c_uint32)]


class _Native:
    def __init__(self):
        need(os.name == "nt", "FILE_INPUT_INVALID")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ole = ctypes.WinDLL("ole32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.msi = ctypes.WinDLL("msi", use_last_error=True)
        signatures = {
            (self.kernel, "CreateFileW"): ([ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p], ctypes.c_void_p),
            (self.kernel, "GetFileInformationByHandle"): ([ctypes.c_void_p, ctypes.POINTER(_FileInfo)], ctypes.c_int),
            (self.kernel, "SetFilePointerEx"): ([ctypes.c_void_p, ctypes.c_int64, ctypes.POINTER(ctypes.c_int64), ctypes.c_uint32], ctypes.c_int),
            (self.kernel, "ReadFile"): ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p], ctypes.c_int),
            (self.kernel, "CloseHandle"): ([ctypes.c_void_p], ctypes.c_int),
            (self.ole, "CoInitializeEx"): ([ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int32),
            (self.ole, "CoUninitialize"): ([], None),
            (self.ole, "CoCreateInstance"): ([ctypes.POINTER(_Guid), ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int32),
            (self.ole, "CoTaskMemFree"): ([ctypes.c_void_p], None),
            (self.shell, "SHGetPathFromIDListEx"): ([ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32], ctypes.c_int),
            (self.msi, "MsiGetShortcutTargetW"): ([ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p], ctypes.c_uint32),
        }
        for (library, name), (arguments, result) in signatures.items():
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result

    def open(self, path):
        # FILE_SHARE_READ only: retained file denies concurrent write/delete.
        handle = self.kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
        need(handle is not None and handle != ctypes.c_void_p(-1).value, "FILE_LOCK_FAILED")
        return handle

    def close(self, handle):
        need(bool(self.kernel.CloseHandle(handle)), "FILE_CLOSE_FAILED")

    def info(self, handle):
        info = _FileInfo()
        need(bool(self.kernel.GetFileInformationByHandle(handle, ctypes.byref(info))), "FILE_READ_FAILED")
        need(not info.attributes & (0x400 | 0x10) and info.links == 1, "FILE_UNSAFE")
        size = (info.size_high << 32) | info.size_low
        need(76 <= size <= MAX_LINK_BYTES, "FILE_UNSAFE")
        return {"volume": info.volume, "file_index": (info.index_high << 32) | info.index_low,
                "bytes": size, "links": info.links, "attributes": info.attributes,
                "created": (info.created.high << 32) | info.created.low,
                "written": (info.written.high << 32) | info.written.low}

    def contents(self, handle, size):
        need(bool(self.kernel.SetFilePointerEx(handle, 0, None, 0)), "FILE_READ_FAILED")
        chunks, total = [], 0
        while True:
            block, count = ctypes.create_string_buffer(65536), ctypes.c_uint32()
            need(bool(self.kernel.ReadFile(handle, block, len(block), ctypes.byref(count), None)), "FILE_READ_FAILED")
            if count.value == 0:
                break
            total += count.value
            need(total <= MAX_LINK_BYTES and total <= size, "FILE_CHANGED")
            chunks.append(block.raw[:count.value])
        need(total == size, "FILE_CHANGED")
        return b"".join(chunks)

    def method(self, pointer, slot, result, *arguments):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *arguments)(table[slot])

    def cleanup(self, pidl, persist, link, initialized):
        """Attempt every owned cleanup even if an earlier release fails."""
        completed = True
        actions = []
        if pidl.value:
            actions.append(lambda: self.ole.CoTaskMemFree(pidl))
        for pointer in (persist, link):
            if pointer.value:
                actions.append(lambda pointer=pointer: self.method(pointer, 2, ctypes.c_uint32)(pointer))
        if initialized:
            actions.append(self.ole.CoUninitialize)
        for action in actions:
            try:
                action()
            except Exception:
                completed = False
        return completed

    def observe(self, path):
        link, persist, pidl = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        initialized, failure = False, None
        try:
            status = self.ole.CoInitializeEx(None, 2)
            need(status in {0, 1}, "COM_INIT_FAILED")
            initialized = True
            cls = _Guid.parse("00021401-0000-0000-c000-000000000046")
            iid = _Guid.parse("000214f9-0000-0000-c000-000000000046")
            need(self.ole.CoCreateInstance(ctypes.byref(cls), None, 1, ctypes.byref(iid), ctypes.byref(link)) == 0 and link.value,
                 "COM_CREATE_FAILED")
            persist_iid = _Guid.parse("0000010b-0000-0000-c000-000000000046")
            query = self.method(link, 0, ctypes.c_int32, ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p))
            need(query(link, ctypes.byref(persist_iid), ctypes.byref(persist)) == 0 and persist.value, "COM_QUERY_FAILED")
            load = self.method(persist, 5, ctypes.c_int32, ctypes.c_wchar_p, ctypes.c_uint32)
            need(load(persist, str(path), 0) == 0, "COM_LOAD_FAILED")  # STGM_READ only.
            raw, arguments, filesystem = (ctypes.create_unicode_buffer(MAX_TEXT) for _ in range(3))
            get_path = self.method(link, 3, ctypes.c_int32, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
            raw_status = get_path(link, raw, MAX_TEXT, None, 4)  # SLGP_RAWPATH.
            get_arguments = self.method(link, 10, ctypes.c_int32, ctypes.c_wchar_p, ctypes.c_int)
            args_status = get_arguments(link, arguments, MAX_TEXT)
            get_id = self.method(link, 4, ctypes.c_int32, ctypes.POINTER(ctypes.c_void_p))
            pidl_status = get_id(link, ctypes.byref(pidl))
            converted = bool(self.shell.SHGetPathFromIDListEx(pidl, filesystem, MAX_TEXT, 0)) if pidl.value else None
            product, feature, component = (ctypes.create_unicode_buffer(39) for _ in range(3))
            msi_status = int(self.msi.MsiGetShortcutTargetW(str(path), product, feature, component))
            return {"raw_target": raw.value, "arguments": arguments.value, "pidl_target": filesystem.value if converted else None,
                    "raw_status": raw_status, "arguments_status": args_status, "pidl_status": pidl_status,
                    "pidl_present": bool(pidl.value), "pidl_filesystem": converted,
                    "msi_status": msi_status, "product_code": product.value, "feature_id": feature.value,
                    "component_code": component.value, "descriptor_fields_empty": not any((product.value, feature.value, component.value))}
        except BaseException as error:
            failure = error
            raise
        finally:
            cleaned = self.cleanup(pidl, persist, link, initialized)
            if not cleaned:
                if failure is None:
                    error = ShortcutReadError("COM_CLEANUP_FAILED")
                    error.diagnostics = {"cleanup_completed": False}
                    raise error
                try:
                    failure.diagnostics = {**getattr(failure, "diagnostics", {}), "cleanup_completed": False}
                except Exception:
                    pass


def read_shortcut(path: Path, expected_sha256: str):
    """Retain the exact ordinary link while both native target paths are read."""
    need(isinstance(path, Path) and path.is_absolute() and path.suffix.casefold() == ".lnk"
         and type(expected_sha256) is str and re.fullmatch(r"[0-9a-f]{64}", expected_sha256), "FILE_INPUT_INVALID")
    try:
        ordinary(path)
    except (OSError, ValueError):
        raise ShortcutReadError("FILE_UNSAFE") from None
    native, handle, observed = _Native(), None, None
    try:
        handle = native.open(path)
        before = native.info(handle)
        data = native.contents(handle, before["bytes"])
        need(hashlib.sha256(data).hexdigest() == expected_sha256, "FILE_CHANGED")
        header = link_header(data)
        observed = native.observe(path)
        observed.update(header)
        target, arguments = selected_identity(observed)
        try:
            ordinary(path)
        except (OSError, ValueError):
            raise ShortcutReadError("FILE_UNSAFE") from None
        reopened = native.open(path)
        try:
            need(native.info(reopened) == before, "FILE_CHANGED")
        finally:
            native.close(reopened)
        need(native.info(handle) == before and native.contents(handle, before["bytes"]) == data, "FILE_CHANGED")
        return {"protocol_version": PROTOCOL, "target": target, "arguments": arguments,
                "native": observed, "file_identity": before, "sha256": expected_sha256,
                "deny_write_delete_retained": True, "identity_stable": True}
    except ShortcutReadError as error:
        status = observed if isinstance(observed, dict) else {}
        error.diagnostics = {**getattr(error, "diagnostics", {}),
            **{key: status.get(key) for key in ("raw_status", "arguments_status", "pidl_status", "msi_status", "pidl_present", "pidl_filesystem")}}
        raise
    finally:
        if handle is not None:
            native.close(handle)


def combine_wsh(row, native):
    """Preserve WSH raw values; real nonempty disagreement remains a failure."""
    need(isinstance(row, dict) and isinstance(native, dict) and native.get("protocol_version") == PROTOCOL
         and native.get("identity_stable") is True and native.get("deny_write_delete_retained") is True,
         "NATIVE_OBSERVATION_INVALID")
    target, arguments = native.get("target"), native.get("arguments")
    need(type(target) is str and bool(target) and type(arguments) is str, "NATIVE_OBSERVATION_INVALID")
    wsh_target, wsh_args = row.get("target"), row.get("arguments")
    need(type(wsh_target) is str and type(wsh_args) is str, "NATIVE_OBSERVATION_INVALID")
    normalize = lambda text: ntpath.normcase(ntpath.normpath(text)).rstrip("\\/")
    need(not wsh_target or normalize(wsh_target) == normalize(target), "WSH_CONFLICT")
    need(not wsh_args or wsh_args == arguments, "WSH_CONFLICT")
    return {**row, "wsh_target": wsh_target, "wsh_arguments": wsh_args, "target": target, "arguments": arguments,
            "native_read": native}
