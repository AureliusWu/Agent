"""Windows desktop process ownership and retained acceptance data.

Importing this module never starts a process. Native launch uses
CREATE_SUSPENDED, and ResumeThread is unreachable until assignment to an exact
test-owned KILL_ON_JOB_CLOSE job succeeds. No PID is used for termination.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Sequence
import uuid


class _IO(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
               ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
               ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
               ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
               ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IO),
               ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
               ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in (
        "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")]
    _fields_ += [(name, wintypes.DWORD) for name in (
        "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]


class _Startup(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
               ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
               ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
               ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
               ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
               ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
               ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
               ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
               ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
               ("hStdError", wintypes.HANDLE)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
               ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class _Win32:
    def __init__(self):
        if os.name != "nt":
            raise ValueError("owned desktop launch requires Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "GetProcessTimes": ([wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                 ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "IsProcessInJob": ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "CreateProcessW": ([wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                ctypes.POINTER(_Startup), ctypes.POINTER(_ProcessInfo)], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result

    def _require(self, value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())
        return value

    def create_job(self) -> Any:
        handle = self._require(self.kernel.CreateJobObjectW(None, None))
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        try:
            self._require(self.kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close(handle)
            raise
        return handle

    def create_suspended(self, executable: Path, cwd: Path, environment: dict[str, str],
                         arguments: Sequence[str] = (), *, show_window: bool = True) -> tuple[int, Any, Any]:
        # The exact executable, not a shell or a Python venv launcher, is born
        # suspended. Handle inheritance is disabled; the child cannot keep the
        # Job alive after its owner exits.
        if len({key.casefold() for key in environment}) != len(environment):
            raise ValueError("case-duplicate Windows environment keys")
        if any(not isinstance(key, str) or not isinstance(value, str) or not key or "=" in key or "\0" in key + value
               for key, value in environment.items()):
            raise ValueError("invalid native environment entry")
        block = "\0".join(key + "=" + environment[key] for key in sorted(environment, key=str.casefold)) + "\0\0"
        native_environment = ctypes.create_unicode_buffer(block)
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(executable), *arguments]))
        startup, information = _Startup(), _ProcessInfo()
        startup.cb, startup.dwFlags, startup.wShowWindow = ctypes.sizeof(startup), 1, 1 if show_window else 0
        self._require(self.kernel.CreateProcessW(str(executable), command, None, None, False,
                      0x4 | 0x400 | (0 if show_window else 0x08000000), native_environment, str(cwd),
                      ctypes.byref(startup), ctypes.byref(information)))
        return int(information.dwProcessId), information.hProcess, information.hThread

    def assign(self, job, process):
        self._require(self.kernel.AssignProcessToJobObject(job, process))

    def resume(self, thread):
        result = self.kernel.ResumeThread(thread)
        if result != 1:
            raise OSError("primary thread was not suspended exactly once")

    def wait(self, process, milliseconds):
        result = self.kernel.WaitForSingleObject(process, milliseconds)
        if result not in (0, 258):
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0

    def exit_code(self, process):
        result = wintypes.DWORD()
        self._require(self.kernel.GetExitCodeProcess(process, ctypes.byref(result)))
        return int(result.value)

    def creation_time(self, process):
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        self._require(self.kernel.GetProcessTimes(process, ctypes.byref(created), ctypes.byref(exited),
                                                 ctypes.byref(kernel), ctypes.byref(user)))
        return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)

    def terminate_handle(self, process):
        self._require(self.kernel.TerminateProcess(process, 124))

    def active(self, job):
        information = _Accounting()
        self._require(self.kernel.QueryInformationJobObject(job, 1, ctypes.byref(information), ctypes.sizeof(information), None))
        return int(information.ActiveProcesses)

    def terminate_job(self, job):
        self._require(self.kernel.TerminateJobObject(job, 124))

    def close(self, handle):
        self._require(self.kernel.CloseHandle(handle))

    def open_observed(self, pid):
        if type(pid) is not int or pid <= 0:
            raise ValueError("actual observed process ID is invalid")
        # Query/synchronize only: this handle can never terminate by PID.
        return self._require(self.kernel.OpenProcess(0x1000 | 0x100000, False, pid))

    def is_member(self, process, job):
        result = wintypes.BOOL()
        self._require(self.kernel.IsProcessInJob(process, job, ctypes.byref(result)))
        return bool(result.value)

    def process_path(self, process):
        size, result = wintypes.DWORD(32768), ctypes.create_unicode_buffer(32768)
        self._require(self.kernel.QueryFullProcessImageNameW(process, 0, result, ctypes.byref(size)))
        return Path(result.value)


class RetainedProcess:
    def __init__(self, api: Any, handle: Any, pid: int, executable: Path):
        self.api, self.handle, self.pid, self.executable = api, handle, pid, executable
        self.creation_time = None
        self.assigned = False
        self.resumed = False
        self.stop_confirmed = False

    def capture_identity(self) -> None:
        value = self.api.creation_time(self.handle)
        if type(value) is not int or value <= 0:
            raise ValueError("retained process has no actual creation identity")
        self.creation_time = value

    def poll(self) -> int | None:
        return self.api.exit_code(self.handle) if self.api.wait(self.handle, 0) else None

    def wait(self, timeout: float) -> int:
        if not self.api.wait(self.handle, max(0, min(round(timeout * 1000), 0xFFFFFFFE))):
            raise subprocess.TimeoutExpired(str(self.executable), timeout)
        return self.api.exit_code(self.handle)

    def close(self) -> None:
        if self.handle is not None:
            self.api.close(self.handle)
            self.handle = None


class OwnedDesktopJob:
    def __init__(self, api: Any = None):
        self.api = api if api is not None else _Win32()
        self.handle = self.api.create_job()
        self.processes = []
        self.query_handles = []
        self.thread_handles = []
        self.cleanup_result = None
        self.accounting_after_close = None
        self.forced_history = False
        self.launch_events = []
        self.launch_cleanup_errors = []

    def launch(self, executable: Path, environment: dict[str, str], arguments: Sequence[str] = (),
               *, show_window: bool = True) -> RetainedProcess:
        if self.handle is None:
            raise ValueError("owned Job has already been closed")
        executable = Path(executable)
        if not executable.is_absolute() or any(not isinstance(item, str) or "\0" in item for item in arguments):
            raise ValueError("owned launch requires an absolute executable and valid argument strings")
        ordinary(executable)
        pid, handle, thread = self.api.create_suspended(
            executable, executable.parent, environment, arguments, show_window=show_window)
        self.thread_handles.append(thread)
        process = None
        try:
            # Once CreateProcess succeeds every later failure owns and closes
            # the exact suspended handles, including GetProcessTimes failure.
            process = RetainedProcess(self.api, handle, pid, executable)
            self.processes.append(process)
            self.launch_events.append("created_suspended")
            process.capture_identity()
            self.api.assign(self.handle, handle)
            process.assigned = True
            if not self.api.is_member(handle, self.handle):
                raise ValueError("desktop is not in the exact owner Job")
            if self.api.process_path(handle).resolve() != executable.resolve():
                raise ValueError("retained desktop handle executes a different image")
            self.api.resume(thread)
            process.resumed = True
            self.launch_events.append("resumed")
            return process
        except BaseException:
            # Exact retained native handle, including unassigned suspended
            # failures. Never kill by a PID that may have been reused.
            # A failed liveness query must never skip exact-handle termination.
            self.forced_history = True
            try:
                self.api.terminate_handle(handle)
            except OSError as exc:
                self.launch_cleanup_errors.append(str(exc))
            try:
                stopped = self.api.wait(handle, 10_000)
                if process is not None:
                    process.stop_confirmed = stopped
                if not stopped:
                    self.launch_cleanup_errors.append("suspended owned process stop was not confirmed")
            except OSError as exc:
                self.launch_cleanup_errors.append(str(exc))
            # Normally RetainedProcess construction cannot fail; retain this
            # emergency handle as a regular owned record rather than lose it.
            if process is None:
                process = RetainedProcess(self.api, handle, pid, executable)
                self.processes.append(process)
            raise
        finally:
            self.api.close(thread)
            self.thread_handles.remove(thread)

    def observe_sidecar(self, pid: int, expected: Path) -> RetainedProcess:
        if self.handle is None:
            raise ValueError("owned Job has already been closed")
        handle = self.api.open_observed(pid)
        process = RetainedProcess(self.api, handle, pid, expected)
        self.query_handles.append(process)
        try:
            process.capture_identity()
            if (process.poll() is not None or not self.api.is_member(handle, self.handle)
                    or self.api.process_path(handle).resolve() != expected.resolve()
                    or not self.processes or process.creation_time < self.processes[0].creation_time):
                raise ValueError("sidecar is not the running expected binary in the exact owner Job")
        except BaseException:
            # This may be a foreign process. The handle is query/synchronize
            # only and is never promoted into termination ownership on failure.
            process.close()
            self.query_handles.remove(process)
            raise
        process.assigned = True
        self.query_handles.remove(process)
        self.processes.append(process)
        return process

    def wait_empty(self, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        while True:
            count = self.api.active(self.handle)
            if count == 0:
                return 0
            if time.monotonic() >= deadline:
                raise ValueError("exact desktop Job still contains active owned processes")
            time.sleep(.05)

    def cleanup(self) -> dict[str, Any]:
        """Return real native accounting; forced cleanup never proves shutdown."""
        if self.cleanup_result is not None:
            return {**self.cleanup_result, "errors": list(self.cleanup_result["errors"])}
        result = {"protocol_version": "exact-native-job-v1", "active_before_cleanup": None,
                  "active_after_cleanup": None, "forced_termination": None,
                  "job_handle_closed": self.handle is None, "unassigned_cleanup_complete": False,
                  "owned_process_handles_remaining": None, "owned_thread_handles_remaining": None,
                  "launch_events": list(self.launch_events),
                  "launch_cleanup_errors": list(self.launch_cleanup_errors), "errors": []}
        try:
            for process in self.processes:
                if process.assigned or process.handle is None or process.stop_confirmed:
                    continue
                stopped = False
                try:
                    stopped = self.api.wait(process.handle, 0)
                except OSError as exc:
                    result["errors"].append(str(exc))
                if not stopped:
                    self.forced_history = True
                    try:
                        self.api.terminate_handle(process.handle)
                    except OSError as exc:
                        result["errors"].append(str(exc))
                    # Independently attempt observation even when termination
                    # or the earlier query failed. Unknown never becomes zero.
                    try:
                        stopped = self.api.wait(process.handle, 10_000)
                    except OSError as exc:
                        result["errors"].append(str(exc))
                process.stop_confirmed = stopped
            result["unassigned_cleanup_complete"] = all(
                process.assigned or process.stop_confirmed for process in self.processes)
            if self.handle is not None:
                before = None
                try:
                    before = self.api.active(self.handle)
                    result["active_before_cleanup"] = before
                except OSError as exc:
                    result["errors"].append(str(exc))
                if before != 0:
                    self.forced_history = True
                    try:
                        self.api.terminate_job(self.handle)
                    except OSError as exc:
                        result["errors"].append(str(exc))
                try:
                    result["active_after_cleanup"] = self.wait_empty(15)
                    self.accounting_after_close = result["active_after_cleanup"]
                except (OSError, ValueError) as exc:
                    result["errors"].append(str(exc))
            else:
                # No new child can enter a closed Job. Reuse an actual retained
                # zero only; unknown accounting from the first attempt stays unknown.
                result["active_after_cleanup"] = self.accounting_after_close
            result["forced_termination"] = self.forced_history
        finally:
            for process in tuple(self.query_handles):
                try:
                    process.close()
                    self.query_handles.remove(process)
                except OSError as exc:
                    result["errors"].append(str(exc))
            for thread in tuple(self.thread_handles):
                try:
                    self.api.close(thread)
                    self.thread_handles.remove(thread)
                except OSError as exc:
                    result["errors"].append(str(exc))
            if self.handle is not None:
                try:
                    # Exact KILL_ON_JOB_CLOSE remains the final owned-only
                    # fallback. Never discard unassigned process handles here.
                    self.api.close(self.handle)
                    self.handle = None
                    result["job_handle_closed"] = True
                except OSError as exc:
                    result["errors"].append(str(exc))
            for process in self.processes:
                if process.handle is None:
                    continue
                if process.assigned and result["active_after_cleanup"] == 0:
                    process.stop_confirmed = True
                elif process.assigned and not process.stop_confirmed:
                    try:
                        process.stop_confirmed = self.api.wait(process.handle, 10_000)
                    except OSError as exc:
                        result["errors"].append(str(exc))
                if not process.stop_confirmed:
                    continue
                try:
                    process.close()
                except OSError as exc:
                    result["errors"].append(str(exc))
            result["owned_process_handles_remaining"] = sum(process.handle is not None for process in self.processes)
            result["owned_process_handles_remaining"] += len(self.query_handles)
            result["owned_thread_handles_remaining"] = len(self.thread_handles)
        if (result["job_handle_closed"] and result["owned_process_handles_remaining"] == 0
                and result["owned_thread_handles_remaining"] == 0):
            self.cleanup_result = {**result, "errors": list(result["errors"])}
        return result


def ordinary(path: Path) -> None:
    for ancestor in (path, *path.parents):
        if ancestor.exists() or ancestor.is_symlink():
            if ancestor.is_symlink() or getattr(ancestor.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("owned acceptance paths cannot contain reparse points")


def write_once(path: Path, payload: Any) -> None:
    ordinary(path)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def valid_uuid(value: Any) -> bool:
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def retained_data(root: Path, output: Path, *, data_directory: str | None = None,
                  owner_run_id: str | None = None) -> tuple[Path, Path, str]:
    """Own fresh data, or reuse only the same explicit collector-owned DB.

    No cleanup/delete exists here. The owner marker is separate from the exact
    production bridge marker, whose schema must remain unchanged.
    """
    boundary = root / "build/v1600-evidence"
    ordinary(output)
    if not output.resolve().is_relative_to(boundary.resolve()) or output.resolve() == boundary.resolve():
        raise ValueError("launch output must remain under the acceptance boundary")
    if (data_directory is None) != (owner_run_id is None):
        raise ValueError("persistent data directory and owner run ID must be supplied together")
    if data_directory is None:
        run = output.with_suffix(".run")
        ordinary(run)
        if run.exists():
            raise ValueError("owned launch namespace must be fresh")
        run.mkdir(parents=True, exist_ok=False)
        data = run / "data"
        data.mkdir()
        owner_run_id = str(uuid.uuid4())
        marker = {"schema_version": 1, "protocol_version": "retained-desktop-data-v1",
                  "created_by": "record-rc-desktop-startup", "owner_run_id": owner_run_id,
                  "data_directory": data.relative_to(root).as_posix()}
        write_once(run / "run-owner.json", marker)
    else:
        relative = Path(data_directory)
        if relative.is_absolute() or ".." in relative.parts or not valid_uuid(owner_run_id):
            raise ValueError("persistent data must be repository-relative with an exact UUID owner")
        data, run = root / relative, (root / relative).parent
        ordinary(data)
        if (data.name != "data" or not data.is_dir()
                or not run.resolve().is_relative_to(boundary.resolve()) or run.resolve() == boundary.resolve()):
            raise ValueError("persistent data is not a scoped, existing acceptance data directory")
        owner = run / "run-owner.json"
        ordinary(owner)
        if not owner.is_file() or owner.stat().st_size > 4096:
            raise ValueError("persistent data lacks its bounded ordinary owner marker")
        marker = json.loads(owner.read_text(encoding="utf-8"))
        expected = {"schema_version": 1, "protocol_version": "retained-desktop-data-v1",
                    "created_by": "record-rc-desktop-startup", "owner_run_id": owner_run_id,
                    "data_directory": data.relative_to(root).as_posix()}
        if marker != expected:
            raise ValueError("persistent data belongs to another acceptance owner")
    return run, data, owner_run_id


def rotate_launch_marker(run: Path, data: Path, owner_run_id: str, nonce: str) -> Path:
    """Archive only a prior completed launch in this exact retained owner.

    Caller must invoke this before any new application starts. The durable
    completion is emitted only after exact Job active count becomes zero.
    """
    if not valid_uuid(nonce):
        raise ValueError("launch nonce must be a UUID")
    ordinary(run)
    ordinary(data)
    bridge = data / "rc-acceptance-owner.json"
    receipt = data / "rc-desktop-observation.json"
    staging = data / "rc-desktop-observation.tmp"
    for path in (bridge, receipt, staging):
        ordinary(path)
    if bridge.exists():
        ordinary(bridge)
        if bridge.stat().st_size > 4096:
            raise ValueError("old launch owner is not bounded")
        previous = json.loads(bridge.read_text(encoding="utf-8"))
        old_nonce = previous.get("acceptance_nonce")
        expected = {"schema_version": 1, "acceptance_nonce": old_nonce,
                    "isolated_test_data": True, "created_by": "record-rc-desktop-startup"}
        if not valid_uuid(old_nonce) or previous != expected:
            raise ValueError("old launch marker is not collector-owned")
        archive = run / "launches" / old_nonce
        ordinary(archive)
        completion = archive / "completion.json"
        ordinary(completion)
        if not completion.is_file() or completion.stat().st_size > 8192:
            raise ValueError("old launch has no retained cleanup completion")
        completed = json.loads(completion.read_text(encoding="utf-8"))
        cleanup = completed.get("process_cleanup", {})
        if (completed.get("owner_run_id") != owner_run_id or completed.get("acceptance_nonce") != old_nonce
                or cleanup.get("protocol_version") != "exact-native-job-v1"
                or type(cleanup.get("active_after_cleanup")) is not int or cleanup["active_after_cleanup"] != 0
                or cleanup.get("job_handle_closed") is not True or cleanup.get("errors") != []
                or cleanup.get("unassigned_cleanup_complete") is not True
                or type(cleanup.get("owned_process_handles_remaining")) is not int
                or cleanup["owned_process_handles_remaining"] != 0
                or type(cleanup.get("owned_thread_handles_remaining")) is not int
                or cleanup["owned_thread_handles_remaining"] != 0):
            raise ValueError("old launch processes were not proven stopped")
        for name in ("rc-acceptance-owner.json", "rc-desktop-observation.json", "rc-desktop-observation.tmp"):
            source, destination = data / name, archive / name
            ordinary(source)
            ordinary(destination)
            if source.exists():
                if destination.exists() or not source.is_file():
                    raise ValueError("old launch archive would overwrite evidence")
                source.rename(destination)
    elif receipt.exists() or staging.exists():
        raise ValueError("unowned old desktop receipt/staging cannot be rotated")
    archive = run / "launches" / nonce
    ordinary(archive)
    archive.mkdir(parents=True, exist_ok=False)
    write_once(bridge, {"schema_version": 1, "acceptance_nonce": nonce,
                       "isolated_test_data": True, "created_by": "record-rc-desktop-startup"})
    return archive
