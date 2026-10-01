"""Explicitly launch one desktop in test-owned data and retain its render receipt.

The production acceptance bridge must report React, Tauri and authenticated
Sidecar identities. An older app without this bridge times out and fails; a
backend health response alone cannot qualify desktop startup.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from rc_gate import ROOT, MANIFEST_FIELDS, attachment, executable_attachment, module
from rc_test_evidence import controlled_environment

RECEIPT_NAME = "rc-desktop-observation.json"


def file_reference(path: Path) -> dict[str, str]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": path.relative_to(ROOT).as_posix(), "sha256": digest.hexdigest()}


def _process_information(pid: int) -> dict:
    if type(pid) is not int or pid <= 0:
        raise ValueError("runtime process ID is invalid")
    command = ["powershell", "-NoProfile", "-Command",
               "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
               f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; if ($null -eq $p) {{ exit 1 }}; "
               "$p | Select-Object ProcessId,ParentProcessId,ExecutablePath | ConvertTo-Json -Compress"]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        raise ValueError("could not inspect the actual desktop-owned process")
    return json.loads(result.stdout)


def validate_receipt(receipt: dict, *, nonce: str, pid: int, desktop: Path, sidecar: Path, allow_development: bool = False) -> dict:
    if (receipt.get("schema_version") != 1 or receipt.get("report_type") != "rc_desktop_runtime_observation"
            or receipt.get("protocol_version") != "desktop-render-ready-v1" or receipt.get("status") != "PASS"
            or receipt.get("actual_run") is not True or receipt.get("acceptance_nonce") != nonce
            or receipt.get("isolated_test_data") is not True or receipt.get("desktop_render_ready") is not True
            or receipt.get("sidecar_ready") is not True):
        raise ValueError("desktop did not provide this launch's actual render/identity receipt")
    processes = receipt.get("process_ids", {})
    if processes.get("desktop") != pid:
        raise ValueError("desktop receipt belongs to a different process")
    for component, expected in (("desktop", desktop), ("sidecar", sidecar)):
        actual = _process_information(processes.get(component))
        path = actual.get("ExecutablePath")
        if not isinstance(path, str) or Path(path).resolve() != expected.resolve():
            raise ValueError(f"{component} process executed a different binary")
        if component == "sidecar" and actual.get("ParentProcessId") != pid:
            raise ValueError("reported sidecar is not owned by this desktop process")
    components = receipt.get("components")
    if not isinstance(components, dict) or set(components) != {"react", "tauri", "sidecar"}:
        raise ValueError("all three actually observed component manifests are required")
    manifest = components["tauri"]
    if not isinstance(manifest, dict):
        raise ValueError("Tauri manifest is missing")
    for component, value in components.items():
        if not isinstance(value, dict) or any(value.get(field) != manifest.get(field) for field in MANIFEST_FIELDS):
            raise ValueError(f"{component} runtime identity disagrees with Tauri")
        if ((value.get("workspace_state") != "CLEAN" and not (allow_development and value.get("workspace_state") == "DIRTY"))
                or value.get("build_type") != "Release"):
            raise ValueError("desktop startup acceptance requires a clean Release build")
    elapsed = receipt.get("readiness_ms")
    if type(elapsed) is not int or elapsed <= 0:
        raise ValueError("desktop receipt has no positive actual readiness measurement")
    return manifest


def close_owned_desktop(process: subprocess.Popen, window_handle: int) -> None:
    """Close the actual native main HWND, never framework dispatch windows."""
    user = ctypes.WinDLL("user32", use_last_error=True)
    from ctypes import wintypes
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.PostMessageW.restype = wintypes.BOOL
    user.IsWindow.argtypes = [wintypes.HWND]
    user.IsWindow.restype = wintypes.BOOL
    if type(window_handle) is not int or not 0 < window_handle < 2 ** 64 or not user.IsWindow(window_handle):
        raise ValueError("actual native main HWND is unavailable")
    owner = wintypes.DWORD()
    user.GetWindowThreadProcessId(window_handle, ctypes.byref(owner))
    if owner.value != process.pid or process.poll() is not None:
        raise ValueError("native main HWND does not belong to the retained test process")
    if not user.PostMessageW(window_handle, 0x0010, 0, 0):
        raise ValueError("could not post close to the test-owned main window")
    print(json.dumps({"phase": "close_owned_desktop", "owned_main_window_count": 1}), flush=True)
    # Native shutdown has a 2 s request and a 10 s sidecar budget; leave a
    # separate bounded margin for WebView disposal and process completion.
    process.wait(timeout=30)


def cleanup_owned_processes(process, owned_sidecar, owned_sidecar_path) -> None:
    if process is not None and process.poll() is None:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        process.wait(timeout=15)
    if owned_sidecar is not None and _process_exists(owned_sidecar):
        actual = _process_information(owned_sidecar)
        if (process is not None and actual.get("ParentProcessId") == process.pid
                and Path(actual.get("ExecutablePath", "")).resolve() == owned_sidecar_path.resolve()):
            subprocess.run(["taskkill", "/PID", str(owned_sidecar), "/T", "/F"], capture_output=True, check=False)


@contextmanager
def isolated_desktop_data(boundary: Path, cleanup):
    with tempfile.TemporaryDirectory(prefix="rc-desktop-data-", dir=boundary, ignore_cleanup_errors=True) as data:
        try:
            yield data
        finally:
            # Stop the retained test-owned tree before removing its database.
            cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop", required=True)
    parser.add_argument("--sidecar", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cache-state", choices=("warm",), required=True)
    parser.add_argument("--startup-path", choices=("installed-nsis", "installed-msi", "portable-desktop"), required=True)
    parser.add_argument("--development-candidate", action="store_true", help="Record a DIRTY build only as DEVELOPMENT_PASS; never RC eligible")
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("desktop startup acceptance requires Windows")
    relative = Path(args.output)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".json":
        parser.error("output must be a fresh repository-relative JSON")
    output = ROOT / relative
    boundary = ROOT / "build/v1600-evidence"
    if not output.resolve().is_relative_to(boundary.resolve()) or output.exists():
        parser.error("output must be fresh under build/v1600-evidence")
    for parent in output.parents:
        if parent == ROOT:
            break
        if parent.exists() and (parent.is_symlink() or getattr(parent.lstat(), "st_file_attributes", 0) & 0x400):
            parser.error("output parent is a reparse point")
    process = None
    owned_sidecar = None
    owned_sidecar_path = None
    started = time.perf_counter()
    try:
        binaries = {key: file_reference(ROOT / value) for key, value in (("desktop", args.desktop), ("sidecar", args.sidecar))}
        paths = {key: executable_attachment(ROOT, value) for key, value in binaries.items()}
        source_before = module("generate_build_info")._release_source_identity(ROOT)
        payload_before = module("rc_payload_inventory").inventory(ROOT, binaries["sidecar"])
        output.parent.mkdir(parents=True, exist_ok=True)
        with isolated_desktop_data(boundary, lambda: cleanup_owned_processes(process, owned_sidecar, owned_sidecar_path)) as data:
            environment = controlled_environment(dict(os.environ))
            nonce = str(uuid.uuid4())
            marker = {"schema_version": 1, "acceptance_nonce": nonce, "isolated_test_data": True,
                      "created_by": "record-rc-desktop-startup"}
            with (Path(data) / "rc-acceptance-owner.json").open("x", encoding="utf-8") as stream:
                json.dump(marker, stream)
                stream.flush()
                os.fsync(stream.fileno())
            environment.update(AGENT_DATA_ROOT=data, AGENT_DESKTOP_DATA_DIRECTORY=data, SIYI_DESKTOP_ACCEPTANCE="1",
                               SIYI_DESKTOP_ACCEPTANCE_NONCE=nonce,
                               WEBVIEW2_USER_DATA_FOLDER=str(Path(data) / "webview2"))
            for name in ("SIYI_BUILD_MANIFEST", "SIYI_BUILD_INFO_LOCKED", "AGENT_DATABASE_PATH", "AGENT_LOG_PATH"):
                environment.pop(name, None)
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            # This is the interactive desktop under test, not a background
            # helper. Hiding its WebView suspends animation-frame rendering and
            # cannot establish a render-ready startup observation.
            startup.wShowWindow = 1
            started = time.perf_counter()
            process = subprocess.Popen([str(paths["desktop"])], cwd=paths["desktop"].parent, env=environment, startupinfo=startup)
            receipt_path = Path(data) / RECEIPT_NAME
            while not receipt_path.exists():
                if process.poll() is not None or time.perf_counter() - started > 60:
                    # Retain bounded, test-owned diagnostics before TemporaryDirectory
                    # cleans up. These are failure logs, never acceptance evidence.
                    logs = {}
                    for name in ("siyi-shell.log", "agent.log"):
                        log_path = Path(data) / "logs" / name
                        if log_path.exists() and not log_path.is_symlink() and not (getattr(log_path.lstat(), "st_file_attributes", 0) & 0x400):
                            with log_path.open("rb") as stream:
                                stream.seek(max(0, log_path.stat().st_size - 128 * 1024))
                                logs[name] = stream.read(128 * 1024).decode("utf-8", "replace")
                    with output.with_suffix(".failure.json").open("x", encoding="utf-8") as stream:
                        json.dump({"report_type": "desktop_startup_failure_diagnostics", "status": "FAIL",
                                   "actual_run": True, "rc_eligible": False, "isolated_test_data": True,
                                   "binary_sha256": {key: value["sha256"] for key, value in binaries.items()},
                                   "source": source_before, "logs": logs}, stream, ensure_ascii=False, indent=2)
                    raise ValueError("desktop exited or timed out without a render-ready identity receipt")
                time.sleep(.05)
            readiness = max(1, round((time.perf_counter() - started) * 1000))
            if receipt_path.is_symlink() or getattr(receipt_path.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("desktop observation is a reparse point")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            manifest = validate_receipt(receipt, nonce=nonce, pid=process.pid, allow_development=args.development_candidate, **paths)
            with output.with_suffix(".application.json").open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False, indent=2)
            owned_sidecar = receipt["process_ids"]["sidecar"]
            owned_sidecar_path = paths["sidecar"]
            source = {"source_version": manifest["product_version"], "source_commit": manifest["git_commit"],
                      "workspace_clean": manifest["workspace_state"] == "CLEAN", "source_tree_fingerprint": manifest["source_fingerprint"]}
            if any(file_reference(paths[key]) != binaries[key] for key in binaries):
                raise ValueError("executables changed during desktop startup")
            if module("rc_payload_inventory").inventory(ROOT, binaries["sidecar"]) != payload_before:
                raise ValueError("sidecar's complete internal payload changed during desktop startup")
            source_after = module("generate_build_info")._release_source_identity(ROOT)
            if source_after != source_before:
                raise ValueError("repository source changed during the actual observation")
            # Each component entry comes from the native app receipt, not a
            # developer-supplied adjacent manifest. Preserve the raw receipt.
            status = "PASS" if source["workspace_clean"] else "DEVELOPMENT_PASS"
            payload = {"schema_version": 1, "report_type": "rc_desktop_startup_observation", "actual_run": True,
                       "status": status, "rc_eligible": source["workspace_clean"], "measurement_object": "desktop", "measurement_protocol": "desktop-render-ready-v1",
                       "startup_path": args.startup_path, "cache_state": args.cache_state,
                       "host_fingerprint": hashlib.sha256((os.environ.get("COMPUTERNAME", "") + "\0" + sys.platform).encode()).hexdigest(),
                       "binary_sha256": {key: value["sha256"] for key, value in binaries.items()}, "build_id": manifest["build_id"],
                       "source": source, "source_after": source, "collector_source": source_before, "collector_source_after": source_after,
                       "desktop_render_ready": True, "sidecar_ready": True, "isolated_test_data": True,
                       "readiness_ms": readiness, "runtime_readiness_ms": receipt["readiness_ms"], "application_receipt": receipt}
            # Exit only this owned process; the desktop shutdown must clean its
            # own sidecar. Never enumerate or stop other application instances.
            close_owned_desktop(process, receipt.get("window_handle"))
            shutdown_deadline = time.monotonic() + 15
            while _process_exists(receipt["process_ids"]["sidecar"]) and time.monotonic() < shutdown_deadline:
                time.sleep(.1)
            if _process_exists(receipt["process_ids"]["sidecar"]):
                raise ValueError("the owned sidecar survived desktop shutdown")
            payload_path = output.with_suffix(".payload.json")
            with payload_path.open("x", encoding="utf-8") as stream:
                json.dump(payload_before, stream, ensure_ascii=False, indent=2)
            payload["sidecar_payload"] = file_reference(payload_path)
            with output.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            print(json.dumps({"status": status, "rc_eligible": source["workspace_clean"], "scope": "desktop_render_startup", "readiness_ms": readiness}))
            return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "FAIL", "detail": str(exc)}))
        return 1
    finally:
        cleanup_owned_processes(process, owned_sidecar, owned_sidecar_path)


def _process_exists(pid: int) -> bool:
    try:
        _process_information(pid)
    except (OSError, ValueError):
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
