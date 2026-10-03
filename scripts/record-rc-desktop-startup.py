"""Explicitly launch one desktop in retained test-owned data.

The production render-ready protocol is unchanged. This collector
never qualifies a backend-only observation as desktop startup acceptance.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from rc_gate import ROOT, MANIFEST_FIELDS, executable_attachment, module
from rc_owned_desktop import OwnedDesktopJob, ordinary, retained_data, rotate_launch_marker, write_once
from rc_owned_desktop import RetainedProcess

RECEIPT_NAME = "rc-desktop-observation.json"


def file_reference(path: Path) -> dict[str, str]:
    ordinary(path)
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
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=15, check=False)
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


def close_owned_desktop(process: RetainedProcess, window_handle: int) -> None:
    """Close only the actual main HWND while its retained process is running."""
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
    if process.wait(timeout=30) != 0:
        raise ValueError("native desktop exited unsuccessfully after main-window shutdown")


def desktop_environment(source: dict[str, str], data: Path, nonce: str) -> dict[str, str]:
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC", "COMPUTERNAME",
               "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROGRAMFILES", "PROGRAMFILES(X86)",
               "PROGRAMW6432", "OS"}
    environment = {key: value for key, value in source.items() if key.upper() in allowed}
    for relative in ("home/AppData/Roaming", "home/AppData/Local", "temp", "cache", "config"):
        directory = data / relative
        ordinary(directory)
        directory.mkdir(parents=True, exist_ok=True)
    env_file = data / "config/acceptance.env"
    ordinary(env_file)
    if env_file.exists():
        if not env_file.is_file() or env_file.stat().st_size:
            raise ValueError("acceptance environment file must remain an ordinary empty fixture")
    else:
        with env_file.open("xb"):
            pass
    environment.update(AGENT_DATA_ROOT=str(data), AGENT_DESKTOP_DATA_DIRECTORY=str(data),
                       SIYI_DESKTOP_ACCEPTANCE="1", SIYI_DESKTOP_ACCEPTANCE_NONCE=nonce,
                       WEBVIEW2_USER_DATA_FOLDER=str(data / "webview2"),
                       HOME=str(data / "home"), USERPROFILE=str(data / "home"),
                       APPDATA=str(data / "home/AppData/Roaming"), LOCALAPPDATA=str(data / "home/AppData/Local"),
                       TEMP=str(data / "temp"), TMP=str(data / "temp"),
                       XDG_CONFIG_HOME=str(data / "config"), XDG_CACHE_HOME=str(data / "cache"),
                       PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PYTHONIOENCODING="utf-8",
                       SIYI_ALLOW_PAID_API="false", SIYI_TEST_PROVIDER="mock",
                       AGENT_ENV_FILE=str(data / "config/acceptance.env"),
                       AGENT_DEEPSEEK_API_KEY="", AGENT_TAVILY_API_KEY="", AGENT_BRAVE_API_KEY="",
                       HF_HOME=str(data / "cache/huggingface"), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                       TORCH_HOME=str(data / "cache/torch"), NO_PROXY="*")
    return environment


def failure_logs(data: Path | None) -> dict[str, str]:
    result = {}
    if data is None:
        return result
    for name in ("siyi-shell.log", "agent.log"):
        path = data / "logs" / name
        ordinary(path)
        if path.is_file():
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 128 * 1024))
                result[name] = stream.read(128 * 1024).decode("utf-8", "replace")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop", required=True)
    parser.add_argument("--sidecar", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cache-state", choices=("warm",), required=True)
    parser.add_argument("--startup-path", choices=("installed-nsis", "installed-msi", "portable-desktop"), required=True)
    parser.add_argument("--development-candidate", action="store_true", help="Record a DIRTY build only as DEVELOPMENT_PASS; never RC eligible")
    parser.add_argument("--data-directory", help="Reuse only the exact retained acceptance-owned repository-relative data directory")
    parser.add_argument("--owner-run-id", help="UUID owner of --data-directory; never infer ownership from a path")
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("desktop startup acceptance requires Windows")
    relative = Path(args.output)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".json":
        parser.error("output must be a fresh repository-relative JSON")
    output = ROOT / relative
    boundary = ROOT / "build/v1600-evidence"
    ordinary(output)
    if not output.resolve().is_relative_to(boundary.resolve()) or output.exists():
        parser.error("output must be fresh under build/v1600-evidence")
    # Never overwrite partial evidence from a prior attempt either.
    for suffix in (".application.json", ".payload.json", ".failure.json"):
        path = output.with_suffix(suffix)
        ordinary(path)
        if path.exists():
            parser.error("output companion evidence must also be fresh")
    output.parent.mkdir(parents=True, exist_ok=True)
    job = data = archive = owner_run_id = None
    nonce = str(uuid.uuid4())
    payload = binaries = source_before = None
    error = None
    actual_run = False
    try:
        for value in (args.desktop, args.sidecar):
            if Path(value).is_absolute() or ".." in Path(value).parts:
                raise ValueError("executable must remain repository-relative")
        binaries = {key: file_reference(ROOT / value) for key, value in (("desktop", args.desktop), ("sidecar", args.sidecar))}
        paths = {key: executable_attachment(ROOT, value) for key, value in binaries.items()}
        source_before = module("generate_build_info")._release_source_identity(ROOT)
        payload_before = module("rc_payload_inventory").inventory(ROOT, binaries["sidecar"])
        output.parent.mkdir(parents=True, exist_ok=True)
        run, data, owner_run_id = retained_data(ROOT, output, data_directory=args.data_directory, owner_run_id=args.owner_run_id)
        archive = rotate_launch_marker(run, data, owner_run_id, nonce)
        environment = desktop_environment(dict(os.environ), data, nonce)
        write_once(archive / "attempt.json", {"schema_version": 1, "owner_run_id": owner_run_id,
                   "acceptance_nonce": nonce, "actual_run": False, "output": relative.as_posix(),
                   "binary_sha256": {key: value["sha256"] for key, value in binaries.items()}})
        job = OwnedDesktopJob()
        # Keep the protocol's process-launch-to-receipt timing. Job creation is
        # setup outside the measured interval; exact suspended native creation,
        # assignment and resume are included and applied equally to both sides.
        started = time.perf_counter()
        process = job.launch(paths["desktop"], environment)
        actual_run = True
        receipt_path = data / RECEIPT_NAME
        while not receipt_path.exists():
            if process.poll() is not None or time.perf_counter() - started > 60:
                raise ValueError("desktop exited or timed out without a render-ready identity receipt")
            time.sleep(.05)
        readiness = max(1, round((time.perf_counter() - started) * 1000))
        ordinary(receipt_path)
        if not receipt_path.is_file() or receipt_path.stat().st_size > 64 * 1024:
            raise ValueError("desktop observation is not a bounded ordinary file")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        manifest = validate_receipt(receipt, nonce=nonce, pid=process.pid, allow_development=args.development_candidate, **paths)
        sidecar = job.observe_sidecar(receipt["process_ids"]["sidecar"], paths["sidecar"])
        write_once(output.with_suffix(".application.json"), receipt)
        source = {"source_version": manifest["product_version"], "source_commit": manifest["git_commit"],
                  "workspace_clean": manifest["workspace_state"] == "CLEAN", "source_tree_fingerprint": manifest["source_fingerprint"]}
        if any(file_reference(paths[key]) != binaries[key] for key in binaries):
            raise ValueError("executables changed during desktop startup")
        if module("rc_payload_inventory").inventory(ROOT, binaries["sidecar"]) != payload_before:
            raise ValueError("sidecar's complete internal payload changed during desktop startup")
        source_after = module("generate_build_info")._release_source_identity(ROOT)
        if source_after != source_before:
            raise ValueError("repository source changed during the actual observation")
        status = "PASS" if source["workspace_clean"] else "DEVELOPMENT_PASS"
        payload = {"schema_version": 1, "report_type": "rc_desktop_startup_observation", "actual_run": True,
                   "status": status, "rc_eligible": source["workspace_clean"], "measurement_object": "desktop", "measurement_protocol": "desktop-render-ready-v1",
                   "startup_path": args.startup_path, "cache_state": args.cache_state,
                   "host_fingerprint": hashlib.sha256((os.environ.get("COMPUTERNAME", "") + "\0" + sys.platform).encode()).hexdigest(),
                   "binary_sha256": {key: value["sha256"] for key, value in binaries.items()}, "build_id": manifest["build_id"],
                   "source": source, "source_after": source, "collector_source": source_before, "collector_source_after": source_after,
                   "desktop_render_ready": True, "sidecar_ready": True, "isolated_test_data": True,
                   "readiness_ms": readiness, "runtime_readiness_ms": receipt["readiness_ms"], "application_receipt": receipt}
        close_owned_desktop(process, receipt.get("window_handle"))
        # Native retained handle, not a reused PID, proves sidecar completion.
        if sidecar.wait(timeout=15) != 0:
            raise ValueError("native sidecar exited unsuccessfully after desktop shutdown")
        job.wait_empty(15)
        payload_path = output.with_suffix(".payload.json")
        write_once(payload_path, payload_before)
        payload["sidecar_payload"] = file_reference(payload_path)
    except (Exception, KeyboardInterrupt) as exc:
        error = type(exc).__name__ + ": " + str(exc)
    finally:
        # launch may resume successfully and then fail while closing a thread
        # handle. That was a real application run even if launch did not return.
        actual_run = actual_run or (job is not None and "resumed" in getattr(job, "launch_events", ()))
        cleanup = job.cleanup() if job is not None else {
            "protocol_version": "exact-native-job-v1", "active_before_cleanup": None,
            "active_after_cleanup": None, "forced_termination": None, "job_handle_closed": None,
            "not_created": True, "errors": []}
        if job is not None and (cleanup["active_after_cleanup"] != 0 or not cleanup["job_handle_closed"] or cleanup["errors"]
                or cleanup.get("unassigned_cleanup_complete") is not True
                or cleanup.get("owned_process_handles_remaining") != 0
                or cleanup.get("owned_thread_handles_remaining") != 0):
            error = error or "owned Job cleanup could not be authoritatively verified"
        if payload is not None and cleanup.get("forced_termination") is not False:
            error = error or "forced Job cleanup is not successful native desktop shutdown"
        if archive is not None:
            try:
                write_once(archive / "completion.json", {"schema_version": 1, "owner_run_id": owner_run_id,
                           "acceptance_nonce": nonce, "actual_run": actual_run,
                           "status": "FAIL" if error else payload["status"], "process_cleanup": cleanup})
            except (OSError, ValueError) as exc:
                error = error or "could not retain cleanup completion: " + str(exc)
    if error:
        logs, diagnostic_error = {}, None
        try:
            logs = failure_logs(data)
        except (OSError, ValueError) as exc:
            diagnostic_error = str(exc)
        write_once(output.with_suffix(".failure.json"), {
            "schema_version": 1, "report_type": "desktop_startup_failure_diagnostics", "status": "FAIL",
            "actual_run": actual_run, "rc_eligible": False, "isolated_test_data": data is not None,
            "binary_sha256": None if binaries is None else {key: value["sha256"] for key, value in binaries.items()},
            "source": source_before, "detail": error, "logs": logs, "diagnostic_error": diagnostic_error,
            "process_cleanup": cleanup, "owner_run_id": owner_run_id,
            "data_directory": data.relative_to(ROOT).as_posix() if data else None})
        print(json.dumps({"status": "FAIL", "detail": error}))
        return 1
    payload.update(process_cleanup=cleanup, owner_run_id=owner_run_id,
                   data_directory=data.relative_to(ROOT).as_posix(),
                   cleanup_completion=file_reference(archive / "completion.json"))
    write_once(output, payload)
    print(json.dumps({"status": payload["status"], "rc_eligible": payload["rc_eligible"],
                      "scope": "desktop_render_startup", "readiness_ms": payload["readiness_ms"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
